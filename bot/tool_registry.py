#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量插件/工具注册表（参考 Shinsekai sdk/tool_registry 的 @tool 装饰器模式）
- @tool 装饰器注册工具（名称/描述/触发关键词）
- match_tool: 按用户消息关键词找出可能的工具（只作候选/兜底，最终由模型决定用不用）
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

# 注册表版本号：每注册一个工具就 +1，工具清单/原生 schema 按版本缓存。
# 工具通常在启动时一次性注册完，缓存命中后每轮不用再重新拼字符串和 JSON。
_REG_VERSION = 0
_MANIFEST_CACHE: dict = {}

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
        global _REG_VERSION
        _REG_VERSION += 1
        _MANIFEST_CACHE.clear()
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


def _fail_msg(name: str, err: str) -> str:
    """把工具失败变成模型能读懂的提示：告诉它为什么失败、下一步怎么补。

    以前这里返回 None，模型看到的是"什么都没发生"，于是要么编一个答案，
    要么干脆沉默（就是"脑袋里一片空白"）。现在把原因回灌，让它自己换路。
    """
    err = (err or "").strip().replace("\n", " ")[:140]
    _note_tool(name, failed=True)
    return (f"（系统：{name} 执行失败：{err or '未知原因'}。"
            f"可以换个同类工具或换个关键词/来源再试一次；实在不行就直接告诉用户做不到）")


def _note_tool(name: str, failed: bool = False):
    """给 metrics 打点（失败静默，不让统计影响主流程）"""
    try:
        import metrics
        metrics.note_tool(name, failed)
    except Exception:
        pass


def execute_tool(info: ToolInfo, query: str) -> Optional[str]:
    """
    执行工具，返回注入 LLM 的上下文文本。
    工具内部异常不应中断聊天；失败时返回可读的失败原因，让模型自己换路。
    """
    if not is_tool_allowed(info.name):
        print(f"[权限] 访客请求本机工具 {info.name}，已拦截")
        return f"（系统：当前是访客会话，{info.name} 这类本机操作不可用，直接说明做不到）"
    _note_tool(info.name)
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
            result = _fail_msg(info.name, iso_err)
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
        return _fail_msg(info.name, f"{type(e).__name__}: {e}")


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
        return f"（系统：当前是访客会话，{info.name} 这类本机操作不可用，直接说明做不到）"
    _note_tool(info.name)
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
        return _fail_msg(info.name, f"{type(e).__name__}: {e}")


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
        if not ({"query", "keyword"} & set(props)):
            _bad = _validate_args(info, args)
            if _bad:
                print(f"[工具] {info.name} 参数校验未过，不执行，让模型改参重试")
                return _bad
            # 结构化参数工具：模型一个参数都没给时也要走命名调用，让函数用自己的默认值，
            # 否则会把用户原话整句当成第一个参数（如 slug）传进去
            return _execute_named(info, named)

    q = str(args.get("query") or "").strip()
    if not q:
        _kw = str(args.get("keyword") or "").strip()
        if _kw:
            q = _kw
            _cnt = args.get("count")
            if _cnt:
                q += f" {_cnt}张"
            _gid = args.get("group_id")
            if _gid:
                q += f" 群{_gid}"
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


# ── 参数值域补丁：在不改各 skill 文件的前提下，给模型看到上下界 ──
# 网关没有 DeepInfer 那种解码期约束（so_args 的 regex / json_schema / ebnf
# 是在采样时屏蔽非法 token），我们改不了解码，只能"事前把 schema 写死 +
# 调用前校验"两边逼近。这里补的是模型最容易给错的那几个数值参数。
_PARAM_PATCH = {
    "page_image": {"count": {"minimum": 1, "maximum": 6}},
    "x_image_search": {"count": {"minimum": 1, "maximum": 10}},
    "run_python": {"timeout": {"minimum": 5, "maximum": 60}},
}


def _patched_schema(info) -> dict:
    """工具自带 schema + 值域补丁（只叠加约束，不改结构）"""
    base = dict(info.parameters or {})
    patch = _PARAM_PATCH.get(info.name)
    props = base.get("properties")
    if not patch or not isinstance(props, dict):
        return base
    merged = dict(props)
    for key, extra in patch.items():
        if key in merged and isinstance(merged[key], dict):
            prop = dict(merged[key])
            prop.update(extra)
            merged[key] = prop
    base["properties"] = merged
    return base


def _check_value(key: str, val, spec: dict) -> Optional[str]:
    """单个参数的值域校验，返回给模型读的错误句（None = 通过）"""
    typ = spec.get("type")
    if typ == "integer" and not isinstance(val, int):
        # 模型常把数字写成字符串（"3"），能转就放行，转不了才报错
        try:
            val = int(str(val).strip())
        except (TypeError, ValueError):
            return f"参数 {key} 要是整数，你给的是 {val!r}"
    if typ == "string" and isinstance(val, (int, float)) and not isinstance(val, bool):
        return f"参数 {key} 要是文本，你给的是 {val!r}"
    enum = spec.get("enum")
    if enum and val not in enum:
        return f"参数 {key} 只能是 {'/'.join(str(e) for e in enum)}，你给的是 {val!r}"
    if isinstance(val, int) and not isinstance(val, bool):
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if lo is not None and val < lo:
            return f"参数 {key} 最小是 {lo}，你给的是 {val}"
        if hi is not None and val > hi:
            return f"参数 {key} 最大是 {hi}，你给的是 {val}"
    return None


def _validate_args(info, args: dict) -> Optional[str]:
    """按 schema 校验结构化参数，返回回灌给模型的说明（None = 放行）。

    以前校验没有这一层：必填参数缺失时直接把用户原话顶进去（"把用户原话
    当 slug"就是这么来的），数值越界也不管。现在不合规就不执行，把错因
    告诉模型，让它带着修正重新调一次。
    """
    schema = _patched_schema(info)
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    args = args if isinstance(args, dict) else {}
    required = schema.get("required") or []
    missing = [k for k in required if not str(args.get(k, "")).strip()]
    if missing:
        return (f"（系统：{info.name} 参数不完整，缺少 {', '.join(missing)}。"
                f"按 schema 补齐后重新调用一次）")
    for key, val in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict) or val in (None, ""):
            continue
        err = _check_value(key, val, spec)
        if err:
            return f"（系统：{info.name} 的{err}。改好参数再调用一次）"
    return None


def build_tool_schemas(limit: int = 0) -> list:
    """
    生成原生 function calling 的 tools 数组。
    limit: 0=全部；>0 时按注册顺序取前 N 个（配合 harness.max_tools）。
    工具自带 parameters 时优先用它，否则用统一单参数 query schema。

    description 只保留首句（到第一个句号/分号）：39 个工具全量下发时，
    长描述会白烧几千 token，把首字延迟拉长；首句已含核心用途与触发场景。
    """
    # 权限门是线程级开关（访客/主人看到的工具表不同），必须一起进缓存键
    _key = ("schemas", _REG_VERSION, limit if limit and limit > 0 else 0,
            is_remote_restricted())
    _hit = _MANIFEST_CACHE.get(_key)
    if _hit is not None:
        return _hit
    tools = [t for t in _TOOLS.values() if is_tool_allowed(t.name)]
    if limit and limit > 0:
        tools = tools[:limit]
    out = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": _short_desc(_TOOL_DESC.get(t.name) or t.description),
                "parameters": _patched_schema(t) if t.parameters else _query_schema(t.name),
            },
        }
        for t in tools
    ]
    _MANIFEST_CACHE[_key] = out
    return out


# ── 模型看到的工具说明（覆盖 skill 文件里给人看的版本）──
# 模型只靠这一句判断"该不该用这个工具"，所以统一写成：
# 干什么 + 什么时候用 + 什么情况别用（和谁容易混）。超过 110 字会被截断。
_TOOL_DESC = {
    "web_search": "联网查外部事实：新闻、价格、最新动态、人物/游戏/作品/政策等你拿不准的信息；本地状态（推理耗时、哪个模型）和常识别用它",
    "browse_url": "打开一个具体网址读内容（网页正文、B站视频简介、图片直链）；手上没有具体 URL 时用 web_search",
    "query_weather": "查实时天气（今天/明天、气温、降雨、空气）；问天气就用它，不要拿它去搜网页",
    "query_time": "查当前时间、日期、星期；别凭记忆答“现在几点”",
    "image_search": "按关键词在网上搜真实图片并发给用户（立绘、壁纸、头像、摄影）；要 X/推文配图用 x_image_search，要某个网页里的图用 page_image",
    "x_image_search": "在 X（推特）按关键词搜带图推文并发图；只在用户点名推特/X/推文/帖子正文时用，keyword 不写“真人”",
    "page_image": "抓取用户给的网页或推文链接里的图片并发出去；需要先搜索再发图时用 image_search 或 x_image_search",
    "send_sticker": "发达妮娅自己的表情包（本地立绘库）；网图用 image_search，两者别混",
    "send_message": "用本机 QQ 给指定好友或群发一条文字消息；发图片用 image_search / page_image",
    "open_app": "在本机打开应用、网站或文件；只是聊到某个应用时不要调",
    "list_dir": "看文件夹里有哪些文件；要看文件内容用 read_file",
    "read_file": "读一个文本文件的内容；只想知道有哪些文件用 list_dir",
    "write_file": "新建或追加一个文本文件；只是让“记住某件事”用 remember",
    "organize_folder": "把文件夹里的文件按类型归类到子文件夹",
    "remember": "长期记住用户明确要求记住的事；写文件用 write_file",
    "set_reminder": "定时提醒：X 分钟后，或写明具体时间提醒某件事",
    "set_volume": "调节系统音量：静音、加减、指定百分比",
    "set_brightness": "调节屏幕亮度",
    "read_clipboard": "读取用户剪贴板里的文字",
    "write_clipboard": "把文字写进用户剪贴板",
    "system_sense": "查看本机推理后端、磁盘、提醒等系统状态；问本机情况时用它",
    "model_detect": "列出本地 models 目录里可用的模型",
    "screen_context": "截屏看用户当前屏幕；用户说“看看我的屏幕/这个页面”时用",
    "bilibili_feed": "看 B 站指定 UP 主最近的投稿动态",
    "run_python": "执行一段 Python 代码或现成脚本（计算、验证、跑 Skill 里的脚本）",
    "list_skills": "列出本地已装的技能包（方法库）；先列再 load_skill",
    "load_skill": "读出某个技能包的完整做法并照着执行，不要只把内容念给用户",
    "read_skill_file": "读技能包里的附属文件（SKILL.md 里写“详见 xxx”时用）",
    "login_site": "打开可见窗口让用户手动登录某网站；登录态只存在达妮娅自己的浏览器里，用户在自己浏览器登录不算数",
    "ui_inspect": "查看某个窗口里有哪些可操作控件；点任何东西之前先用它看清再点",
    "ui_click": "点击窗口里的某个控件；点之前先用 ui_inspect 确认控件名",
    "ui_type": "往窗口输入框里打字（支持中文）",
    "ui_hotkey": "发送快捷键组合（如 ctrl+s）",
    "ui_window": "激活、切换、最小化、最大化、关闭窗口",
    "list_books": "看书架（D:\\denia_terminal\\books）上有哪些书；问“有哪些书/能读什么”时用",
    "feed_book": "把书架上的书拆成可分批阅读的块；用户说“读一下 XX/拆一下 XX”时先调它，之后才能 read_book_chunk",
    "read_book_chunk": "读某本书的第 N 块正文；必须先 feed_book，按块号顺序读",
    "continue_reading": "从上次读到的地方接着往下读；用户说“接着读/继续读”时用，不传 slug 自动选在读的那本",
    "note_book_chunk": "读完一块后立刻把要点记成笔记；不调用就等于没读，之后想不起来",
    "read_book_notes": "翻看已整理的读书笔记；用户说“看看笔记/翻翻笔记/回顾一下”时用，不要凭空回忆书里细节",
}


def _short_desc(text: str, limit: int = 110) -> str:
    t = (text or "").strip().replace("\n", " ")
    head = re.split(r"[。]", t)[0].strip()
    return (head or t)[:limit]


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
    _key = ("manifest", _REG_VERSION, limit if limit and limit > 0 else 0)
    _hit = _MANIFEST_CACHE.get(_key)
    if _hit is not None:
        return _hit
    tools = list(_TOOLS.values())
    if limit and limit > 0:
        tools = tools[:limit]
    _out = "\n".join(f"TOOL: {t.name} —— {t.description}" for t in tools)
    _MANIFEST_CACHE[_key] = _out
    return _out


def list_tools() -> list:
    """列出所有已注册工具（调试用）"""
    return [f"{t.name}: {t.description}" for t in _TOOLS.values()]


def list_tool_names() -> list:
    """列出所有已注册工具名（按注册顺序，供能力清单注入 system prompt）"""
    return [t.name for t in _TOOLS.values()]
