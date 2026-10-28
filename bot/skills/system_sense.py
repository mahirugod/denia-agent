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
from datetime import datetime  # noqa: F401  # 保留以兼容旧引用

from skills import skill
from tool_registry import tool
from system_probe import (
    check_disk as _check_disk,
    check_memory as _check_memory,
    check_ollama as _check_ollama,
    pending_reminders as _pending_reminders,
    time_event as _time_event,
)

skill("system_sense", "监控本地推理后端/磁盘/时间事件/待办提醒等系统状态", tools=["system_sense"])

SKILLS = ["system_sense"]
SKILL_DESCRIPTION = "监控本地推理后端/磁盘/时间事件/待办提醒等系统状态"


# ── 监控子项（Ollama/磁盘/时间/待办统一实现在 system_probe）──────────


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


# ── 工具入口 ───────────────────────────────────────────────────────────

@tool(
    name="system_sense",
    description="监控本地推理后端/磁盘/时间事件/待办提醒等系统状态，主动报告环境",
    keywords=["系统状态", "环境", "看看系统", "磁盘", "本地模型", "还有多少空间", "现在几点该做什么",
              # 资源占用类问法
              "内存", "cpu", "CPU", "显卡", "占用", "负载", "还剩多少", "电脑配置", "电脑状态", "卡不卡"],
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

    # 2. 内存 / CPU
    mem = _check_memory()
    if mem:
        parts.append(
            f"内存用了 {mem['used_gb']}/{mem['total_gb']} GB（{mem['percent']}%，还剩 {mem['free_gb']} GB）；"
            f"CPU 占用 {mem['cpu_percent']}%；"
        )

    # 3. 磁盘
    for d in _check_disk():
        if d.get("free_gb") is not None:
            warn = "（空间偏紧，注意清理）" if d["free_pct"] < 10 else ""
            parts.append(f"{d['drive']} 剩 {d['free_gb']} GB（{d['free_pct']}%）{warn}；")

    # 4. 时间事件
    te = _time_event()
    parts.append(f"现在是{te['greet']}时段；")

    # 5. 待办提醒
    n = _pending_reminders()
    if n > 0:
        parts.append(f"你有 {n} 个待办提醒未触发，留意一下；")
    else:
        parts.append(f"当前没有待办提醒；")

    parts.append("请用达妮娅的语气自然地说出来，必要时主动提醒用户）。")
    return "".join(parts)
