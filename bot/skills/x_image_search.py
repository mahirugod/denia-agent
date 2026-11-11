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
from tool_registry import tool, is_remote_restricted

skill("x_image_search", "在 X 上按关键词搜图，可发到当前会话或指定 QQ 群", tools=["x_image_search"])

SKILLS = ["x_image_search"]
SKILL_DESCRIPTION = "在 X 上按关键词搜图，可发到当前会话或指定 QQ 群"


# 数量词逻辑统一在 text_utils（本技能上限 10 张）
from text_utils import COUNT_RE as _COUNT_RE, extract_count as _extract_count_impl

# 口语噪声（长词优先，避免「搜一下」被「搜」先吃掉）
_KW_NOISE = (
    "真人照片", "真人的照片", "真人图", "真人", "真实的", "真实",
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
    """从原话里取数量（「3张」「两张」都认），没写就用默认值，最多 10 张"""
    return _extract_count_impl(text, default=default, max_n=10)


def _clean_keyword(raw: str) -> str:
    """把整句口语削成纯搜索词：「从帖子正文里找腿」→「腿」"""
    kw = (raw or "").strip()
    for tok in _KW_NOISE:
        kw = kw.replace(tok, " ")
    kw = re.sub(_COUNT_RE, " ", kw)
    kw = re.sub(r"[，。！？、,.!?~～\s]+", " ", kw).strip()
    kw = re.sub(r"^[的了的]+", "", kw)
    kw = re.sub(r"[吧呢啊呀哦嘛]+$", "", kw)
    # 中英文交界处补空格，否则「cos真人」会被 X 当成一个词
    kw = re.sub(r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", kw)
    kw = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", kw)
    return re.sub(r"\s+", " ", kw).strip()


# 用户点名要「真人」类图片时，才启用真人筛选（避免把插画当真人）
_REAL_HINT = re.compile(r"真人|自拍|写真|摄影|实拍|coser|cos照", re.I)
# 这些主题默认就是真人实拍，不需要用户额外说「真人」
_REAL_TOPICS = re.compile(r"cosplay|cos|腿部|大腿|小腿|腿|身材|人像|美女|自拍", re.I)


def _face_score(path: str) -> int:
    """本地人脸检测：命中级联器越多越像真人照片，0 表示没检出人脸"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return 0
    try:
        # 用 imdecode 而不是 imread：OpenCV 在 Windows 上读不了含中文的路径
        with open(path, "rb") as f:
            img = cv2.imdecode(np.frombuffer(f.read(), dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return 0
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        min_size = max(48, int(min(h, w) * 0.08))
        hit = 0
        for name in ("haarcascade_frontalface_default.xml",
                     "haarcascade_frontalface_alt2.xml",
                     "haarcascade_profileface.xml"):
            cas = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, name))
            if cas.empty():
                continue
            if len(cas.detectMultiScale(gray, 1.12, 5, minSize=(min_size, min_size))):
                hit += 1
        return hit
    except Exception:
        return 0


def _vision_probe(path: str):
    """视觉模型判别：真人照片（含身体局部/背影/cos 实拍）=True，插画等=False，失败=None"""
    import base64
    import io
    import json

    try:
        import requests
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(os.path.join(root, "settings.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        model = cfg.get("model_name") or "agnes-3.0-flash"
        key = cfg.get("cloud_api_key") or ""
        base = (cfg.get("cloud_base_url") or "").rstrip("/")
    except Exception:
        return None
    if not key or not base:
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是图片审核器，只输出「真人」或「非真人」，不解释。"},
            {"role": "user", "content": [
                {"type": "text", "text": (
                    "这张图是真人拍摄的照片吗？真人任何身体局部（脸、手、腿、背影）"
                    "以及 cosplay 实拍都算「真人」；插画、动漫、二次元、3D 渲染、梗图、"
                    "纯风景、截图、文字图算「非真人」。只回一个词。")},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
        "max_tokens": 8,
        "temperature": 0,
    }
    try:
        r = requests.post(f"{base}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {key}"}, timeout=20)
        if not r.ok:
            return None
        txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None
    if not txt:
        return None
    if txt.startswith("非") or "非真人" in txt:
        return False
    return True if "真人" in txt else None


@tool(
    name="x_image_search",
    description=(
        "在 X（推特）上按关键词搜索带图推文，只取推文正文里的配图，抓图并发给用户。"
        "用户点名「推特 / X / 推文 / 帖子正文」找图时用这个；没点名平台就自己判断去哪找，别硬套它。"
        "keyword 只填关键词本身（如「腿」「原神 立绘」），不要把「搜图」「找图」这类指令词带上。"
        "用户说「真人 / 真人照片 / cos / 摄影」时，keyword 仍然只填主题词（如「cos」「腿」），"
        "绝对不要把「真人」「照片」这类词加进去——正常推文不会这么写，加了反而搜不到东西。"
        "cos、腿这类主题会自动按真人实拍筛选，不用你额外处理。"
        "用户说「发到某个群 / 发到 123456789 这个群」时，把群号填进 group_id，图会直接发到那个群；"
        "没说群就省略 group_id，默认发当前会话。"
        "count 可省略，自己按内容决定（默认 3 张，最多 10 张）。"
        "没拿到结果时可能只是加载慢，也可能未登录；需要时用 login_site 打开登录窗口再试。"
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
                "description": "要几张图，1-10；用户没说就省略，自己决定",
            },
            "group_id": {
                "type": "integer",
                "description": "要把图发到的 QQ 群号；只在用户明确给了群号时填，否则省略",
            },
        },
        "required": ["keyword"],
    },
)
def x_image_search(keyword: str, count: int = 3, group_id: int = 0) -> str:
    """搜 X 关键词配图；group_id 非 0 时直接发到该 QQ 群，否则发当前会话"""
    raw = (keyword or "").strip()
    # 路由层会把群号拼在关键词后面（「腿 10张 群750033328」），这里先摘出来再从搜索词里剥掉
    _m = re.search(r"群\s*(\d{5,})", raw)
    if _m:
        if not group_id:
            group_id = int(_m.group(1))
        raw = raw.replace(_m.group(0), " ")
    n = _extract_count(raw, default=count if isinstance(count, int) else 3)
    kw = _clean_keyword(raw)
    if not kw:
        return "（要搜什么关键词呢？）"
    # cos / 腿这类主题默认按真人筛，不用用户额外说「真人」；关键词里也不加这类词
    _want_real = bool(_REAL_HINT.search(raw or "")) or bool(_REAL_TOPICS.search(kw or ""))

    try:
        import browser_session
    except Exception as e:
        return f"（浏览器组件没加载起来：{e}）"

    # 只有搜 X 图需要 WARP：直连不通时临时连上，搜完若由本次连接则断开
    warp_connected_here = False
    try:
        import warp_control
        if warp_control.maybe_auto_connect() == "connected":
            warp_connected_here = True
    except Exception as e:
        print(f"[X搜图] WARP 按需连接失败: {e}")

    try:
        res = browser_session.search_x_images(kw, count=n)
    except Exception as e:
        return f"（搜图失败了：{e}）"
    finally:
        if warp_connected_here:
            try:
                warp_control.set_enabled(False)
                print("[X搜图] 已断开临时 WARP 隧道")
            except Exception:
                pass

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
    # 真人筛选：视觉模型为准（能认身体局部/背影），本地人脸只在云端调用失败时兜底
    _dropped, _drop_note = 0, ""
    if _want_real and len(paths) > 1:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=3) as ex:
                _verdicts = list(ex.map(_vision_probe, paths))
        except Exception:
            _verdicts = [None] * len(paths)
        if any(v is not None for v in _verdicts):
            _kept = [p for p, v in zip(paths, _verdicts)
                     if v is True or (v is None and _face_score(p) > 0)]
            _dropped = len(paths) - len(_kept)
            if _kept:
                paths = _kept
                _drop_note = f"，剔掉 {_dropped} 张不像真人的" if _dropped else ""
            elif _dropped:
                return (f"（在 X 搜「{kw}」翻了 {_dropped} 张图，"
                        "一张真人的都没挑出来……换个关键词我再试试？）")
    try:
        target_group = int(group_id or 0)
    except (TypeError, ValueError):
        target_group = 0

    if target_group:
        # 往别的群发图属于外发操作，只有主人能用
        if is_remote_restricted():
            return "（往别的群发图这种事，只有主人开口我才做哦。）"
        try:
            from napcat_adapter_ws import send_image_files
            sent_n = send_image_files(target_group, paths, True)
        except Exception as e:
            return f"（图抓到了 {len(paths)} 张，但发群的时候出错了：{e}）"
        if sent_n > 0:
            return f"（在 X 搜「{kw}」扒到 {len(paths)} 张图{_drop_note}，已经发到群 {target_group} 了）"
        return f"（图抓到了 {len(paths)} 张，但发到群 {target_group} 没成功，可能我不在那个群里）"

    try:
        import qq_outbox
        sent = qq_outbox.queue_images(paths)
    except Exception:
        sent = False

    head = f"（在 X 搜「{kw}」扒到 {len(paths)} 张图{_drop_note}，"
    if sent:
        return head + "已经发到当前会话了）"
    names = "、".join(os.path.basename(p) for p in paths[:3])
    return head + f"当前没有可发送的会话，先存本地了：{names}）"
