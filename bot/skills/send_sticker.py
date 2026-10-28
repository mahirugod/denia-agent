#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：send_sticker
给用户发一张达妮娅自己的表情包（本地表情库，不是搜来的网图）。
QQ 会话直达聊天，桌面控制台显示成图片气泡。

表情库是平铺目录：bot/stickers/*.png，不分标签、不传参数；
发不发、什么时候发由模型按语境自己判断，具体哪张随机轮换（避开最近发过的）。
"""
import os
import random

import qq_outbox
from skills import skill
from tool_registry import tool

skill("send_sticker", "发表情包到当前会话", tools=["send_sticker"])

SKILLS = ["send_sticker"]
SKILL_DESCRIPTION = "发表情包到当前会话"

_STICKER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stickers")
_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_recent = []          # 最近发过的图，避免连着发同一张
_RECENT_KEEP = 3


def _all_files() -> list:
    if not os.path.isdir(_STICKER_DIR):
        return []
    return [
        os.path.join(_STICKER_DIR, f)
        for f in sorted(os.listdir(_STICKER_DIR))
        if f.lower().endswith(_EXTS)
        and os.path.isfile(os.path.join(_STICKER_DIR, f))
    ]


@tool(
    name="send_sticker",
    description=(
        "给用户发一张达妮娅自己的表情包（本地表情库里的立绘，不是网图）；"
        "QQ 会话直达聊天，桌面控制台显示成图片气泡。"
        "不需要参数：觉得这时候发张图比说话合适就发（被逗笑、撒娇、无语、犯困、得意…），"
        "一次只发一张，别连着刷。不想发就不要调用。"
    ),
    keywords=["表情包", "发表情", "发个表情"],
    parameters={"type": "object", "properties": {}},
)
def send_sticker() -> str:
    """挑一张表情包并发到当前会话"""
    files = _all_files()
    if not files:
        return "（表情库还是空的，先往 bot/stickers/ 里放几张图吧）"

    global _recent
    pool = [f for f in files if f not in _recent] or files
    path = random.choice(pool)
    _recent = (_recent + [path])[-_RECENT_KEEP:]

    if qq_outbox.queue_images([path]):
        return "（表情已发出）"
    return f"（表情包在 {path}，当前没有可发送的会话）"
