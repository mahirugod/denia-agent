#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：remember
用户说「帮我记住……」时把事实落盘，之后每轮对话都会带上。
"""
import re

from skills import skill
from tool_registry import tool
from memory_facts import add_fact, read_facts

skill("remember", "记住用户明确要求长期记住的事实", tools=["remember"])

SKILLS = ["remember"]
SKILL_DESCRIPTION = "记住用户明确要求长期记住的事实"


def _extract_fact(query: str) -> str:
    """从「帮我记住我明天要开会」里取出「我明天要开会」"""
    text = (query or "").strip()
    text = re.sub(
        r"^(?:请|麻烦)?(?:你)?(?:帮我|给我)?(?:记一下|记住|记下|记得|别忘了)?\s*",
        "",
        text,
    )
    text = re.sub(r"^(?:一下|这件事|这个)\s*[：:，,]?\s*", "", text)
    text = re.sub(r"[。！!？?～~]+$", "", text)
    text = re.sub(r"(?:哦|嘛|啊|呀|吧|呢)$", "", text)
    return text.strip(" ：:，,、")


@tool(
    name="remember",
    description=(
        "把用户明确要求记住的事情长期保存下来（之后每轮对话都会带上）。"
        "只在用户说「记住…」「别忘了…」「以后记住…」时调用。"
    ),
    keywords=["帮我记住", "记住我说", "记住：", "记住:", "以后要记住", "别忘了", "帮我记一下", "记下来"],
)
def remember(query: str) -> str:
    fact = _extract_fact(query)
    if not fact:
        return "（没听清要记什么，让用户再说一遍）"
    add_fact(fact)
    return f"（记住了：{fact}）"
