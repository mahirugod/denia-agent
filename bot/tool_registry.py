#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量插件/工具注册表（参考 Shinsekai sdk/tool_registry 的 @tool 装饰器模式）
- @tool 装饰器注册工具（名称/描述/触发关键词）
- match_tool: 按用户消息关键词路由到工具（快路径）
- model_pick_tool: 模型自报工具名的解析（慢路径，配合双通道）
- 工具返回上下文文本，由 LLM 以达妮娅人设转述（不破坏人设）
"""
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict


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


def get_tool(name: str) -> Optional[ToolInfo]:
    """按工具名精确取（慢路径解析后调用）"""
    return _TOOLS.get(name)


_TOOL_CALL_RE = re.compile(
    r"TOOL[:：]\s*(\w+)\s*(?:ARGS[:：]\s*(\{.*\}))?",
    re.IGNORECASE,
)


def model_pick_tool(model_text: str) -> Optional[ToolInfo]:
    """
    双通道「慢路径」：模型在输出里自报工具调用。
    固定格式：TOOL: <工具名> ARGS: {"key":"value"}（小模型容易遵守）
    解析不到合法工具名返回 None（不执行、不中断）。
    支持中文工具名反查（模型可能报"查询天气"而非"query_weather"）。
    """
    if not model_text:
        return None
    m = _TOOL_CALL_RE.search(model_text)
    if not m:
        return None
    name = m.group(1).strip().lower()
    # 1. 精确匹配英文工具名
    info = _TOOLS.get(name)
    if info is not None:
        return info
    # 2. 中文/模糊匹配：按描述关键词反查
    info = _fuzzy_match_tool(name)
    if info is not None:
        print(f"[工具/慢路径] 中文工具名 '{name}' → 映射到 {info.name}")
        return info
    print(f"[工具/慢路径] 模型选了未注册工具: {name}")
    return None


# 中文工具名/描述片段 → 英文工具名 的反查表
_CN_TOOL_MAP = [
    (("天气", "气温", "温度", "下雨", "下雪", "降雨", "降雪", "天气预报"), "query_weather"),
    (("提醒", "定时", "闹钟", "叫我"), "set_reminder"),
    (("几点", "时间", "几号", "星期", "礼拜"), "query_time"),
    (("打开", "启动", "运行", "应用", "程序", "软件"), "open_app"),
    (("屏幕", "网页", "截屏", "看看页面"), "screen_context"),
    (("系统状态", "环境", "看看系统", "磁盘", "Ollama", "还有多少空间"), "system_sense"),
]


def _fuzzy_match_tool(cn_name: str) -> Optional[ToolInfo]:
    """中文工具名/描述片段 → 英文工具名 反查"""
    for keywords, tool_name in _CN_TOOL_MAP:
        for kw in keywords:
            if kw in cn_name:
                return _TOOLS.get(tool_name)
    # 兜底：拿中文片段去匹配工具描述/关键词
    for info in _TOOLS.values():
        if any(kw in info.description for kw in [
            cn_name[:4] if len(cn_name) >= 4 else cn_name
        ]):
            return info
    return None


def tool_manifest(limit: int = 0) -> str:
    """
    生成工具清单（供注入 system prompt，让小模型知道有什么工具可选）。
    limit: 0=全部；>0 时按注册顺序取前 N 个（配合 harness.max_tools）。
    每行一个：TOOL: 名称 —— 描述
    """
    tools = list(_TOOLS.values())
    if limit and limit > 0:
        tools = tools[:limit]
    lines = []
    for t in tools:
        lines.append(f"TOOL: {t.name} —— {t.description}")
    return "\n".join(lines)


def list_tools() -> list:
    """列出所有已注册工具（调试用）"""
    return [f"{t.name}: {t.description}" for t in _TOOLS.values()]


def list_tool_names() -> list:
    """列出所有已注册工具名（按注册顺序，供能力清单注入 system prompt）"""
    return [t.name for t in _TOOLS.values()]
