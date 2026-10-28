# -*- coding: utf-8 -*-
"""工具子进程隔离

把无状态的纯查询工具丢进常驻子进程执行（工具 ↔ 主进程走 JSON 行协议）。
工具卡死或崩溃时只杀子进程并在下次调用时重启，主对话不受影响。

有状态/需要弹窗的工具（提醒、发消息、剪贴板以外的本机操作、文件写入、
UI 自动化、截屏等）仍留在主进程执行。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(BOT_DIR, "tool_worker.py")
SETTINGS = os.path.join(os.path.dirname(BOT_DIR), "settings.json")

# 只放无状态、纯查询、无弹窗的工具
ISOLATED_TOOLS = {
    "web_search",
    "query_weather",
    "query_time",
    "image_search",
    "bilibili_feed",
    "model_detect",
    "read_clipboard",
    "write_clipboard",
}

_TIMEOUTS = {"web_search": 90.0, "bilibili_feed": 45.0, "image_search": 60.0}
_DEFAULT_TIMEOUT = 60.0
_IDLE_CLOSE = 300.0     # 空闲 5 分钟关掉子进程，释放内存

_cfg = {"at": 0.0, "on": True}


def enabled() -> bool:
    """settings.json 的 tool_isolation 开关，10 秒缓存"""
    now = time.time()
    if now - _cfg["at"] < 10:
        return _cfg["on"]
    _cfg["at"] = now
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            _cfg["on"] = bool(json.load(f).get("tool_isolation", True))
    except Exception:
        _cfg["on"] = True
    return _cfg["on"]


class _Worker:
    def __init__(self):
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.seq = 0
        self.last_used = 0.0
        self._guard = None

    # ── 生命周期 ──
    def _spawn(self):
        self.q = queue.Queue()
        self.proc = subprocess.Popen(
            [sys.executable, WORKER],
            cwd=BOT_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._reader, args=(self.proc, self.q),
                         name="tool-worker-reader", daemon=True).start()

    @staticmethod
    def _reader(proc, q):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    q.put(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass
        q.put({"__closed__": True})

    def _kill(self):
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def _idle_loop(self):
        while True:
            time.sleep(30)
            if self.proc is None:
                continue
            if time.time() - self.last_used > _IDLE_CLOSE and self.lock.locked() is False:
                with self.lock:
                    if time.time() - self.last_used > _IDLE_CLOSE:
                        self._kill()

    # ── 调用 ──
    def run(self, name, query, timeout):
        with self.lock:
            self.last_used = time.time()
            if self.proc is None or self.proc.poll() is not None:
                try:
                    self._spawn()
                except Exception as e:
                    return None, f"worker spawn failed: {e}"
            if self._guard is None:
                self._guard = threading.Thread(target=self._idle_loop,
                                               name="tool-worker-idle", daemon=True)
                self._guard.start()

            self.seq += 1
            rid = self.seq
            try:
                self.proc.stdin.write(json.dumps(
                    {"id": rid, "name": name, "query": query},
                    ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                self._kill()
                return None, f"worker write failed: {e}"

            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    self._kill()
                    return None, f"timeout after {timeout:.0f}s"
                try:
                    msg = self.q.get(timeout=left)
                except queue.Empty:
                    self._kill()
                    return None, f"timeout after {timeout:.0f}s"
                if msg.get("__closed__"):
                    self._kill()
                    return None, "worker crashed"
                if msg.get("id") == rid:
                    return msg.get("result"), (msg.get("error") or "")


_WORKER = _Worker()


def try_run(name: str, query: str):
    """在子进程里执行工具。

    返回 (handled, result, error)：
      handled=False 表示不归子进程管（调用方回落到主进程）
      handled=True 且 error 非空表示子进程里执行失败或超时
    """
    if name not in ISOLATED_TOOLS or not enabled():
        return False, None, ""
    timeout = _TIMEOUTS.get(name, _DEFAULT_TIMEOUT)
    result, err = _WORKER.run(name, query, timeout)
    return True, result, err
