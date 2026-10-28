#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：bilibili_feed
从 B站官方 API 获取指定 UP 主的中近期投稿动态。
底层依赖 requests 直接调用 B站公开接口（acc/info / arc/search / card）。
"""
import re
import html
import time
import urllib.parse
from skills import skill
from tool_registry import tool

skill("bilibili_feed", "查看 B 站指定 UP 主的中近期投稿动态", tools=["bilibili_feed"])

SKILLS = ["bilibili_feed"]
SKILL_DESCRIPTION = "查看 B 站指定 UP 主的中近期投稿动态"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def _get_json(url: str, referer: str, params: dict = None, timeout: int = 8):
    """GET 请求 B 站接口，返回 JSON dict；失败返回 None。"""
    import requests
    try:
        headers = {
            "User-Agent": _UA,
            "Referer": referer,
        }
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[B站Feed] 请求失败 {url} : {e}")
        return None


def _uid_from_nickname(nickname: str) -> int:
    """通过昵称解析 uid，失败返回 0。"""
    encoded = urllib.parse.quote(nickname)
    data = _get_json(
        "https://api.bilibili.com/x/web-interface/card",
        referer="https://space.bilibili.com/",
        params={"nickname": encoded},
    )
    if not data or data.get("code") != 0:
        print(f"[B站Feed] 昵称解析失败 nickname={nickname} code={data.get('code') if data else 'N/A'}")
        return 0
    card = data.get("data", {}).get("card", {})
    return int(card.get("mid", 0))


def _resolve_uid(query: str) -> int:
    """从自然语言中提取 uid（数字）或昵称（用 @ 或中文昵称）并解析。"""
    # 尝试直接抓 uid 数字（≥4 位，避免误匹配）
    m = re.search(r"(\d{4,10})", query)
    if m:
        uid = int(m.group(1))
        print(f"[B站Feed] 提取到 uid={uid}")
        return uid

    # 尝试 @xxx 或纯中文昵称
    m = re.search(r"[@＠]\s*([\w\u4e00-\u9fff]+)", query)
    if m:
        nickname = m.group(1).strip()
        print(f"[B站Feed] 提取到昵称={nickname}，解析 uid 中…")
        uid = _uid_from_nickname(nickname)
        if uid:
            print(f"[B站Feed] 昵称 {nickname} → uid={uid}")
            return uid

    return 0


def _up_info(uid: int) -> dict:
    """获取 UP 主基本信息，失败返回空 dict。"""
    data = _get_json(
        "https://api.bilibili.com/x/space/acc/info",
        referer=f"https://space.bilibili.com/{uid}/",
        params={"mid": uid},
    )
    if not data or data.get("code") != 0:
        print(f"[B站Feed] acc/info 失败 uid={uid}")
        return {}
    return data.get("data", {})


def _recent_videos(uid: int, limit: int = 10) -> list:
    """获取最近投稿列表，失败返回空 list。"""
    data = _get_json(
        "https://api.bilibili.com/x/space/arc/search",
        referer=f"https://space.bilibili.com/{uid}/video",
        params={"mid": uid, "pn": 1, "ps": limit, "order": "pubdate"},
    )
    if not data or data.get("code") != 0:
        print(f"[B站Feed] arc/search 失败 uid={uid} code={data.get('code') if data else 'N/A'}")
        return []
    vlist = data.get("data", {}).get("vlist", [])
    return vlist[:limit]


def _fmt_date(ts: int) -> str:
    """unix 时间戳 → MM月DD日 HH:MM"""
    if not ts:
        return "?月?日 ??:??"
    t = time.localtime(ts)
    return "%02d月%02d日 %02d:%02d" % (t[1], t[2], t[3], t[4])


@tool(
    name="bilibili_feed",
    description="查看 B 站指定 UP 主的中近期投稿动态",
    keywords=[
        "b站动态", "b站最近", "b站更新", "看看b站",
        "看看up主", "看看 UP", "看看up",
        "b站最新", "up主最新", "up主动态",
        "看看xxx的b站",
    ],
)
def bilibili_feed(query: str) -> str:
    """
    从 query 中提取 UP 主（uid 或昵称），返回其最近 10 条投稿。
    支持格式：
      - "看看xxx的b站" / "b站动态 xxx" / "xxx的up主动态"
      - 含 uid 数字：如 "看看123456的b站"
      - 含 @昵称：如 "看看@xxx的b站"
    """
    uid = _resolve_uid(query)
    if not uid:
        print("[B站Feed] 未能提取到有效 uid 或昵称")
        return "（B站接口有点忙，或者没认出你说的 UP 主呢，换个更具体的说法试试？）"

    info = _up_info(uid)
    nickname = info.get("uname", f"uid={uid}")
    print(f"[B站Feed] UP主：{nickname} (uid={uid})")

    videos = _recent_videos(uid, limit=10)
    if not videos:
        print(f"[B站Feed] uid={uid} 无近期投稿")
        return "（B站接口有点忙，或者这个 UP 主没有近期投稿呢）"

    lines = [f"📺 {nickname} 最近的 {len(videos)} 个投稿："]
    for i, v in enumerate(videos, 1):
        title = v.get("title", "").strip()
        # B 站 title 可能带 HTML 实体
        title = html.unescape(title)
        pubdate = _fmt_date(int(v.get("senddate", 0)))
        play = v.get("play", 0)
        # arc/search 顶层无 favorite；收藏在 stat.favorite，播放量在 stat.view
        stat = v.get("stat", {}) if isinstance(v.get("stat"), dict) else {}
        fav = stat.get("favorite", 0)
        link = v.get("short_link") or f"https://www.bilibili.com/video/{v.get('bvid', '')}"
        lines.append(
            f"  {i}. {pubdate}  《{title}》  播放 {play} 收藏 {fav}  链接：{link}"
        )
    print(f"[B站Feed] 返回 {len(videos)} 条投稿")
    return "\n".join(lines)
