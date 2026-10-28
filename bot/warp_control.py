# -*- coding: utf-8 -*-
"""
Cloudflare WARP 开关
供控制台顶栏的小胶囊调用：查状态 / 连 / 断。

实现要点：
  - 只调用 warp-cli，不操作 GUI，不碰服务启动类型
  - 状态在后台线程每 8 秒刷新一次，前端轮询时直接读缓存，不卡界面
  - warp-svc 没装或没注册时返回 available/registered 标志，前端隐藏胶囊
"""
import os
import re
import subprocess
import threading
import time

_WARP_CLI = r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"

_CACHE = {"at": 0.0, "data": None}
_WORKER = {"thread": None, "lock": threading.Lock()}
_REFRESH_INTERVAL = 8.0


def available() -> bool:
    return os.path.isfile(_WARP_CLI)


def _run(args, timeout: float = 20):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [_WARP_CLI, "--accept-tos", *args],
        capture_output=True, text=True, timeout=timeout,
        creationflags=flags,
    )


def _parse(out: str) -> dict:
    """把 warp-cli status 的文本转成结构化状态"""
    text = (out or "").strip()
    low = text.lower()
    if re.search(r"status update:\s*connected", low):
        state = "connected"
    elif re.search(r"status update:\s*connecting", low):
        state = "connecting"
    elif re.search(r"status update:\s*disconnected", low):
        state = "disconnected"
    elif "registration missing" in low or "registration" in low and "missing" in low:
        state = "unregistered"
    elif "unable to connect" in low or "daemon" in low:
        state = "unavailable"
    else:
        state = "unknown"
    return {
        "available": True,
        "state": state,
        "connected": state == "connected",
        "text": text[:160],
    }


def _refresh() -> dict:
    """真正调一次 warp-cli status 并刷新缓存"""
    if not available():
        data = {"available": False, "state": "missing", "connected": False,
                "text": "未安装 Cloudflare WARP"}
        _CACHE["at"] = time.time()
        _CACHE["data"] = data
        return data
    try:
        p = _run(["status"], timeout=8)
        out = (p.stdout or "") + (p.stderr or "")
        data = _parse(out)
    except Exception as e:
        data = {"available": True, "state": "error", "connected": False,
                "text": f"状态查询失败: {str(e)[:60]}"}
    _CACHE["at"] = time.time()
    _CACHE["data"] = data
    return data


def _ensure_refresher():
    """后台刷新线程：前端轮询读缓存，避免每次都阻塞 GUI 线程"""
    with _WORKER["lock"]:
        t = _WORKER["thread"]
        if t is not None and t.is_alive():
            return
        def loop():
            while True:
                try:
                    _refresh()
                except Exception:
                    pass
                time.sleep(_REFRESH_INTERVAL)
        t = threading.Thread(target=loop, name="warp-status", daemon=True)
        _WORKER["thread"] = t
        t.start()


def status() -> dict:
    """当前状态（首次同步查一次，之后读后台缓存）"""
    if _CACHE["data"] is None:
        _refresh()
    elif time.time() - _CACHE["at"] > _REFRESH_INTERVAL * 2:
        _ensure_refresher()
    _ensure_refresher()
    return dict(_CACHE["data"] or {})


def set_enabled(on: bool) -> dict:
    """连接 / 断开，最多等 10 秒等状态翻转"""
    if not available():
        return {"available": False, "state": "missing", "connected": False,
                "text": "未安装 Cloudflare WARP"}
    try:
        _run(["connect" if on else "disconnect"], timeout=25)
    except Exception as e:
        return {"available": True, "state": "error", "connected": False,
                "text": f"命令执行失败: {str(e)[:60]}"}
    want = "connected" if on else "disconnected"
    for _ in range(20):
        data = _refresh()
        if data.get("state") == want:
            return data
        time.sleep(0.5)
    return dict(_CACHE["data"] or {})


def toggle() -> dict:
    """已连则断开，未连则连接"""
    cur = status()
    if cur.get("state") == "unregistered":
        return cur
    return set_enabled(not bool(cur.get("connected")))


# ── 开机自动连接 ──────────────────────────────────────────────
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")
_SETTING_CACHE = {"mtime": 0, "cfg": {}}


def _setting(key: str, default=None):
    try:
        mt = os.path.getmtime(_SETTINGS_PATH)
        if _SETTING_CACHE["mtime"] != mt:
            import json
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                _SETTING_CACHE["cfg"] = json.load(f)
            _SETTING_CACHE["mtime"] = mt
        return _SETTING_CACHE["cfg"].get(key, default)
    except Exception:
        return default


def _x_reachable(timeout: float = 5.0) -> bool:
    """探测目标站点（X）是否可直连，用来判断要不要帮忙连 WARP"""
    try:
        import requests
        r = requests.get(
            "https://x.com",
            timeout=timeout,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/124.0.0.0 Safari/537.36"},
        )
        r.close()
        return r.status_code < 400
    except Exception:
        return False


def maybe_auto_connect() -> str:
    """启动时调用：目标站点不通且 WARP 可用时自动连上。

    返回一句简短结果，便于日志排查：
      off / missing / unregistered / already / reachable / connected / failed
    """
    if not _setting("warp_auto_connect", True):
        return "off"
    if not available():
        return "missing"
    data = status()
    state = data.get("state")
    if state == "connected":
        return "already"
    if state in ("unregistered", "unavailable", "missing"):
        return state
    if _x_reachable():
        return "reachable"
    print("[WARP] x.com 直连不通，自动连接 WARP…")
    data = set_enabled(True)
    if data.get("connected"):
        print("[WARP] 已自动连接")
        return "connected"
    print(f"[WARP] 自动连接失败：{data.get('text', '')[:80]}")
    return "failed"


# ── 退出时断开 ────────────────────────────────────────────────
def disconnect_on_exit(timeout: float = 6.0) -> str:
    """关掉达妮娅终端时断开 WARP，别把隧道留在后台。

    失败不抛异常（退出流程不能被它卡住）；warp-cli 未安装时直接跳过。
    """
    if not available():
        return "missing"
    # 现查一次真实状态：没连着就不必多发一条 disconnect
    if not _refresh().get("connected"):
        return "skip"
    try:
        _run(["disconnect"], timeout=timeout)
    except Exception as e:
        print(f"[WARP] 退出时断开失败: {e}")
        return "failed"
    _CACHE["at"] = 0.0
    _CACHE["data"] = None
    print("[WARP] 终端已退出，WARP 已断开")
    return "disconnected"
