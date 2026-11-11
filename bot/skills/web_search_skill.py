#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：联网搜索（web_search_skill）

模型自选联网通道：只在"正则拿不准要不要搜"时开放，由模型在流式回复里
自报 `TOOL: web_search ARGS: {"query":"关键词"}` 触发（慢路径二段式）。

keywords 故意留空：不走关键词快路径。强信号（天气/新闻/卡池/几点…）的预搜索
仍由 denia_llm 主搜索块负责，避免同一轮重复搜索。
"""
from skills import skill
from tool_registry import tool

skill("web_search_skill", "联网搜索实时信息", tools=["web_search"])

SKILLS = ["web_search"]
SKILL_DESCRIPTION = "联网搜索实时信息"


@tool(
    name="web_search",
    description='联网搜索实时信息：新闻、价格、最新动态、你不确定的事实',
    keywords=[],  # 空 → match_tool 永不命中，只能靠模型自报调用
)
def web_search(query: str) -> str:
    """按 query 联网搜索，返回结果文本供模型第二段回答。

    query 由模型生成的搜索关键词（ARGS.query）；无则退回原始用户输入。
    """
    try:
        from web_search import (
            search_with_entity_fallback,
            sort_by_recency,
            search_bilibili,
            is_game_news_query,
            is_generic_hot_query,
            fetch_hot_board,
            extract_high_value_lines,
        )
    except Exception as e:
        print(f"[工具/联网搜索] 导入失败: {e}")
        return ""

    query = (query or "").strip()
    if not query:
        return ""

    result = None
    try:
        # 泛热点 → 实时热搜榜；游戏资讯 → B站；其余 → 多引擎并发
        if is_generic_hot_query(query):
            result = fetch_hot_board()
        if not result and is_game_news_query(query):
            result = search_bilibili(query)
        if not result:
            result = search_with_entity_fallback(query)
            if result:
                result = sort_by_recency(result)
                result = extract_high_value_lines(result, query, limit=3)
    except Exception as e:
        print(f"[工具/联网搜索] 执行失败: {e}")
        return ""

    if result:
        print(f"[工具/联网搜索] 返回 {len(result)} 字（query={query}）")
    else:
        print(f"[工具/联网搜索] 无结果（query={query}）")
    return result or ""
