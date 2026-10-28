#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：发消息（messaging）
- send_message   通过本机 NapCat 给 QQ 好友/群发消息

依赖用户本机的 NapCat 常驻运行（默认 http://127.0.0.1:3000）且 QQ 已登录。
NapCat 没起来时友好失败，不抛异常、不影响其它能力。
"""
import re

import requests

from skills import skill
from tool_registry import tool

skill("messaging", "通过 NapCat 给 QQ 好友/群发消息", tools=["send_message"])

SKILLS = ["send_message"]
SKILL_DESCRIPTION = "通过 NapCat 给 QQ 好友/群发消息"

try:
    from config_napcat import NAPCAT_HTTP_URL as _NAPCAT_URL
except Exception:
    _NAPCAT_URL = "http://127.0.0.1:3000"

_NOT_ALIVE = "（系统信息：QQ/NapCat 没开着呢，请告诉用户先启动 NapCat 登录 QQ，我才能帮忙发消息）"


def _alive() -> bool:
    try:
        r = requests.get(f"{_NAPCAT_URL}/get_status", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _parse(query: str):
    """从自然语言里抽 (是否群聊, 目标号, 内容)"""
    q = (query or "").strip()
    m = re.search(r"给\s*(\d{5,12})\s*(群)?\s*"
                  r"(?:发|说|留言|转告)[\s：:，,]*[\"“]?([\s\S]+?)[\"”]?$", q)
    if not m:
        return None, None, None
    target = int(m.group(1))
    is_group = bool(m.group(2)) or ("群" in q[: q.find(m.group(1)) + len(m.group(1)) + 2])
    content = m.group(3).strip().strip('"“”')
    return is_group, target, content


@tool(
    name="send_message",
    description="通过本机 QQ 给好友或群发一条消息",
    keywords=["发消息", "发条消息", "给QQ发", "发qq", "转告他", "帮我告诉他"],
)
def send_message(query: str) -> str:
    is_group, target, content = _parse(query)
    if target is None:
        return ("（系统信息：告诉我发给谁、发什么呀，比如「给123456发晚安」。"
                "我用的是 QQ 号，昵称我认不出来哦）")
    if not content:
        return "（系统信息：要说的话是空的呢，请让用户补一句内容）"
    if not _alive():
        return _NOT_ALIVE

    endpoint = "send_group_msg" if is_group else "send_private_msg"
    payload = {"group_id" if is_group else "user_id": target, "message": content}
    try:
        r = requests.post(f"{_NAPCAT_URL}/{endpoint}", json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[工具] send_message 失败: {e}")
        return _NOT_ALIVE

    where = f"群 {target}" if is_group else f"QQ {target}"
    preview = content if len(content) <= 20 else content[:20] + "…"
    return f"（系统信息：已把「{preview}」发给{where}了。请用达妮娅的语气确认）"
