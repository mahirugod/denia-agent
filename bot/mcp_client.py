#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP（Model Context Protocol）子进程客户端 —— 纯 Python 标准库实现，零依赖。

借鉴 Open Interpreter 的 MCP 配置格式（command/args/cwd/env + enabled/disabled_tools
+ output_token_limit），但用最简协议层：JSON-RPC 2.0 over stdio（子进程双向管道）。

生命周期：
  1. 启动时从 settings.json 读 `mcp_servers` 声明块，为每个 server 起一个子进程
  2. initialize 握手 → tools/list 发现工具 → 注册进 tool_registry（带 mcp_ 前缀）
  3. 工具调用走 tools/call，结果按 harness 截断后注入 LLM
  4. 子进程崩溃自动重启（带退避）；关闭时 terminate

配置声明（settings.json）：
  "mcp_servers": {
    "my_server": {
      "command": "python",
      "args": ["my_mcp_server.py"],
      "cwd": "D:/tools",
      "env": {"API_KEY": "xxx"},
      "enabled_tools": ["a", "b"],
      "disabled_tools": ["c"],
      "output_token_limit": 200
    }
  }

工具命名规则：`mcp_<server>_<tool>`，避免与本地 @tool 撞名。
"""
import json
import os
import subprocess
import threading
import time
from typing import Optional, List, Dict, Any

# 全局：server 名 -> 已发现的工具信息 {name, description, inputSchema}
_MCP_TOOLS: Dict[str, List[Dict[str, Any]]] = {}
# server 名 -> 子进程句柄
_PROCS: Dict[str, "MCPClient"] = {}
# 工具输出截断（按 harness 档位，加载时设置）
_MAX_OUTPUT_CHARS: int = 400


def set_max_output_chars(n: int):
    """由 harness 档位设置（denia_llm 加载时调）"""
    global _MAX_OUTPUT_CHARS
    _MAX_OUTPUT_CHARS = n


class MCPClient:
    """单台 MCP 服务器子进程客户端（JSON-RPC over stdio）"""

    def __init__(self, name: str, command: str, args: List[str] = None,
                 cwd: str = "", env: Dict[str, str] = None,
                 enabled_tools: List[str] = None,
                 disabled_tools: List[str] = None,
                 output_token_limit: int = 0):
        self.name = name
        self.command = command
        self.args = args or []
        self.cwd = cwd or None
        self.env = env
        self.enabled_tools = set(enabled_tools or [])
        self.disabled_tools = set(disabled_tools or [])
        self.output_token_limit = output_token_limit or _MAX_OUTPUT_CHARS
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._rpc_id = 0
        self._started = False
        self._restart_count = 0
        self._last_restart_ts = 0.0

    # ── 子进程生命周期 ─────────────────────────────────────────

    def start(self) -> bool:
        """启动子进程 + initialize 握手。失败返回 False。"""
        if self._started:
            return True
        try:
            env = os.environ.copy()
            if self.env:
                env.update(self.env)
            cmd = [self.command] + self.args
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=env,
            )
        except Exception as e:
            print(f"[MCP] {self.name} 启动子进程失败: {e}")
            return False

        # initialize 握手
        init = self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "dania-terminal", "version": "1.0"},
        })
        if init is None:
            print(f"[MCP] {self.name} initialize 失败")
            self._stop_process()
            return False
        print(f"[MCP] {self.name} 已连接 (server={init.get('serverInfo', {}).get('name', '?')})")
        self._started = True
        return True

    def discover(self) -> List[Dict[str, Any]]:
        """tools/list 发现，按 enabled/disabled 过滤。"""
        if not self._started:
            self.start()
        raw = self._call("tools/list", {})
        if not raw:
            return []
        tools = raw.get("tools", [])
        result = []
        for t in tools:
            tname = t.get("name", "")
            if self.enabled_tools and tname not in self.enabled_tools:
                continue
            if tname in self.disabled_tools:
                continue
            result.append(t)
        _MCP_TOOLS[self.name] = result
        print(f"[MCP] {self.name} 发现 {len(result)} 个工具")
        return result

    def call_tool(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """调用工具，返回截断后的文本结果。"""
        if not self._started:
            self.start()
        result = self._call("tools/call", {
            "name": tool_name,
            "arguments": args,
        })
        if result is None:
            return None
        # 兼容两种返回：{content:[{type:"text",text:...}]} 或 {result: "..."}
        text_parts = []
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and "text" in block:
                        text_parts.append(block["text"])
                    elif isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
        elif isinstance(result.get("result"), str):
            text_parts.append(result["result"])
        text = "\n".join(text_parts).strip()
        if not text:
            return None
        # 截断（harness 档位）
        limit = self.output_token_limit
        if len(text) > limit:
            text = text[:limit] + f"\n…（MCP 结果已截断，原文 {len(text)} 字）"
        return text

    def _call(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """发一个 JSON-RPC 请求，收响应。带自动重启（最多 2 次，退避 2s）。"""
        with self._lock:
            for attempt in range(3):
                if self.proc is None or self.proc.poll() is not None:
                    if not self._maybe_restart():
                        return None
                req = {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": method,
                    "params": params,
                }
                try:
                    data = (json.dumps(req) + "\n").encode("utf-8")
                    self.proc.stdin.write(data)
                    self.proc.stdin.flush()
                    line = self.proc.stdout.readline()
                    if not line:
                        # 子进程退出
                        self._stop_process()
                        if not self._maybe_restart():
                            return None
                        continue
                    resp = json.loads(line.decode("utf-8"))
                    if "error" in resp:
                        print(f"[MCP] {self.name} {method} 错误: {resp['error']}")
                        return None
                    return resp.get("result")
                except Exception as e:
                    print(f"[MCP] {self.name} {method} 异常: {e}")
                    self._stop_process()
                    if not self._maybe_restart():
                        return None
        return None

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _maybe_restart(self) -> bool:
        """子进程崩溃自动重启（最多 3 次，指数退避）。"""
        now = time.time()
        if self._restart_count >= 3:
            print(f"[MCP] {self.name} 重启次数已达上限，放弃")
            self._started = False
            return False
        # 退避：2s * 2^count
        backoff = 2.0 * (2 ** self._restart_count)
        if now - self._last_restart_ts < backoff:
            time.sleep(backoff - (now - self._last_restart_ts))
        self._restart_count += 1
        self._last_restart_ts = now
        print(f"[MCP] {self.name} 重启中（第 {self._restart_count} 次）...")
        self._stop_process()
        if not self.start():
            return False
        # 重新发现工具
        self.discover()
        return True

    def _stop_process(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def close(self):
        self._stop_process()
        self._started = False


# ── 加载器：从 settings.json 读 mcp_servers ──────────────────────

def _load_server_configs() -> Dict[str, Dict[str, Any]]:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        settings_path = os.path.join(root, "settings.json")
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            return {}
        return servers
    except Exception as e:
        print(f"[MCP] 读取 settings.json 失败: {e}")
        return {}


def load_mcp_servers() -> List[str]:
    """启动时调用：起所有 MCP server，发现工具。返回已加载的 server 名。"""
    global _PROCS
    configs = _load_server_configs()
    loaded = []
    for name, cfg in configs.items():
        command = cfg.get("command", "")
        if not command:
            print(f"[MCP] {name} 缺 command，跳过")
            continue
        client = MCPClient(
            name=name,
            command=command,
            args=cfg.get("args", []),
            cwd=cfg.get("cwd", ""),
            env=cfg.get("env", {}),
            enabled_tools=cfg.get("enabled_tools", []),
            disabled_tools=cfg.get("disabled_tools", []),
            output_token_limit=cfg.get("output_token_limit", _MAX_OUTPUT_CHARS),
        )
        if client.start():
            _PROCS[name] = client
            client.discover()
            loaded.append(name)
        else:
            print(f"[MCP] {name} 启动失败")
    return loaded


def get_mcp_client(name: str) -> Optional[MCPClient]:
    return _PROCS.get(name)


def list_mcp_tools() -> List[str]:
    """列出所有 MCP 工具名（已注册到 tool_registry 的）"""
    from tool_registry import _TOOLS
    return [n for n in _TOOLS if n.startswith("mcp_")]


def register_mcp_tools_to_registry():
    """把已发现的 MCP 工具注册进 tool_registry（带 mcp_ 前缀）。

    每个 MCP 工具生成一个 handler：解析 query 里带不带参数都行，
    简单起见取 query 里 JSON 片段作为 args，没有就空参调用。
    """
    from tool_registry import ToolInfo, _TOOLS
    import re

    _ARGS_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)

    for server_name, tools in _MCP_TOOLS.items():
        client = _PROCS.get(server_name)
        if client is None:
            continue
        for t in tools:
            tname = t.get("name", "")
            if not tname:
                continue
            reg_name = f"mcp_{server_name}_{tname}"
            desc = t.get("description", "")

            def make_handler(c=client, tn=tname, d=desc, rn=reg_name):
                def handler(query: str) -> Optional[str]:
                    # 尝试从 query 提取 JSON 参数
                    args = {}
                    if query:
                        m = _ARGS_JSON_RE.search(query)
                        if m:
                            try:
                                args = json.loads(m.group(0))
                            except Exception:
                                args = {}
                    # 没有参数就按 description 作为兜底文本传过去
                    result = c.call_tool(tn, args)
                    if result:
                        return f"（MCP {rn} 结果：{result}。请用达妮娅的语气自然回答）"
                    return None
                return handler

            _TOOLS[reg_name] = ToolInfo(
                name=reg_name,
                description=f"[MCP] {desc}".strip(),
                keywords=[],  # MCP 工具走慢路径（模型自选调用）
                patterns=[],
                handler=make_handler(),
            )
    print(f"[MCP] 已注册 {len(list_mcp_tools())} 个 MCP 工具到 tool_registry")


def close_all():
    for c in _PROCS.values():
        c.close()
    _PROCS.clear()
