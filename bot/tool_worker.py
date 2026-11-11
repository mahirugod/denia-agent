# -*- coding: utf-8 -*-
"""工具子进程 worker

从 stdin 读 JSON 行请求，执行对应工具后把结果写回 stdout。
业务模块里的 print 全部改道 stderr，避免污染协议流。
"""
import json
import os
import sys

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

# 协议流强制 UTF-8：Windows 下子进程 stdout/stdin 默认跟随系统代码页（GBK），
# 父进程按 UTF-8 读写，中文结果会整段变成乱码（搜索/天气/时间都受影响）
try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_PROTO = sys.stdout          # 协议专用通道（父进程的管道）
sys.stdout = sys.stderr      # 其余 print 一律走 stderr

import tool_registry         # 工具注册表
import skills                # 触发技能包加载（skills/__init__ 导入时自动 load_skills）


def _handle(req):
    name = req.get("name") or ""
    query = req.get("query") or ""
    info = tool_registry.get_tool(name)
    if info is None:
        return None, f"unknown tool: {name}"
    try:
        return info.handler(query), ""
    except Exception as e:
        import traceback
        return None, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}"


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        result, err = _handle(req)
        _PROTO.write(json.dumps(
            {"id": req.get("id"), "result": result, "error": err},
            ensure_ascii=False) + "\n")
        _PROTO.flush()


if __name__ == "__main__":
    main()
