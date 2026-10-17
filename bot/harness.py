#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Harness（载具）—— 按模型档位自动选一组"约束参数"。

参考 Open Interpreter 的 minimal harness 思想：给小模型套 compact prompt +
短工具列表 + 短结果截断，避免 3B/7B 模型爆 token 或被长上下文带跑偏。

三档：
- tiny    ：3B 以下，只开 4~6 个高频工具，结果截断 160 字，num_ctx=768
- small   ：7B，开 8~10 个工具，结果截断 240 字，num_ctx=1024
- standard：更大的模型 / 非本地，开全部工具，结果截断 400 字，num_ctx=2048

用法：
    from harness import pick_harness
    h = pick_harness("qwen2.5:7b")
    h.max_tools          # 8
    h.max_result_chars   # 240
    h.num_ctx            # 1024
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Harness:
    """一组对模型友好的约束参数（不直接改模型，只改"喂给模型多少东西"）"""

    name: str
    max_tools: int            # 每轮最多暴露给模型的工具数
    max_result_chars: int     # 工具结果注入 LLM 的截断长度
    num_ctx: int              # Ollama 上下文窗口
    max_steps: int            # Agent 循环最大步数（1 = 单轮，不循环）
    needs_confirmation: bool  # 高危工具是否要用户确认


# 三档预设
_TINY = Harness("tiny", max_tools=5, max_result_chars=160, num_ctx=768,
                max_steps=1, needs_confirmation=True)
_SMALL = Harness("small", max_tools=9, max_result_chars=240, num_ctx=1024,
                 max_steps=2, needs_confirmation=True)
_STANDARD = Harness("standard", max_tools=15, max_result_chars=400, num_ctx=2048,
                    max_steps=3, needs_confirmation=False)


def _detect_size(model_name: str) -> Optional[str]:
    """从模型名识别尺寸档位。识别不了返回 None（走 standard）。"""
    m = model_name.lower()
    # 显式尺寸标签：3b / 1.5b / 1.7b / 0.5b / 7b / 8b / 14b / 32b
    import re
    mtag = re.search(r"(\d+(?:\.\d+)?)([bm])", m)
    if mtag:
        size = float(mtag.group(1))
        unit = mtag.group(2)
        if unit == "b":
            if size <= 3:
                return "tiny"
            if size <= 14:
                return "small"
            return "standard"
        # 1.5g 之类（本地 gemma/gemma 等）
        if unit == "g" and size <= 4:
            return "tiny"
        return "small"
    # 没有数字：按已知模型名启发式
    for key, tag in [
        ("phi-3", "tiny"), ("phi-2", "tiny"), ("llama-3.2", "tiny"),
        ("qwen2.5:1.5", "tiny"), ("qwen2.5:3", "tiny"), ("gemma-2-2b", "tiny"),
        ("gemma-2-4b", "small"), ("qwen2.5:7b", "small"), ("qwen3:8b", "small"),
        ("qwen2.5:14b", "small"), ("llama-3.1-8b", "small"),
        ("qwen2.5:32b", "standard"), ("llama-3.1-70b", "standard"),
    ]:
        if key in m:
            return tag
    return None


def pick_harness(model_name: str, override: str = "") -> Harness:
    """
    根据模型名自动选 harness。

    Args:
        model_name: 模型全名，如 "qwen2.5:7b" / "ollama-qwen3:8b" / "gpt-4o"
        override:   手动覆盖档位（tiny/small/standard），空则自动判

    Returns:
        Harness 实例
    """
    if override in ("tiny", "small", "standard"):
        return {"tiny": _TINY, "small": _SMALL, "standard": _STANDARD}[override]

    tag = _detect_size(model_name)
    if tag == "tiny":
        return _TINY
    if tag == "small":
        return _SMALL
    # 未知模型（如 GPT-4o）或没数字：走 standard（工具全开、结果长一点）
    return _STANDARD


def describe(h: Harness) -> str:
    """打印 harness 配置（启动日志用）"""
    return (f"[Harness] {h.name}: 工具≤{h.max_tools} 结果≤{h.max_result_chars}字 "
            f"ctx={h.num_ctx} 步数≤{h.max_steps} 确认={'开' if h.needs_confirmation else '关'}")
