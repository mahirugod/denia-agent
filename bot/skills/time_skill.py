#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：时间（time_skill）
查询当前时间、日期、星期（本地，零网络）。
"""
from datetime import datetime

from skills import skill
from tool_registry import tool

skill("time_skill", "查询当前时间/日期/星期", tools=["query_time"])

SKILLS = ["query_time"]
SKILL_DESCRIPTION = "查询当前时间/日期/星期"


@tool(
    name="query_time",
    description="查询当前时间、日期、星期",
    keywords=["几点", "几点了", "现在几点", "什么时候", "今天几号", "礼拜几", "星期几"],
)
def query_time(query: str) -> str:
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    ctx = (f"（系统信息：现在是{now.year}年{now.month}月{now.day}日 {weekdays[now.weekday()]} "
           f"{now.hour}点{now.minute}分。请用达妮娅的语气自然地说出来）")
    return ctx
