#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：page_image
把网页 / 推文里的图片抓下来并发给用户：QQ 会话里直接发到聊天，
桌面控制台里显示成图片气泡。
底层依赖 browser_session.fetch_images（复用登录态与代理）。
"""
import os
import re

from skills import skill
from tool_registry import tool

skill("page_image", "抓取网页/推文配图并发到当前会话", tools=["page_image"])

SKILLS = ["page_image"]
SKILL_DESCRIPTION = "抓取网页/推文配图并发到当前会话"

_URL_RE = re.compile(r"https?://[^\s，。；、）)\]】\"'<>]+")


@tool(
    name="page_image",
    description=(
        "把网页/推文里的图片抓下来并发给用户：QQ 会话里发到聊天，桌面控制台里显示成图片气泡。"
        "用户说「把这推特的图发我/发到QQ/发群里」时使用。"
        "count 可省略，自己按内容决定（默认 3 张，最多 6 张）。"
    ),
    keywords=["发图", "发张图", "发给我", "把图发我", "配图", "文章里的图", "页面的图"],
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "页面链接（可以是含链接的整句原话）",
            },
            "count": {
                "type": "integer",
                "description": "要几张图，1-6；用户没说就省略，自己决定",
            },
        },
        "required": ["url"],
    },
)
def page_image(url: str, count: int = 3) -> str:
    """抓页面配图并发送到当前会话"""
    m = _URL_RE.search(url or "")
    link = m.group(0) if m else (url or "").strip()
    if not link:
        return "（没看到要打开的链接呢，把网址发给我？）"

    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 6))

    try:
        import browser_session
    except Exception as e:
        return f"（浏览器组件没加载起来：{e}）"

    try:
        res = browser_session.fetch_images(link, count=n)
    except Exception as e:
        return f"（抓图失败了：{e}）"

    if not res.get("ok"):
        err = res.get("error") or "没找到可用的图片"
        if res.get("anonymous"):
            err += "；这次是匿名读取，要登录才能看的图拿不到"
        return f"（{err}）"

    paths = res.get("paths") or []
    try:
        import qq_outbox
        sent = qq_outbox.queue_images(paths)
    except Exception:
        sent = False

    title = (res.get("title") or "").strip()
    head = f"（从「{title}」里扒到 {len(paths)} 张图，" if title else f"（扒到 {len(paths)} 张图，"
    if sent:
        return head + "已经发到当前会话了）"
    names = "、".join(os.path.basename(p) for p in paths[:3])
    return head + f"当前没有可发送的会话，先存本地了：{names}）"
