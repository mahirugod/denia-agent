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
- cloud   ：agnes / OpenAI 兼容网关，工具全开，结果截断 6000 字（够答，又不至于让工具轮把上下文吃满）

用法：
    from harness import pick_harness
    h = pick_harness("qwen2.5:7b")
    h.max_tools          # 8
    h.max_result_chars   # 240
    h.num_ctx            # 1024
"""
from dataclasses import dataclass, field
from typing import Optional


# ── 采样预设：按"这一步在干什么"取温度，而不是每条路径各写一套字面量 ──
# chat     ：首轮。它既可能直接回答，也可能是调工具前的静音轮（后者正文会被丢弃），
#            所以按"闲聊"给温度，别把决策用的低温套到能直接回答的轮次上
# answer   ：拿到工具结果之后的合成轮，一定是要说话 → 正常温度
# proactive：主动搭话 → 最自由，像随口一句
# 只收温度：max_tokens 仍是各调用点的硬预算（工具参数需要空间），不在这里统一砍。
_SAMPLING_STD = {"chat": 0.7, "answer": 0.7, "proactive": 0.8}
_SAMPLING_CLOUD = {"chat": 0.85, "answer": 0.8, "proactive": 0.9}


@dataclass
class Harness:
    """一组对模型友好的约束参数（不直接改模型，只改"喂给模型多少东西"）

    每轮对话的观测口径不在这里：见 metrics.py（一行一轮落 data/metrics.jsonl）。
    """

    name: str
    max_tools: int            # 每轮最多暴露给模型的工具数
    max_result_chars: int     # 工具结果注入 LLM 的截断长度
    num_ctx: int              # Ollama 上下文窗口
    max_steps: int            # Agent 循环最大步数（1 = 单轮，不循环）
    needs_confirmation: bool  # 高危工具是否要用户确认
    history_turns: int = 6   # 上下文窗口条数硬上限（不是主裁剪口径，见 history_chars）
    history_chars: int = 800  # 上下文预算（字符≈token）：按字数从新到旧取，长回复不会挤掉多轮
    sampling: dict = field(default_factory=lambda: dict(_SAMPLING_STD))

    def sampling_for(self, purpose: str = "chat") -> dict:
        """取某个用途的采样参数；未知用途按闲聊，保证调用方永远拿到温度"""
        temp = self.sampling.get(purpose, self.sampling.get("chat", 0.7))
        return {"temperature": float(temp)}


# 三档预设
# standard 放宽到 0（不截断）：新增界面自动化后工具已超过 20 个，
# 若仍截断会把 weather/web_search 这类核心工具挤掉。
_TINY = Harness("tiny", max_tools=5, max_result_chars=160, num_ctx=768,
                max_steps=1, needs_confirmation=True, history_turns=4,
                history_chars=600)
_SMALL = Harness("small", max_tools=9, max_result_chars=240, num_ctx=1024,
                 max_steps=2, needs_confirmation=True, history_turns=8,
                 history_chars=800)
_STANDARD = Harness("standard", max_tools=0, max_result_chars=400, num_ctx=2048,
                    max_steps=3, needs_confirmation=False, history_turns=12,
                    history_chars=1200)

# 云端模型（agnes / OpenAI 兼容网关）：上下文几十万 token，没必要像本地小模型那样
# 把工具结果砍到 400 字——砍掉的部分往往正是模型答得浅的原因。
#
# 但 12000 字也别给：一次网页抓取就是 4~8k token，而工具循环每一轮都要把整段
# 重新发一遍，同一轮里 2 步就是双倍。6000 字（约 3~4k token）足够答"这篇讲了啥"，
# 也让工具轮的上下文不再被网页正文吃掉。
_CLOUD = Harness("cloud", max_tools=0, max_result_chars=6000, num_ctx=2048,
                 max_steps=3, needs_confirmation=False, history_turns=12,
                 history_chars=3000,
                 sampling=dict(_SAMPLING_CLOUD))


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

    # 云端/网关模型不按参数量猜档位：一律给宽结果预算，别把工具返回砍成摘要
    _m = (model_name or "").lower()
    if _m.startswith("agnes") or _m.startswith("cloud:"):
        return _CLOUD

    tag = _detect_size(model_name)
    if tag == "tiny":
        return _TINY
    if tag == "small":
        return _SMALL
    # 未知模型（如 GPT-4o）或没数字：走 standard（工具全开、结果长一点）
    return _STANDARD


def describe(h: Harness) -> str:
    """打印 harness 配置（启动日志用）"""
    tools_desc = "全部" if not h.max_tools else str(h.max_tools)
    return (f"[Harness] {h.name}: 工具≤{tools_desc} 结果≤{h.max_result_chars}字 "
            f"ctx={h.num_ctx} 历史≤{h.history_chars}字/≤{h.history_turns}条 "
            f"步数≤{h.max_steps} 确认={'开' if h.needs_confirmation else '关'}")
