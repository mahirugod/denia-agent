#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：image_search
搜索并下载图片（角色立绘、头像、壁纸等）。
底层依赖 media_downloader.MediaDownloader.search_bilibili_images + download_image。
"""
import re
from skills import skill
from tool_registry import tool

skill("image_search", "搜索并下载图片（角色立绘/头像/壁纸等）", tools=["image_search"])

SKILLS = ["image_search"]
SKILL_DESCRIPTION = "搜索并下载图片（角色立绘/头像/壁纸等）"


def _extract_query(text: str) -> str:
    """从自然语言中提取搜索关键词（去掉口语前缀）"""
    # 去掉"找/搜/下载/要/帮我/给我"等动词前缀
    m = re.search(
        r"(?:帮我|给我|去|请)?(?:帮我|给我)?(?:找|搜|下载|要|看看|查|发|来|弄)"
        r"(?:一下|几张|一些|一张|张|点)?\s*(.+)",
        text,
    )
    raw = m.group(1).strip() if m else text.strip()
    # 「X的图/X的图片」这类先整体削掉后缀，避免关键词变成「腿图」
    raw = re.sub(r"的(?:图片|照片|相片|图)$", "", raw).strip()
    # 「真人」是筛选条件，不是搜索词：正常图文不会写「真人腿」，带着它反而搜不到
    _stripped = re.sub(r"(?:真人照片|真人的照片|真人图|真人)", "", raw).strip()
    if _stripped:
        raw = _stripped
    # 去掉标点、语气词
    raw = re.sub(r"[？?！!。，,的]", "", raw).strip()
    # 去掉尾部的「图片/照片/相片」
    raw = re.sub(r"(?:图片|照片|相片)$", "", raw).strip()
    return raw


# MediaDownloader 单例（避免每次调用都重建 + 重复 load_index IO）
_dl_instance = None
_dl_lock = None


def _get_downloader():
    global _dl_instance, _dl_lock
    import threading
    if _dl_lock is None:
        _dl_lock = threading.Lock()
    with _dl_lock:
        if _dl_instance is None:
            from media_downloader import MediaDownloader
            _dl_instance = MediaDownloader()
        return _dl_instance


# 数量词逻辑统一在 text_utils（本技能上限 10 张）
from text_utils import COUNT_RE as _COUNT_RE, extract_count as _extract_count_impl


def _extract_count(text: str) -> int:
    """从文本提取数量（阿拉伯数字或中文数字），默认 3，最多 10 张"""
    return _extract_count_impl(text, default=3, max_n=10)


@tool(
    name="image_search",
    description="搜索并下载图片（角色立绘、头像、壁纸等），支持指定数量；找到后直接发给用户（QQ 会话直达聊天，桌面控制台显示成图片气泡）",
    keywords=[
        "立绘", "角色图", "头像", "壁纸", "搜图", "找图",
        "找几张", "找一些", "找一张", "下载图", "搜图片",
        "要图片", "要几张", "立绘图", "角色立绘",
        # 口语说法：「给我发几张猫的图片」「来张壁纸」
        "发几张", "发张", "发图", "来几张", "来张", "来点图", "弄几张", "找点图",
    ],
)
def image_search(query: str) -> str:
    """
    搜索并下载图片。
    query: 搜索关键词（如"鸣潮 达妮娅 立绘"）
    """
    text = query
    count = _extract_count(text)
    # 重新提取关键词（去掉数量词）
    kw_text = re.sub(_COUNT_RE, "", text)
    keyword = _extract_query(kw_text)

    if not keyword:
        return "（没有有效的搜索关键词呢，告诉我要找什么图？）"

    # 真人筛选：用户点名「真人/实拍」，或主题本身就是实拍类（腿、cos、美女…）
    # 复用 X 搜图那套判定，避免把插画、动漫封面当成用户要的图发出去
    from skills.x_image_search import _vision_probe, _face_score, _REAL_HINT, _REAL_TOPICS
    want_real = bool(_REAL_HINT.search(text) or _REAL_TOPICS.search(keyword))

    dl = _get_downloader()
    # 需要筛真人时多抓一批候选：插画会被剔掉，留出余量才凑得够数量
    fetch_n = min(max(count * 4, count), 15) if want_real else count
    results = dl.search_bilibili_images(keyword, max_results=fetch_n)
    if not results:
        # 搜索未命中任何来源（Bing/B站/百度都空）→ 明确失败
        return f"（哪里都没搜到「{keyword}」的图片呢，换个更具体的词试试？）"

    urls = []
    _seen = set()
    for r in results:
        u = r.get("url") or r.get("cover")
        # 同一个 URL 只发一次：搜索源经常把同一张封面重复返回
        if u and u not in _seen:
            _seen.add(u)
            urls.append(u)
        if len(urls) >= fetch_n:
            break

    if want_real and urls:
        from concurrent.futures import ThreadPoolExecutor

        # 先并发落到本地，再让视觉模型判断是不是真人实拍
        with ThreadPoolExecutor(max_workers=4) as ex:
            paths = [p for p in ex.map(lambda u: dl.download_image(u, keyword=keyword), urls) if p]
        with ThreadPoolExecutor(max_workers=3) as ex:
            verdicts = list(ex.map(_vision_probe, paths))
        if any(v is not None for v in verdicts):
            kept = [p for p, v in zip(paths, verdicts)
                    if v is True or (v is None and _face_score(p) > 0)]
            dropped = len(paths) - len(kept)
            if not kept:
                return (f"（搜到 {len(paths)} 张「{keyword}」，但一张像真人实拍的都没有……"
                        "换个更具体的词我再试试？）")
            urls = kept
            print(f"[真人筛选] 保留 {len(kept)} 张，剔掉 {dropped} 张插画/非真人")
        else:
            # 视觉模型不可用：放着不筛，好过把真图也全丢掉
            urls = paths
        urls = urls[:count]

    # QQ 会话里直接把链接交给 NapCat 发（它自己下载），本地不落盘
    try:
        import qq_outbox
        if urls and qq_outbox.queue_images(urls):
            return f"📸 找到「{keyword}」的图片 {len(urls)} 张，已经发到当前会话了～"
    except Exception:
        pass

    # 没有会话出口（QQ 未接线、控制台未打开）时才下载到本地
    downloaded = []
    for url in urls:
        local_path = dl.download_image(url, keyword=keyword)
        if local_path:
            downloaded.append(local_path)

    if not downloaded:
        # 降级：返回 URL 列表
        lines = [f"（找到了 {len(urls)} 张「{keyword}」的图片，但都没下载成功。可以直接打开：）"]
        for url in urls:
            lines.append(f"  {url}")
        return "\n".join(lines)

    import os

    lines = [f"📸 找到「{keyword}」的图片 {len(downloaded)} 张，已下载到本地："]
    for i, p in enumerate(downloaded, 1):
        lines.append(f"  {i}. {os.path.basename(p)}")
    return "\n".join(lines)
