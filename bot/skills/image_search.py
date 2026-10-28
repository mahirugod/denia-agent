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

    dl = _get_downloader()
    results = dl.search_bilibili_images(keyword, max_results=count)
    if not results:
        # 搜索未命中任何来源（B站/百度都空）→ 明确失败
        return f"（在 B 站和百度都没搜到「{keyword}」的图片呢，换个更具体的词试试？）"

    urls = []
    for r in results[:count]:
        u = r.get("url") or r.get("cover")
        if u:
            urls.append(u)

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
