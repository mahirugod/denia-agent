#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量插件/工具注册表（参考 Shinsekai sdk/tool_registry 的 @tool 装饰器模式）
- @tool 装饰器注册工具（名称/描述/触发关键词）
- match_tool: 按用户消息关键词路由到工具
- 工具返回上下文文本，由 LLM 以达妮娅人设转述（不破坏人设）
"""
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ToolInfo:
    name: str
    description: str
    keywords: list = field(default_factory=list)   # 命中任一关键词即触发
    patterns: list = field(default_factory=list)   # 正则（可选，用于提取参数）
    handler: Optional[Callable] = None


# 全局注册表
_TOOLS: dict = {}

# 工具执行事件回调（UI 协作流监听）：callback(event: dict)
_EVENT_CALLBACK = None


def set_event_callback(callback):
    """注册工具执行事件回调。事件类型：tool_start / tool_finish / tool_fail"""
    global _EVENT_CALLBACK
    _EVENT_CALLBACK = callback


def _emit_event(event: dict):
    if _EVENT_CALLBACK is None:
        return
    try:
        _EVENT_CALLBACK(event)
    except Exception:
        pass


def tool(name: str, description: str, keywords: list = None, patterns: list = None):
    """工具注册装饰器"""
    def deco(fn: Callable):
        _TOOLS[name] = ToolInfo(
            name=name,
            description=description,
            keywords=keywords or [],
            patterns=patterns or [],
            handler=fn,
        )
        return fn
    return deco


def match_tool(query: str) -> Optional[ToolInfo]:
    """按关键词/正则匹配工具（按注册顺序，先注册先匹配）"""
    if not query:
        return None
    for info in _TOOLS.values():
        for kw in info.keywords:
            if kw in query:
                return info
        for pat in info.patterns:
            if re.search(pat, query):
                return info
    return None


def execute_tool(info: ToolInfo, query: str) -> Optional[str]:
    """
    执行工具，返回注入 LLM 的上下文文本。
    工具内部异常不应中断聊天，返回 None 则跳过注入。
    """
    _emit_event({
        "type": "tool_start",
        "name": info.name,
        "description": info.description,
        "query": query,
    })
    try:
        result = info.handler(query)
        _emit_event({
            "type": "tool_finish",
            "name": info.name,
            "description": info.description,
            "result": result or "",
        })
        return result
    except Exception as e:
        print(f"[工具] {info.name} 执行失败: {e}")
        _emit_event({
            "type": "tool_fail",
            "name": info.name,
            "description": info.description,
            "error": str(e),
        })
        return None


def list_tools() -> list:
    """列出所有已注册工具（调试用）"""
    return [f"{t.name}: {t.description}" for t in _TOOLS.values()]
