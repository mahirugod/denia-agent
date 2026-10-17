#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：屏幕上下文（screen_context）
用户提到看屏幕/网页内容时，提示已截屏（实际截屏由 UI 主线程完成，这里只做拦截标记）。
"""
from skills import skill
from tool_registry import tool

skill("screen_context", "看屏幕/网页内容时提示已截屏", tools=["screen_context"])

SKILLS = ["screen_context"]
SKILL_DESCRIPTION = "看屏幕/网页内容时提示已截屏"


@tool(
    name="screen_context",
    description="用户提到看屏幕/网页内容时提示已截屏（实际截屏由UI层完成）",
    keywords=["屏幕", "网页", "页面上", "桌面上", "看看页面", "看下页面"],
)
def screen_context(query: str) -> str:
    # 截屏在 main.py 主线程完成（Qt 限制），这里只返回提示上下文
    return None  # 由 main.py 截屏流程接管，不在此注入
