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
    """从自然语言里抽 (是否群聊, 目标（QQ号或昵称/群名）, 内容)"""
    q = (query or "").strip()
    m = re.search(r"给\s*(.+?)\s*(群)?\s*"
                  r"(?:发|说|留言|转告)[\s：:，,]*[\"“]?([\s\S]+?)[\"”]?$", q)
    if not m:
        return None, False, None
    token = m.group(1).strip().strip("`'\"“”")
    if not token:
        return None, False, None
    content = m.group(3).strip().strip('"“”「」『』')
    is_group = bool(m.group(2)) or "群" in token
    return is_group, token, content


def _list_api(name: str):
    try:
        r = requests.get(f"{_NAPCAT_URL}/{name}", timeout=5)
        return r.json().get("data") or []
    except Exception:
        return []


def _resolve_target(token: str, is_group: bool):
    """号码直接用；昵称/群名走 NapCat 列表换算，唯一命中才算成功"""
    if token.isdigit() and 5 <= len(token) <= 12:
        return int(token), None
    items = _list_api("get_group_list" if is_group else "get_friend_list")
    if not items:
        return None, "（系统信息：读不到 QQ 列表，请确认 NapCat 已登录）"
    id_key = "group_id" if is_group else "user_id"
    cands = []
    for it in items:
        names = ([it.get("group_name") or ""] if is_group else
                 [it.get("remark") or "", it.get("nickname") or ""])
        if any(n and (token == n or token in n) for n in names):
            cands.append(it)
    if not cands:
        return None, (f"（系统信息：{'群' if is_group else '好友'}列表里没找到「{token}」，"
                      f"请直接给 QQ 号）")
    if len(cands) > 1:
        pool = "、".join(
            (c.get("group_name") or c.get("nickname") or c.get("remark") or "?")
            + f"({c.get(id_key)})" for c in cands[:3])
        return None, f"（系统信息：「{token}」匹配到多个：{pool}，请让用户指定号码）"
    return int(cands[0][id_key]), None


@tool(
    name="send_message",
    description="通过本机 QQ 给好友或群发一条消息（支持昵称/群名，自动换算号码）",
    keywords=["发消息", "发条消息", "给QQ发", "发qq", "转告他", "帮我告诉他",
              "给好友发", "给群发", "跟他说"],
)
def send_message(query: str) -> str:
    is_group, token, content = _parse(query)
    if token is None:
        return ("（系统信息：告诉我发给谁、发什么呀，比如「给小明发晚安」或「给123456发晚安」。"
                "昵称和群名我能帮你换算，重名的话会再问你号码）")
    if not content:
        return "（系统信息：要说的话是空的呢，请让用户补一句内容）"
    if not _alive():
        return _NOT_ALIVE

    target, err = _resolve_target(token, is_group)
    if err:
        return err
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
