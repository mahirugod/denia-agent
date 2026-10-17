#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文本工具：繁简转换（懒加载 opencc，失败回退原文，不影响主流程）
"""
from typing import Optional

_cc: Optional["object"] = None
_cc_loaded = False


def to_simplified(text: str) -> str:
    """繁转简。opencc 不可用时原样返回（不中断主流程）。"""
    if not text:
        return text
    global _cc, _cc_loaded
    if not _cc_loaded:
        _cc_loaded = True
        try:
            from opencc import OpenCC
            _cc = OpenCC("t2s")
        except Exception:
            _cc = None
    if _cc is None:
        return text
    try:
        return _cc.convert(text)
    except Exception:
        return text


def to_simplified_if_traditional(text: str) -> str:
    """检测文本是否含繁体字，是则转简，否则原样返回（避免无谓计算）。

    仅做轻量判断：取前 40 字扫一遍常见繁体字。
    """
    if not text:
        return text
    sample = text[:40]
    # 常见繁体字样本（覆盖高频字，足够判断）
    _TRAD_MARKERS = set(
        "們說發學時長見問話開關電機語點樣氣東車馬鳥龍風雲書畫圖"
        "讓誰邊過經結細線練網縣質當歲難幫今天天氣"
    )
    if any(ch in sample for ch in _TRAD_MARKERS):
        return to_simplified(text)
    return text
