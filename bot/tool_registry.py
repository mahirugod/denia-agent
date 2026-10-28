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
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict

import tool_runner


@dataclass
class ToolInfo:
    name: str
    description: str
    keywords: list = field(default_factory=list)   # 命中任一关键词即触发
    patterns: list = field(default_factory=list)   # 正则（可选，用于提取参数）
    handler: Optional[Callable] = None
    parameters: Optional[dict] = None             # 原生 function calling 的 JSON Schema（可选）


# 全局注册表
_TOOLS: dict = {}

# ── 访客权限门（QQ 群聊/非主人私聊）──
# 本机操作类工具（打开应用、截屏、UI 自动化、文件、剪贴板、音量亮度…）只允许主人触发。
# 采用「默认拒绝」：非主人只能使用下面这份白名单里的纯网络/查询类工具。
_REMOTE_SAFE_TOOLS = frozenset({
    "web_search", "query_weather", "query_time",
    "bilibili_feed", "image_search", "browse_url", "x_image_search",
})
_perm = threading.local()


def set_remote_restricted(enabled: bool):
    """开启/关闭访客限制（线程级，随消息线程设置）。"""
    _perm.restricted = bool(enabled)


def is_remote_restricted() -> bool:
    return bool(getattr(_perm, "restricted", False))


def is_tool_allowed(name: str) -> bool:
    """当前请求上下文是否允许使用该工具。"""
    if not is_remote_restricted():
        return True
    return name in _REMOTE_SAFE_TOOLS


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


def tool(name: str, description: str, keywords: list = None, patterns: list = None,
         parameters: dict = None):
    """工具注册装饰器"""
    def deco(fn: Callable):
        _TOOLS[name] = ToolInfo(
            name=name,
            description=description,
            keywords=keywords or [],
            patterns=patterns or [],
            handler=fn,
            parameters=parameters,
        )
        return fn
    return deco


# 只有明确点名 X/推特/推文/帖子正文时，快路径才直奔 x_image_search；
# 其余「找/搜某种图」不预设来源，交给模型自己判断去哪找。
_X_SEARCH_HINT = re.compile(r"(?:推特|twitter|x\.com|x上|x里|推文|帖子正文)")


def _want_x_images(query: str) -> bool:
    """这句是不是明确要求在 X（推特）上找图"""
    return bool(_X_SEARCH_HINT.search((query or "").lower()))


# 模型原生任务（翻译/解释/改写…）：句首是这类动词时不该抢给工具执行，
# 否则「把这句翻译成英文：今天天气不错」会被 query_weather 的「天气」关键字抢走。
_NATIVE_TASK_RE = re.compile(
    r"^(?:请|麻烦)?(?:帮我|给我)?(?:把)?"
    r"(?:这句|这段|这句话|上面这句|下面这段|这段话)?"
    r"\s*(?:翻译|译成|译一下|解释一下|解释|改写|润色|总结|概括|续写|缩写|扩写|校对|改成)"
)

# 个别工具的关键词太泛，命中后容易误伤：出现这些词就跳过该工具
_TOOL_EXCLUDE = {
    # 「她什么时候上线」问的是角色上线时间，不是现在几点
    "query_time": re.compile(r"(上线|出生|生日|发布|发售|开服|公测|维护|上市|开赛|开始|结束|更新)"),
    # 「帮我记住」是记事情，「帮我写首诗」是让模型创作，都不是写文件；
    # 只有出现明确的落盘线索才交给 write_file
    "write_file": re.compile(r"^(?!.*(?:文件|保存到|保存成|写入|写到|存成|存为|新建|创建|覆盖|txt|md|json))"),
}


def _skip_tool(name: str, query: str) -> bool:
    """该工具在这句话里是否应当跳过（关键词太泛导致的误命中）"""
    pat = _TOOL_EXCLUDE.get(name)
    return bool(pat and pat.search(query or ""))


def _score_tool(info, query: str) -> tuple:
    """
    给单个工具打分，返回 (score, 首个命中位置)。
    正则比关键词更精确；关键词越长越具体（「帮我记住」应压过「帮我记」）；
    命中位置越靠前越可能是句子主意图。
    """
    best, pos = 0, -1
    for pat in info.patterns:
        try:
            m = re.search(pat, query)
        except re.error:
            continue
        if m:
            s = 10 + (5 if m.start() == 0 else 0)
            if s > best:
                best, pos = s, m.start()
    for kw in info.keywords:
        i = query.find(kw)
        if i >= 0:
            s = 3 + len(kw) * 2 + (3 if i == 0 else 0)
            if s > best:
                best, pos = s, i
    return best, pos


# 分差小于这个值就认为两个工具都有道理，交给模型裁决，不猜
_AMBIGUOUS_GAP = 3


def match_tool(query: str) -> Optional[ToolInfo]:
    """按正则/关键词匹配工具（正则更精确，先匹配正则，再匹配关键词）"""
    if not query:
        return None
    # 翻译/总结这类交给模型自己处理，不走工具快路径（模型仍可用 TOOL: 协议主动调）
    if _NATIVE_TASK_RE.search(query):
        print(f"[工具] 模型原生任务，跳过关键词路由: {query[:24]}")
        return None
    if _want_x_images(query):
        info = _TOOLS.get("x_image_search")
        if info and is_tool_allowed(info.name) and not _skip_tool(info.name, query):
            return info

    cands = []
    for info in _TOOLS.values():
        if not is_tool_allowed(info.name) or _skip_tool(info.name, query):
            continue
        score, pos = _score_tool(info, query)
        if score:
            cands.append((score, pos, info))
    if not cands:
        return None
    # 分数高的优先；同分时命中的位置更靠前的优先
    cands.sort(key=lambda x: (-x[0], x[1]))
    if len(cands) > 1 and cands[0][0] - cands[1][0] < _AMBIGUOUS_GAP:
        print(f"[工具] 歧义: {cands[0][2].name}({cands[0][0]}) vs "
              f"{cands[1][2].name}({cands[1][0]}) → 交给模型裁决")
        return None
    return cands[0][2]


def execute_tool(info: ToolInfo, query: str) -> Optional[str]:
    """
    执行工具，返回注入 LLM 的上下文文本。
    工具内部异常不应中断聊天，返回 None 则跳过注入。
    """
    if not is_tool_allowed(info.name):
        print(f"[权限] 访客请求本机工具 {info.name}，已拦截")
        return None
    _emit_event({
        "type": "tool_start",
        "name": info.name,
        "description": info.description,
        "query": query,
    })
    try:
        # 纯查询类工具丢进子进程：卡死/崩溃只影响子进程，不拖累对话
        handled, result, iso_err = tool_runner.try_run(info.name, query)
        if handled and iso_err:
            print(f"[工具] {info.name} 子进程失败: {iso_err}")
            result = None
        elif not handled:
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


def _execute_named(info: ToolInfo, named: dict) -> Optional[str]:
    """参数化工具：按 JSON Schema 的属性名直接传 kwargs。

    老接口只会把整句话塞给 handler(query)，slug/index/name/path 这类结构化
    参数拿不到真值（read_book_chunk、load_skill 都受影响），所以单开一条通道。
    需要子进程隔离的纯查询工具不在此列（带 keyword 的工具仍走老路）。
    """
    if not is_tool_allowed(info.name):
        print(f"[权限] 访客请求本机工具 {info.name}，已拦截")
        return None
    _emit_event({
        "type": "tool_start",
        "name": info.name,
        "description": info.description,
        "query": " ".join(f"{k}={v}" for k, v in named.items()),
    })
    try:
        result = info.handler(**named)
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


def execute_tool_args(info: ToolInfo, args: dict, fallback_query: str = "") -> Optional[str]:
    """
    原生 function calling 入口。
    - schema 声明了结构化参数（slug/index/name/path…）→ 按名字传 kwargs
    - schema 是空对象（无参工具）→ 不带参数调用
    - 其余（query/keyword 这类自然语言单参）→ 压成一句话交给 handler
    """
    args = args if isinstance(args, dict) else {}
    props = (info.parameters or {}).get("properties")
    if isinstance(props, dict):
        if not props:
            return _execute_named(info, {})
        named = {k: args[k] for k in props if k in args}
        if named and not ({"query", "keyword"} & set(props)):
            return _execute_named(info, named)

    q = str(args.get("query") or "").strip()
    if not q:
        _kw = str(args.get("keyword") or "").strip()
        if _kw:
            q = _kw
            _cnt = args.get("count")
            if _cnt:
                q += f" {_cnt}张"
    if not q:
        q = (fallback_query or "").strip()
    return execute_tool(info, q)


# ── 原生 function calling：单参数 query 的说明（不改各 skill 文件）──
_QUERY_DESC = {
    "web_search": "搜索关键词，2-8 个词，保留城市/游戏名/专有名词，去掉“帮我”“看一下”这类废话",
    "browse_url": "要访问的 URL，或包含链接的原话，如“看看 bilibili.com/video/BVxxx”",
    "query_weather": "把用户原话整句填入，如“天津今天天气”",
    "query_time": "把用户原话整句填入，如“现在几点了”",
    "set_reminder": "把时间+事项整句填入，如“30分钟后提醒我喝水”",
    "bilibili_feed": "把用户原话整句填入，如“看看 B站 达妮娅的动态”",
    "system_sense": "把用户原话整句填入，如“看看系统状态”",
    "image_search": "要找的图片关键词，可带数量，如“鸣潮 达妮娅 立绘 3张”",
    "model_detect": "本地模型相关原话，如“看看本地有哪些模型”",
    "open_app": "把用户原话整句填入，如“打开网易云音乐”",
    "screen_context": "把用户原话整句填入，如“看看我屏幕上是什么”",
    "list_dir": "要查看的文件夹，如“下载文件夹”“桌面”",
    "read_file": "要读的文件，如“读一下桌面的 笔记.txt”",
    "write_file": "把“内容 + 目标文件名”整句填入，如“把周末计划写进 待办.txt”",
    "organize_folder": "要整理的文件夹，如“下载文件夹”“桌面”，默认下载文件夹",
    "set_volume": "把用户原话整句填入，如“音量调到30”“静音”",
    "set_brightness": "把用户原话整句填入，如“亮度调到50”",
    "read_clipboard": "把用户原话整句填入，如“读一下我剪贴板”",
    "write_clipboard": "把“要复制的文本”填入，如“把这段话复制到剪贴板：明天九点开会”",
    "send_message": "把“发给谁 + 内容”整句填入，如“给123456发晚安”",
    "ui_inspect": "要查看的窗口名，如“记事本”，留空则列出当前可见窗口",
    "ui_click": "窗口名 + 要点击的控件，如“点击记事本里的 保存”",
    "ui_type": "窗口 + 内容整句，如“在记事本里输入：明天九点开会”",
    "ui_hotkey": "快捷键组合，如“ctrl+s”“alt+tab”",
    "ui_window": "动作 + 窗口名，如“最小化记事本”“切换到浏览器”",
}


def _query_schema(name: str) -> dict:
    """统一单参数 schema：模型只需把自然语言整句塞进 query"""
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": _QUERY_DESC.get(name, "把用户原话整句填入")}
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def build_tool_schemas(limit: int = 0) -> list:
    """
    生成原生 function calling 的 tools 数组。
    limit: 0=全部；>0 时按注册顺序取前 N 个（配合 harness.max_tools）。
    工具自带 parameters 时优先用它，否则用统一单参数 query schema。
    """
    tools = [t for t in _TOOLS.values() if is_tool_allowed(t.name)]
    if limit and limit > 0:
        tools = tools[:limit]
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters or _query_schema(t.name),
            },
        }
        for t in tools
    ]


_TOOL_CALL_RE = re.compile(
    r"TOOL[:：]\s*(\w+)\s*(?:ARGS[:：]\s*(\{.*\}))?",
    re.IGNORECASE,
)


def model_pick_tool(model_text: str):
    """
    双通道「慢路径」：模型在输出里自报工具调用。
    固定格式：TOOL: <工具名> ARGS: {"key":"value"}（小模型容易遵守）
    解析不到合法工具名返回 None（不执行、不中断）。
    支持中文工具名反查（模型可能报"查询天气"而非"query_weather"）。

    返回 (ToolInfo, args_dict)；args_dict 是 ARGS 里的 JSON（解析失败则为 {}）。
    """
    if not model_text:
        return None
    m = _TOOL_CALL_RE.search(model_text)
    if not m:
        return None
    args = {}
    if m.group(2):
        try:
            import json as _json
            _parsed = _json.loads(m.group(2))
            if isinstance(_parsed, dict):
                args = _parsed
        except Exception:
            args = {}
    name = m.group(1).strip().lower()
    # 1. 精确匹配英文工具名
    info = _TOOLS.get(name)
    if info is not None:
        return (info, args) if is_tool_allowed(info.name) else None
    # 2. 中文/模糊匹配：按描述关键词反查
    info = _fuzzy_match_tool(name)
    if info is not None:
        if not is_tool_allowed(info.name):
            return None
        print(f"[工具/慢路径] 中文工具名 '{name}' → 映射到 {info.name}")
        return info, args
    print(f"[工具/慢路径] 模型选了未注册工具: {name}")
    return None


# 中文工具名/描述片段 → 英文工具名 的反查表
_CN_TOOL_MAP = [
    (("天气", "气温", "温度", "下雨", "下雪", "降雨", "降雪", "天气预报"), "query_weather"),
    (("提醒", "定时", "闹钟", "叫我"), "set_reminder"),
    (("几点", "时间", "几号", "星期", "礼拜"), "query_time"),
    (("打开", "启动", "运行", "应用", "程序", "软件"), "open_app"),
    (("屏幕", "网页", "截屏", "看看页面"), "screen_context"),
    (("推特搜图", "推文配图", "帖子正文", "X搜图", "搜推特", "推特找图"), "x_image_search"),
    (("立绘", "头像", "壁纸", "搜图", "找图", "找几张", "下载图", "角色图"), "image_search"),
    (("系统状态", "环境", "看看系统", "磁盘", "Ollama", "还有多少空间"), "system_sense"),
    (("b站", "up主", "UP", "看看xxx的b站"), "bilibili_feed"),
    (("整理", "归类", "文件夹", "目录"), "organize_folder"),
    (("读文件", "看看文件", "打开文件"), "read_file"),
    (("写文件", "记下来", "写进", "记到"), "write_file"),
    (("剪贴板", "复制", "粘贴"), "read_clipboard"),
    (("音量", "静音", "声音"), "set_volume"),
    (("亮度", "屏幕暗", "屏幕亮"), "set_brightness"),
    (("发消息", "发qq", "发条消息", "告诉他"), "send_message"),
    (("控件", "窗口里有什么", "有什么按钮", "界面元素"), "ui_inspect"),
    (("点击", "单击", "点一下"), "ui_click"),
    (("输入框", "打字", "填写"), "ui_type"),
    (("快捷键", "组合键"), "ui_hotkey"),
    (("最小化", "最大化", "切换窗口", "关闭窗口", "激活窗口"), "ui_window"),
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
