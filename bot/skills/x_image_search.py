#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：x_image_search
在 X（推特）上按关键词搜索带图推文，把配图抓下来发给用户
（QQ 会话直达聊天，桌面控制台显示成图片气泡）。
底层依赖 browser_session.search_x_images（复用登录态与代理）。
"""
import os
import re

from skills import skill
from tool_registry import tool

skill("x_image_search", "在 X 上按关键词搜图并发到当前会话", tools=["x_image_search"])

SKILLS = ["x_image_search"]
SKILL_DESCRIPTION = "在 X 上按关键词搜图并发到当前会话"


# 数量词逻辑统一在 text_utils（本技能上限 6 张）
from text_utils import COUNT_RE as _COUNT_RE, extract_count as _extract_count_impl

# 口语噪声（长词优先，避免「搜一下」被「搜」先吃掉）
_KW_NOISE = (
    "从帖子正文里", "从帖子正文中", "从推文正文里", "从推文里", "从帖子里",
    "帖子正文", "推文正文", "正文里", "正文中",
    "去推特上", "去推特", "推特上", "推特里", "推特",
    "twitter", "x.com", "X上", "x上",
    "帮我", "给我", "替我", "帮忙",
    "搜一下", "搜一搜", "搜搜", "搜索", "搜",
    "找一下", "找一找", "找几张", "找几幅", "找",
    "扒一下", "扒",
    "来几张", "来几幅", "要几张", "整几张", "弄几张", "来", "要",
    "的图片", "的图", "图片", "照片", "配图", "原图",
    "发给我", "发我", "发过来",
    "看一下", "看看",
)


def _extract_count(text: str, default: int = 3) -> int:
    """从原话里取数量（「3张」「两张」都认），没写就用默认值，最多 6 张"""
    return _extract_count_impl(text, default=default, max_n=6)


def _clean_keyword(raw: str) -> str:
    """把整句口语削成纯搜索词：「从帖子正文里找腿」→「腿」"""
    kw = (raw or "").strip()
    for tok in _KW_NOISE:
        kw = kw.replace(tok, " ")
    kw = re.sub(_COUNT_RE, " ", kw)
    kw = re.sub(r"[，。！？、,.!?~～\s]+", " ", kw).strip()
    kw = re.sub(r"^[的了的]+", "", kw)
    kw = re.sub(r"[吧呢啊呀哦嘛]+$", "", kw)
    return kw.strip()


@tool(
    name="x_image_search",
    description=(
        "在 X（推特）上按关键词搜索带图推文，只取推文正文里的配图，抓图并发给用户"
        "（QQ 会话直达聊天，桌面控制台显示成图片气泡）。"
        "用户点名「推特 / X / 推文 / 帖子正文」找图时用这个；没点名平台就自己判断去哪找，别硬套它。"
        "keyword 只填关键词本身（如「腿」「原神 立绘」），不要把「搜图」「找图」这类指令词带上。"
        "没拿到结果时可能只是加载慢，也可能未登录；需要时用 login_site 打开登录窗口再试。"
        "count 可省略，自己按内容决定（默认 3 张，最多 6 张）。"
    ),
    keywords=["帖子正文", "推文", "推特搜", "推特找", "推特上搜", "推文配图"],
    patterns=[
        r"(?:推特|twitter|x\.com)\s*(?:上|里)?\s*.{0,6}(?:搜|找|扒|要|来)",
        r"(?:去|在)\s*[Xx]\s*(?:上|里)?\s*(?:搜|找|扒)",
        r"(?:搜|找|扒)\s*.{0,6}(?:推特|推文|twitter|x\.com)",
    ],
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "搜索关键词，例如「腿」「原神 立绘」",
            },
            "count": {
                "type": "integer",
                "description": "要几张图，1-6；用户没说就省略，自己决定",
            },
        },
        "required": ["keyword"],
    },
)
def x_image_search(keyword: str, count: int = 3) -> str:
    """搜 X 关键词配图并发送到当前会话"""
    raw = (keyword or "").strip()
    n = _extract_count(raw, default=count if isinstance(count, int) else 3)
    kw = _clean_keyword(raw)
    if not kw:
        return "（要搜什么关键词呢？）"

    try:
        import browser_session
    except Exception as e:
        return f"（浏览器组件没加载起来：{e}）"

    try:
        res = browser_session.search_x_images(kw, count=n)
    except Exception as e:
        return f"（搜图失败了：{e}）"

    if not res.get("ok"):
        err = res.get("error") or "没搜到带图的推文"
        if res.get("login_wall") or "登录" in err:
            return ("（唔……X 那边这次没把图递给我呢。也许是它一时没缓过来，"
                    "也许是我看着太像个路过的陌生人了……等我登一下再试试吧。"
                    "要是还得你帮我点一下登录窗口，我再叫你，好吗？）")
        if res.get("anonymous"):
            return ("（这回我只能当个路过的访客看，X 对陌生人总归冷淡些……"
                    "等我登好了再来吧。）")
        return f"（{err}。……别用网页搜图顶替就好，那些多半不是推文里的。）"

    paths = res.get("paths") or []
    try:
        import qq_outbox
        sent = qq_outbox.queue_images(paths)
    except Exception:
        sent = False

    head = f"（在 X 搜「{kw}」扒到 {len(paths)} 张图，"
    if sent:
        return head + "已经发到当前会话了）"
    names = "、".join(os.path.basename(p) for p in paths[:3])
    return head + f"当前没有可发送的会话，先存本地了：{names}）"
