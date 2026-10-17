#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅主动性监控（active_monitor）
定时巡检 system_sense 输出，发现异常（Ollama 挂了/磁盘偏紧/有待办）时，
通过 callback 主动发声 + 弹窗 + 桌面通知。

设计：
- 调用方（main.py）每 60s 调一次 check_now()
- check_now() 是幂等的：同样的异常不重复报（去重 by key）
- callback(alert: dict) 由调用方决定如何发声（主线程合成 + 弹窗 + 通知）
- 异常 key：
    - ollama_down     Ollama 未启动
    - disk_low        有盘 < 10%
    - has_pending     有 N 个待办提醒
    - recovery        Ollama 恢复了（之前 down 现在 alive）
"""
import os
import socket
import subprocess
from datetime import datetime
from typing import Optional, Callable, List

try:
    from skills.system_sense import _check_ollama, _check_disk, _time_event, _pending_reminders
except Exception:
    # 兜底：手动实现（防止 skills 加载顺序问题）
    def _check_ollama():
        port = 11434
        alive = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(("127.0.0.1", port))
            s.close()
            alive = True
        except Exception:
            alive = False
        return {"alive": alive, "process_running": False}

    def _check_disk():
        import shutil
        out = []
        for drive in ["C:", "D:"]:
            try:
                du = shutil.disk_usage(drive + "\\")
                out.append({"drive": drive, "free_gb": round(du.free / 1024**3, 1),
                            "total_gb": round(du.total / 1024**3, 1),
                            "free_pct": round(du.free / du.total * 100, 1)})
            except Exception:
                pass
        return out

    def _time_event():
        h = datetime.now().hour
        return {"slot": "evening", "greet": "晚上好", "hour": h}

    def _pending_reminders():
        try:
            from skills import get_active_reminders
            return len(get_active_reminders())
        except Exception:
            return 0


# 去重记忆：key → 上次告警时间戳（避免同一异常反复弹）
_ALERT_DEDUP: dict = {}
# Ollama 状态记忆（用于 recovery 检测）
_LAST_OLLAMA_ALIVE: Optional[bool] = None
# 默认异常静默时长（秒）：同一异常 N 秒内不重复报
DEDUP_SECONDS = 300


def _now_ts() -> int:
    return int(datetime.now().timestamp())


def _should_alert(key: str, cooldown: int = DEDUP_SECONDS) -> bool:
    """同一异常在 cooldown 内不重复报"""
    global _ALERT_DEDUP
    last = _ALERT_DEDUP.get(key, 0)
    if _now_ts() - last < cooldown:
        return False
    _ALERT_DEDUP[key] = _now_ts()
    return True


class ActiveMonitor:
    """
    主动性监控器：定期跑 check_now()，发现异常通过 callback 主动发声。

    callback 签名：
        callback(alert: dict) → None
        alert = {"key": "ollama_down", "severity": "warn"|"info"|"ok",
                 "title": "⚠️ Ollama 服务异常", "detail": "...", "ts": "..."}
    """

    def __init__(self, interval_ms: int = 60000, auto_start: bool = True):
        self.interval_ms = interval_ms
        self._timer = None
        self._callback: Optional[Callable] = None
        self._started = False
        self._stop_flag = False
        # Ollama 巡检开关（当前模型是 Ollama 本地模型才打开，云端/ags 模型关闭）
        self._ollama_enabled: bool = True
        if auto_start:
            self.start()

    # ---------- 生命周期 ----------
    def start(self):
        if self._started:
            return
        self._started = True
        self._stop_flag = False
        import threading
        t = threading.Thread(target=self._loop, daemon=True, name="ActiveMonitor")
        self._timer = t
        t.start()

    def stop(self):
        self._stop_flag = True
        self._started = False

    def set_callback(self, cb: Callable):
        self._callback = cb

    def set_ollama_enabled(self, enabled: bool):
        """开关 Ollama 巡检（模型切换时由 main.py 调用）"""
        self._ollama_enabled = enabled
        if not enabled:
            # 关闭时重置状态记忆，避免切回 Ollama 时误报 recovery
            global _LAST_OLLAMA_ALIVE
            _LAST_OLLAMA_ALIVE = None

    # ---------- 核心 ----------
    def check_now(self) -> list:
        """跑一次巡检，返回本次触发的异常列表（去重后）"""
        alerts = []

        # 1. Ollama 状态（仅当 Ollama 巡检开关打开时才执行，云端/ags 模型不巡检本地推理后端）
        if self._ollama_enabled:
            try:
                ol = _check_ollama()
                alive = ol.get("alive", False)
                proc_running = ol.get("process_running", False)
                global _LAST_OLLAMA_ALIVE
                if _LAST_OLLAMA_ALIVE is not None:
                    # 状态翻转检测
                    if not alive and _LAST_OLLAMA_ALIVE:
                        if _should_alert("ollama_down"):
                            alerts.append({
                                "key": "ollama_down", "severity": "warn",
                                "title": "⚠️ Ollama 服务断了",
                                "detail": f"Ollama 进程{'在跑但端口未响应' if proc_running else '未启动'}，"
                                         f"对话会受影响。建议手动 `ollama serve` 拉起。",
                            })
                    elif alive and not _LAST_OLLAMA_ALIVE:
                        if _should_alert("ollama_recovery", cooldown=0):
                            alerts.append({
                                "key": "ollama_recovery", "severity": "ok",
                                "title": "✅ Ollama 已恢复",
                                "detail": "Ollama 服务重新可用，对话正常。",
                            })
                _LAST_OLLAMA_ALIVE = alive
            except Exception as e:
                print(f"[主动性] Ollama 巡检异常: {e}")

        # 2. 磁盘
        try:
            low = [d for d in _check_disk() if d.get("free_pct") is not None and d["free_pct"] < 10]
            if low:
                if _should_alert("disk_low", cooldown=3600):  # 1 小时只报一次
                    lines = "、".join(f"{d['drive']} 剩 {d['free_gb']} GB" for d in low)
                    alerts.append({
                        "key": "disk_low", "severity": "warn",
                        "title": "⚠️ 磁盘空间偏紧",
                        "detail": f"{lines}，注意清理。",
                    })
        except Exception as e:
            print(f"[主动性] 磁盘巡检异常: {e}")

        # 3. 待办提醒
        try:
            n = _pending_reminders()
            if n > 0:
                # 只报数量增加的情况：用 _ALERT_DEDUP 记录上次数量
                last_n = _ALERT_DEDUP.get("_pending_count", 0)
                if n > last_n:
                    _ALERT_DEDUP["_pending_count"] = n
                    if _should_alert("has_pending"):
                        alerts.append({
                            "key": "has_pending", "severity": "info",
                            "title": "📋 你有待办提醒",
                            "detail": f"当前 {n} 个待办提醒未触发，留意一下。",
                        })
                else:
                    _ALERT_DEDUP["_pending_count"] = n
        except Exception as e:
            print(f"[主动性] 待办巡检异常: {e}")

        # 4. 投递
        for a in alerts:
            a["ts"] = datetime.now().strftime("%H:%M:%S")
            if self._callback:
                try:
                    self._callback(a)
                except Exception as e:
                    print(f"[主动性] callback 异常: {e}")
        return alerts

    # ---------- 内部 ----------
    def _loop(self):
        import time as _t
        while not self._stop_flag:
            try:
                self.check_now()
            except Exception as e:
                print(f"[主动性] 巡检循环异常: {e}")
            _t.sleep(self.interval_ms / 1000.0)


# ── 模块级默认实例（供 main.py 直接 import 用）────────────────────
_default_monitor: Optional[ActiveMonitor] = None

def get_default_monitor(interval_ms: int = 60000) -> ActiveMonitor:
    """惰性创建全局巡检实例（main.py 调一次即可）"""
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = ActiveMonitor(interval_ms=interval_ms, auto_start=True)
    return _default_monitor
