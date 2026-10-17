#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：全局感知（system_sense）
达妮娅主动监控系统/环境状态，而不是被动等用户问。
监控项：
  - 本地推理后端是否存活（Ollama 可选，仅当前模型为 Ollama 时报告；其它后端只报「非 Ollama 后端，状态未知」）
  - 磁盘空间（C/D 盘剩余，避免写满）
  - 时间事件（早上/下午/晚上/深夜，主动问候语切换）
  - 待办提醒（未触发的提醒数，主动提醒用户）
"""
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from skills import skill
from tool_registry import tool

skill("system_sense", "监控本地推理后端/磁盘/时间事件/待办提醒等系统状态", tools=["system_sense"])

SKILLS = ["system_sense"]
SKILL_DESCRIPTION = "监控本地推理后端/磁盘/时间事件/待办提醒等系统状态"


# ── 监控子项 ───────────────────────────────────────────────────────────

def _check_ollama() -> dict:
    """本地 Ollama 后端存活检查（探活 + 端口监听），仅当前模型为 Ollama 时有效"""
    port = 11434
    alive = False
    proc_running = False
    try:
        # 1. 尝试连端口（Ollama 正常响应才真 alive）
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("127.0.0.1", port))
        s.close()
        alive = True
    except Exception:
        alive = False
    # 2. 看 Ollama 进程是否在跑（Windows 用 tasklist）
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ollama.exe"],
            capture_output=True, text=True, timeout=5,
        )
        proc_running = "ollama" in r.stdout.lower()
    except Exception:
        proc_running = False
    return {"alive": alive, "process_running": proc_running}


def _is_ollama_backend() -> bool:
    """当前模型是否走 Ollama 本地后端（读 settings.json，与 main.py 判断逻辑一致）"""
    try:
        import json
        from pathlib import Path
        settings_file = Path(__file__).resolve().parent.parent.parent / "settings.json"
        if not settings_file.exists():
            settings_file = Path("settings.json")
        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        model_name = data.get("model_name", "qwen2.5:3b")
        llm_url = data.get("llm_url", "http://127.0.0.1:11434")
        return (
            not model_name.startswith("cloud:")
            and not model_name.startswith("agnes")
            and ("ollama" in llm_url.lower() or "11434" in llm_url)
        )
    except Exception:
        return True


def _check_disk() -> list:
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


def _time_event() -> dict:
    """时间事件分段（决定主动问候语）"""
    h = datetime.now().hour
    if 5 <= h < 9:
        slot = "morning"
        greet = "早上好"
    elif 9 <= h < 12:
        slot = "noon"
        greet = "上午好"
    elif 12 <= h < 14:
        slot = "noon"
        greet = "中午好"
    elif 14 <= h < 18:
        slot = "afternoon"
        greet = "下午好"
    elif 18 <= h < 23:
        slot = "evening"
        greet = "晚上好"
    else:
        slot = "night"
        greet = "这么晚了"
    return {"slot": slot, "greet": greet, "hour": h}


def _pending_reminders() -> int:
    """待触发的提醒数（复用 reminder 技能）"""
    try:
        from skills import get_active_reminders
        return len(get_active_reminders())
    except Exception:
        return 0


# ── 工具入口 ───────────────────────────────────────────────────────────

@tool(
    name="system_sense",
    description="监控本地推理后端/磁盘/时间事件/待办提醒等系统状态，主动报告环境",
    keywords=["系统状态", "环境", "看看系统", "磁盘", "本地模型", "还有多少空间", "现在几点该做什么"],
)
def system_sense(query: str) -> str:
    """
    返回一段「系统状态摘要」文本，供本地小模型以达妮娅语气转述。
    格式：
      - 本地推理后端: Ollama 存活/未启动（非 Ollama 后端则跳过该项）
      - 磁盘: C 盘剩 X GB / D 盘剩 Y GB
      - 时间: 上午/下午/晚上/深夜
      - 待办提醒: N 个
      - 主动问候: 根据时间事件切换
    """
    parts = []
    parts.append(f"（系统状态监控：")

    # 1. 本地推理后端（仅 Ollama 模型才报告 Ollama 状态，其它后端跳过）
    if _is_ollama_backend():
        ol = _check_ollama()
        if ol["alive"]:
            parts.append(f"Ollama 服务存活，本地模型可用；")
        elif ol["process_running"]:
            parts.append(f"Ollama 进程在跑但端口 {11434} 未响应（可能加载中，稍等几秒）；")
        else:
            parts.append(f"Ollama 未启动（建议 `ollama serve` 拉起来）；")

    # 2. 磁盘
    for d in _check_disk():
        if d.get("free_gb") is not None:
            warn = "（空间偏紧，注意清理）" if d["free_pct"] < 10 else ""
            parts.append(f"{d['drive']} 剩 {d['free_gb']} GB（{d['free_pct']}%）{warn}；")

    # 3. 时间事件
    te = _time_event()
    parts.append(f"现在是{te['greet']}时段；")

    # 4. 待办提醒
    n = _pending_reminders()
    if n > 0:
        parts.append(f"你有 {n} 个待办提醒未触发，留意一下；")
    else:
        parts.append(f"当前没有待办提醒；")

    parts.append("请用达妮娅的语气自然地说出来，必要时主动提醒用户）。")
    return "".join(parts)
