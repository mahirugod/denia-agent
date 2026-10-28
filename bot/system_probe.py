#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统探针：Ollama 存活 / 磁盘 / 时间事件 / 待办提醒。

system_sense（给模型看的系统状态摘要）与 active_monitor（主动巡检告警）
共用同一份实现，避免两边各写一遍导致判定口径漂移。
本模块不依赖 skills / tool_registry，可被任意层安全导入。
"""
import socket
import subprocess
from datetime import datetime


def check_ollama() -> dict:
    """Ollama 后端存活检查：端口 11434 探活 + 进程是否在跑"""
    alive = False
    proc_running = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("127.0.0.1", 11434))
        s.close()
        alive = True
    except Exception:
        alive = False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ollama.exe"],
            capture_output=True, text=True, timeout=5,
        )
        proc_running = "ollama" in r.stdout.lower()
    except Exception:
        proc_running = False
    return {"alive": alive, "process_running": proc_running}


def check_memory() -> dict:
    """内存/CPU 占用（psutil 缺失时返回空 dict，调用方跳过该项）"""
    try:
        import psutil
    except Exception:
        return {}
    try:
        vm = psutil.virtual_memory()
        return {
            "total_gb": round(vm.total / 1024 ** 3, 1),
            "used_gb": round(vm.used / 1024 ** 3, 1),
            "free_gb": round(vm.available / 1024 ** 3, 1),
            "percent": round(vm.percent, 0),
            "cpu_percent": round(psutil.cpu_percent(interval=None), 0),
        }
    except Exception:
        return {}


def check_disk() -> list:
    """C/D 盘剩余空间（GB）"""
    import shutil
    out = []
    for drive in ["C:", "D:"]:
        try:
            du = shutil.disk_usage(drive + "\\")
            out.append({
                "drive": drive,
                "free_gb": round(du.free / 1024**3, 1),
                "total_gb": round(du.total / 1024**3, 1),
                "free_pct": round(du.free / du.total * 100, 1),
            })
        except Exception:
            out.append({"drive": drive, "free_gb": None, "total_gb": None, "free_pct": None})
    return out


def time_event() -> dict:
    """时间事件分段（决定主动问候语）"""
    h = datetime.now().hour
    if 5 <= h < 9:
        slot, greet = "morning", "早上好"
    elif 9 <= h < 14:
        slot, greet = "noon", "上午好" if h < 12 else "中午好"
    elif 14 <= h < 18:
        slot, greet = "afternoon", "下午好"
    elif 18 <= h < 23:
        slot, greet = "evening", "晚上好"
    else:
        slot, greet = "night", "这么晚了"
    return {"slot": slot, "greet": greet, "hour": h}


def pending_reminders() -> int:
    """待触发的提醒数（复用 reminder 技能）"""
    try:
        from skills import get_active_reminders
        return len(get_active_reminders())
    except Exception:
        return 0
