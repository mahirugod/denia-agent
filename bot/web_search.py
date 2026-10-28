#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联网搜索模块 - Playwright Edge Bing
支持普通搜索（摘要）和深度爬取（进入页面抓正文）
比 Selenium 更快更稳更轻量

重要：所有 Playwright 操作通过专用浏览器线程执行，
      解决 Playwright sync API 不能跨线程调用的问题（greenlet.error）
"""

import re
import os
import time
import html
import hashlib
import threading
import queue
import urllib.parse
from typing import Optional, List

import requests
from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PWTimeout

# 系统 Edge 路径
_EDGE_PATH = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


def _clean_text(text: str) -> str:
    """清理文本中的特殊 Unicode 字符，避免 GBK 编码错误"""
    if not text:
        return ""
    # 替换特殊空格字符为普通空格
    text = text.replace('\u2002', ' ')  # EN SPACE
    text = text.replace('\u2003', ' ')  # EM SPACE
    text = text.replace('\u2009', ' ')  # THIN SPACE
    text = text.replace('\u00a0', ' ')  # NO-BREAK SPACE
    text = text.replace('\u3000', ' ')  # IDEOGRAPHIC SPACE
    # 移除其他不可见字符
    text = re.sub(r'[\u200b-\u200f\u2028-\u202f\u205f-\u206f]', '', text)
    return text.strip()


_TAG_RE = re.compile(r'<[^>]+>')


def _strip_tags(html: str) -> str:
    """去除 HTML 标签"""
    return _TAG_RE.sub('', html)


def _search_baidu_lite(query: str, timeout: int = 6) -> Optional[str]:
    """百度轻量搜索（备用渠道）：Bing 超时/无结果时的兜底。

    中文天气/本地信息百度命中率比 Bing 高，返回同格式（标题 + 描述）。
    """
    _t = time.time()
    url = "https://www.baidu.com/s"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        resp = requests.get(url, params={"wd": query}, headers=headers, timeout=timeout)
    except Exception as e:
        print(f"[百度搜索] 请求失败: {e}")
        return None
    if resp.status_code != 200:
        print(f"[百度搜索] HTTP {resp.status_code}")
        return None

    html_text = resp.content.decode("utf-8", errors="ignore")
    seen = []
    # 百度结果块：<div class="result ..."> 内含 h3 标题 + c-abstract 摘要
    for block in re.findall(r'<div class="result[^"]*".*?</div>\s*(?:</div>\s*)*?(?:<div class="c-abstract[^"]*"[^>]*>.*?</div>)?', html_text, re.S):
        title = ""
        snippet = ""
        m = re.search(r'<h3[^>]*>.*?>(.*?)</h3>', block, re.S)
        if m:
            title = _clean_text(html.unescape(_strip_tags(m.group(1))))
        m = re.search(r'<div class="c-abstract[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if m:
            snippet = _clean_text(html.unescape(_strip_tags(m.group(1))))
        if not title and not snippet:
            continue
        seen.append((title, snippet))
        if len(seen) >= 5:
            break

    # 兜底：单独匹配 h3 标题
    if not seen:
        for m in re.finditer(r'<h3[^>]*>.*?>(.*?)</h3>', html_text, re.S):
            title = _clean_text(html.unescape(_strip_tags(m.group(1))))
            if title:
                seen.append((title, ""))
            if len(seen) >= 5:
                break

    if not seen:
        print("[百度搜索] 未解析到结果")
        return None

    lines = []
    for title, snippet in seen:
        if title and snippet:
            lines.append((title + "：" + snippet)[:200])
        elif title:
            lines.append(title[:200])
        else:
            lines.append(snippet[:200])

    print(f"[百度搜索] 返回 {len(lines)} 条摘要，耗时 {time.time()-_t:.2f}s")
    return "\n".join(lines)

def search_bing_lite(query: str, timeout: int = 6, fallback: bool = True) -> Optional[str]:
    """轻量搜索：requests 直抓 cn.bing.com，解析摘要（无浏览器，快）

    返回格式化的摘要文本（标题 + 描述）；失败返回 None，由调用方决定是否降级。
    fallback=False 时不自动降级百度（供 search_multi 并发调用，避免重复请求）。
    """
    _t = time.time()
    url = "https://cn.bing.com/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        resp = requests.get(url, params={"q": query, "count": "10"}, headers=headers, timeout=timeout)
    except Exception as e:
        print(f"[轻量搜索] 请求失败: {e}，试试百度")
        return _search_baidu_lite(query, timeout) if fallback else None

    if resp.status_code != 200:
        print(f"[轻量搜索] HTTP {resp.status_code}，试试百度")
        return _search_baidu_lite(query, timeout) if fallback else None

    html_text = resp.content.decode("utf-8", errors="ignore")

    seen = []
    for block in re.findall(r'<li class="b_algo[^"]*".*?</li>', html_text, re.S):
        title = ""
        snippet = ""
        m = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.S)
        if m:
            title = _clean_text(html.unescape(_strip_tags(m.group(1))))
        m = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        if m:
            snippet = _clean_text(html.unescape(_strip_tags(m.group(1))))
        # 去掉 Bing 摘要末尾固定的"…阅读更多"
        snippet = re.sub(r'…?阅读更多$', '', snippet).strip()
        if not title and not snippet:
            continue
        seen.append((title, snippet))
        if len(seen) >= 5:
            break

    if not seen:
        print("[轻量搜索] 未解析到结果（Bing 可能返回验证页/JS 页），试试百度")
        return _search_baidu_lite(query, timeout) if fallback else None

    lines = []
    for title, snippet in seen:
        if title and snippet:
            lines.append(f"{title}：{snippet}"[:200])
        elif title:
            lines.append(title[:200])
        else:
            lines.append(snippet[:200])

    print(f"[轻量搜索] 返回 {len(lines)} 条摘要，耗时 {time.time()-_t:.2f}s")
    return "\n".join(lines)


# ────────────────────────────────────────
# B站登录 cookie（从 settings.json 读取，不硬编码）
# B站 API（搜索/官号动态）要求 SESSDATA cookie，否则 412/-799
# ────────────────────────────────────────
import json as _json
import os as _os

_SETTINGS_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "settings.json")
_bili_cookie_cache = {"val": None, "mtime": 0}


def get_bilibili_cookie() -> Optional[str]:
    """读取 settings.json 的 bilibili_sesdata，拼成 Cookie 请求头。

    返回形如 "SESSDATA=xxx; bili_jct=xxx" 的字符串；未配置则返回 None。
    用 mtime 缓存，cookie 改了不用重启进程。
    """
    try:
        mtime = _os.path.getmtime(_SETTINGS_PATH)
        if _bili_cookie_cache["val"] is not None and _bili_cookie_cache["mtime"] == mtime:
            return _bili_cookie_cache["val"]
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        sesdata = (cfg.get("bilibili_sesdata") or "").strip()
        bili_jct = (cfg.get("bilibili_jct") or "").strip()
        buvid3 = (cfg.get("bilibili_buvid3") or "").strip()
        duid = (cfg.get("bilibili_duid") or "").strip()
        if sesdata:
            cookie = f"SESSDATA={sesdata}"
            if bili_jct:
                cookie += f"; bili_jct={bili_jct}"
            if buvid3:
                cookie += f"; buvid3={buvid3}"
            if duid:
                cookie += f"; DUID={duid}"
            _bili_cookie_cache["val"] = cookie
            _bili_cookie_cache["mtime"] = mtime
            return cookie
        return None
    except Exception as e:
        print(f"[B站cookie] 读取失败: {e}")
        return None


# ────────────────────────────────────────
# B站搜索通道（游戏资讯专用）
# 游戏卡池/版本/活动/抽卡等实时信息，B站命中率远高于通用搜索
# ────────────────────────────────────────

_GAME_KW = [
    "卡池", "抽卡", "池子", "up", "UP", "限定", "复刻",
    "活动", "版本", "更新", "维护", "上线", "公测", "开服",
    "停服", "版本更新", "角色", "武器", "池子", "爆料",
]

_GAME_NAMES = [
    "鸣潮", "原神", "崩坏", "崩铁", "星穹", "绝区零",
    "王者荣耀", "明日方舟", "库洛", "战双", "深空",
]


def is_game_news_query(text: str) -> bool:
    """判断 query 是否为游戏资讯类（游戏名 + 资讯关键词）"""
    if not text:
        return False
    has_game = any(g in text for g in _GAME_NAMES)
    has_news = any(kw in text for kw in _GAME_KW) or bool(re.search(r"\d+\.\d+", text))
    return has_game and has_news


_OFFICIAL_HINTS = ("官方", "公告", "前瞻", "预告", "爆料", "直播", "开发组", "版本更新")
# "库洛" 是厂商名，属于鸣潮/战双本身，不当作"别的游戏"排除
_OTHER_GAME_NAMES = [g for g in _GAME_NAMES if g != "库洛"]


def guess_game_topic(text: str) -> str:
    """从文本里认出游戏名，认不出返回空串"""
    for g in _GAME_NAMES:
        if g in (text or ""):
            return g
    return ""


def rank_official_first(text: str) -> str:
    """含官方/公告/前瞻/爆料的行前置，其余保持原顺序"""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    hot, rest = [], []
    for line in lines:
        (hot if any(h in line for h in _OFFICIAL_HINTS) else rest).append(line)
    return "\n".join(hot + rest)


# 通用查询的"权威来源"信号：官网/政府/机构发布优先采信，其次才看自媒体
_OFFICIAL_TAG = "[官方] "
_OFFICIAL_SOURCE_HINTS = (
    "官网", "官方网站", "官方公告", "官方账号", "官方回应", "官方声明",
    "官方微博", "官微", "官方发布", "正式发布", "发布会", "新闻稿",
    "公告", "公示", "通报", "白皮书", "财报", "年报",
    "政府", "委员会", "管理局", "研究院", "疾控", "气象台", "统计局", "央行",
)


def mark_and_rank_official(text: str) -> str:
    """给含官方/机构信号的摘要行打 [官方] 标记，并把标记行前置"""
    marked = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if not s.startswith(_OFFICIAL_TAG.strip()) and any(h in s for h in _OFFICIAL_SOURCE_HINTS):
            s = _OFFICIAL_TAG + s
        marked.append(s)
    hot = [s for s in marked if s.startswith(_OFFICIAL_TAG.strip())]
    rest = [s for s in marked if not s.startswith(_OFFICIAL_TAG.strip())]
    return "\n".join(hot + rest)


_BOILERPLATE = ("的最新相关信息", "相关搜索", "_百度百科", "汉典", "拼音", "部首", "笔画")

def filter_game_lines(text: str, topic: str) -> str:
    """剔除不是本游戏的搜索结果（相关性过滤 + 去搜索引擎噪声）

    规则：行里必须出现主题游戏名；不能提到别的游戏；不能是搜索页样板文字。
    宁可返回空，也不把无关内容塞给模型。
    """
    if not topic:
        return text or ""
    keep = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or topic not in s:
            continue
        if any(h in s for h in _BOILERPLATE):
            continue
        if any(g in s for g in _OTHER_GAME_NAMES if g != topic):
            continue
        keep.append(s)
    return "\n".join(keep)


def search_bilibili(query: str, timeout: int = 8, order: str = "pubdate") -> Optional[str]:
    """B站搜索（游戏资讯专用通道）

    用 B站 API 搜索，返回视频标题+简介。游戏卡池/版本/活动等信息
    B站有大量实时内容，比通用搜索通稿有用得多。

    注意：B站搜索 API 要求登录 cookie（wbi 签名），无 cookie 裸调会 412/-799。
    412/频控时返回 None，调用方降级到 Bing。
    """
    _t = time.time()
    url = "https://api.bilibili.com/x/web-interface/search/type"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://search.bilibili.com/",
        "Origin": "https://search.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    _cookie = get_bilibili_cookie()
    if _cookie:
        headers["Cookie"] = _cookie
    params = {
        "search_type": "video",
        "keyword": query,
        "order": order,  # pubdate=最新发布（问当前）；totalrank=综合（问下个/前瞻）
        "page": "1",
        "pagesize": "10",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            print(f"[B站搜索] HTTP {resp.status_code}（可能需登录 cookie），降级 Bing")
            return None
        data = resp.json()
        if data.get("code") != 0:
            print(f"[B站搜索] API 错误: code={data.get('code')} msg={data.get('message')}，降级 Bing")
            return None
        results = (data.get("data") or {}).get("result") or []
        if not results:
            print("[B站搜索] 无结果")
            return None
        # 提取 video 字段
        videos = []
        for r in results:
            if isinstance(r, dict) and "title" in r:
                videos.append(r)
        lines = []
        for v in videos[:6]:
            title = html.unescape(re.sub(r'<[^>]+>', '', v.get("title", "")))
            desc = v.get("description", "").strip()
            up = v.get("author", "")
            # 格式化时间
            import datetime
            t_str = ""
            try:
                ts = v.get("pubdate") or v.get("pubdate") or 0
                if ts:
                    t_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:
                pass
            line_parts = []
            if title:
                line_parts.append(title)
            if desc:
                line_parts.append(desc[:80])
            if up:
                line_parts.append(f"@{up}")
            if t_str:
                line_parts.append(t_str)
            if line_parts:
                lines.append(" | ".join(line_parts)[:200])
        if not lines:
            print("[B站搜索] 解析为空")
            return None
        print(f"[B站搜索] 返回 {len(lines)} 条，耗时 {time.time()-_t:.2f}s")
        return "\n".join(lines)
    except Exception as e:
        print(f"[B站搜索] 异常: {e}")
        return None


# ────────────────────────────────────────
# 游戏官方账号 B站动态（权威信息源）
# 卡池/版本/活动公告首选官号投稿，比玩家 UP 主更准确
# ────────────────────────────────────────

_GAME_OFFICIAL_UID = {
    "鸣潮": 1955897084,
    # 后续可扩展：原神、星穹铁道等官号 uid
}


_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]
_WBI_CACHE = {"img": "", "sub": "", "ts": 0.0}


def _get_wbi_keys(headers: dict) -> tuple:
    """取 B站 WBI 签名密钥（nav 接口，需要登录 cookie），10 分钟缓存"""
    if _WBI_CACHE["img"] and time.time() - _WBI_CACHE["ts"] < 600:
        return _WBI_CACHE["img"], _WBI_CACHE["sub"]
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/nav",
                         headers=headers, timeout=4)
        data = (r.json() or {}).get("data") or {}
        wbi = data.get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if img and sub:
            _WBI_CACHE.update({"img": img, "sub": sub, "ts": time.time()})
        return img, sub
    except Exception as e:
        print(f"[官号动态] wbi 密钥获取失败: {e}")
        return "", ""


def _sign_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """给参数加 wts + w_rid（B站 space/arc/search 等接口必需）"""
    mixin = "".join((img_key + sub_key)[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
    p = dict(params, wts=int(time.time()))
    query = urllib.parse.urlencode(sorted(p.items()))
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p


def _dynamic_text(item: dict) -> str:
    """从动态条目里取正文字（图文/专栏/视频/转发各类型字段不同）"""
    md = (item.get("modules") or {}).get("module_dynamic") or {}
    major = md.get("major") or {}
    opus = major.get("opus") or {}
    for cand in (
        ((opus.get("summary") or {}).get("text") or ""),
        opus.get("title") or "",
        (major.get("archive") or {}).get("title") or "",
        ((major.get("article") or {}).get("title") or ""),
        ((md.get("desc") or {}).get("text") or ""),
    ):
        text = html.unescape(re.sub(r"<[^>]+>", "", str(cand))).strip()
        if text:
            return re.sub(r"\s+", " ", text)
    draw_items = ((major.get("draw") or {}).get("items") or [])
    for d in draw_items:
        text = (d.get("description") or "").strip()
        if text:
            return re.sub(r"\s+", " ", html.unescape(text))
    return ""


def _dynamic_date(item: dict) -> str:
    import datetime
    author = (item.get("modules") or {}).get("module_author") or {}
    ts = author.get("pub_ts")
    try:
        if ts:
            return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        pass
    return str(author.get("pub_time") or "?")


def fetch_game_official_feed(query: str, limit: int = 5, timeout: int = 8) -> Optional[str]:
    """抓游戏官方账号 B站最近动态（卡池/版本公告权威源）。

    从 query 识别游戏名 → 取对应官号 uid → 抓最近 N 条动态正文。
    无官号或抓取失败返回 None，调用方应降级到 search_bilibili。
    """
    target_uid = 0
    target_name = ""
    for name, uid in _GAME_OFFICIAL_UID.items():
        if name in (query or ""):
            target_uid, target_name = uid, name
            break
    if not target_uid:
        return None

    _cookie = get_bilibili_cookie()
    if not _cookie:
        print("[官号动态] 未配置 bilibili_sesdata，跳过（降级 B站搜索）")
        return None

    _t = time.time()
    # 视频列表接口 space/arc/search 已被风控封（412/-799），改用动态接口：
    # 无需 wbi 签名，且图文公告（版本/卡池/活动）比视频标题信息量大得多
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": f"https://space.bilibili.com/{target_uid}/dynamic",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": _cookie,
    }
    try:
        resp = requests.get(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            params={"host_mid": target_uid, "features": "itemOpusStyle"},
            headers=headers, timeout=timeout,
        )
        if resp.status_code != 200:
            print(f"[官号动态] HTTP {resp.status_code}")
            return None
        data = resp.json()
        if data.get("code") != 0:
            print(f"[官号动态] API 错误: code={data.get('code')}")
            return None
        items = (data.get("data") or {}).get("items") or []
        picked = []
        for item in items:
            text = _dynamic_text(item)
            if len(text) < 8:
                continue
            picked.append((_dynamic_date(item), text))
            if len(picked) >= limit:
                break
        if not picked:
            print("[官号动态] 无有效动态")
            return None
        lines = [f"《{target_name}》官方 B站账号最近 {len(picked)} 条动态："]
        for i, (date_str, text) in enumerate(picked, 1):
            lines.append(f"  {i}. [{date_str}] {text[:220]}")
        print(f"[官号动态] {target_name} 返回 {len(picked)} 条，耗时 {time.time()-_t:.2f}s")
        return "\n".join(lines)
    except Exception as e:
        print(f"[官号动态] 异常: {e}")
        return None


# ────────────────────────────────────────
# 搜索结果信息抽取（规则层，0 额外延迟）
# 从原始摘要里挑出含数字/日期/百分比/实体的高价值行
# ────────────────────────────────────────

import re as _re

# 高价值信号：含这些的摘要行最可能是有用信息
_HIGH_VALUE_PATTERNS = [
    r'\d+(\.\d+)?\s*%',          # 百分比
    r'\d+\.\d+\s*°?[cC]',        # 温度
    r'\d+\.\d+\s*[¥$€£]',       # 金额
    r'\d+\s*%[^\d]*[¥$€£]',      # 金额百分比
    r'(今天|明天|后天|本周|下周|最新|当前|现在)',  # 时效词
    r'\d+月\d+日',                 # 日期
    r'\d+\s*(元|万|亿|万|倍|倍)',  # 数量
    r'\d+\s*(度|℃)',              # 温度
    r'\d+\s*(小时|分钟|天)',       # 时长
    r'\d+\s*(抽|池|版本|期)',      # 游戏相关
    r'\d+\.\d+版本',              # 版本号
    r'\d+/\d+/\d+',              # 日期（斜杠）
    r'\d{4}-\d{2}-\d{2}',        # 日期（横杠，B站/新闻常见）
]

_HIGH_VALUE_RE = _re.compile('|'.join(_HIGH_VALUE_PATTERNS))

# 结果噪音：词典/百科释义、门户与栏目介绍、旅游攻略
# 这些行看着像结果，但没有任何事实内容，注入后模型只能说"没查到"
_DICT_NOISE_RE = _re.compile(
    r"(汉语文字|字的解释|字的含义|拼音是|拼音为|部首|笔顺|汉语国学|新华字典|康熙字典|"
    r"汉典|词典网|成语大全)"
)
_TRAVEL_NOISE_RE = _re.compile(
    r"(旅游攻略|旅游景点|景点|必去|去哪儿玩|打卡攻略|旅游全攻略|自由行攻略|游玩)"
)
# 新闻类查询里的门户/栏目介绍页：讲的是"这个网站/节目是什么"，不是当天发生了什么
_PORTAL_NOISE_RE = _re.compile(
    r"(权威媒体|新闻网站|节目简介|栏目宗旨|栏目介绍|频道首页|创办于)"
)


# 摘要"串味"清洗：搜索摘要常从文章中间截一段，主语被截掉只剩"他/她/其"，
# 模型会把这个代词安到本条问题的主角身上（实测诺奖问题里把"唯一两获"安给了获奖者）。
_PRONOUN_RE = _re.compile(r"(?:^|[，。；、：\s])(?:他|她|其|此人|这位|该人|上述)")
_LATIN_NAME_RE = _re.compile(r"[A-Za-z]{2,}")
# 榜单/条目碎片（"5．居里夫人…"），主语通常已被截断
_LIST_MARK_RE = _re.compile(r"^\s*(?:\d+[．.、]|[①②③④⑤⑥⑦⑧⑨⑩])")


def _sanitize_snippet(line: str) -> str:
    """去掉摘要里主语不明的句子，避免模型把别人的事实安到答案主体上。"""
    sep = ""
    if "·" in line:
        head, body = line.split("·", 1)
        sep = "·"
    elif "：" in line:
        head, body = line.split("：", 1)
        sep = "："
    else:
        head, body = "", line
    kept = []
    for sent in _re.split(r"(?<=[。！？；])", body):
        if _LIST_MARK_RE.match(sent):
            continue
        # 代词做主语且全句没有西文人名 → 指代对象不可确认，整句丢弃
        if _PRONOUN_RE.search(sent) and not _LATIN_NAME_RE.search(sent):
            continue
        kept.append(sent)
    body = "".join(kept).strip()
    if not body:
        return ""
    return f"{head}{sep}{body}" if sep else body


def extract_high_value_lines(search_result: str, query: str = "", limit: int = 5) -> str:
    """从搜索摘要里挑出含高价值信号（数字/日期/百分比/实体）的行。

    用于"信息提取"——把 5 条摘要浓缩成最相关的 2-3 条，让 LLM 直接挑有用信息。
    规则层 0 延迟，适合天气/汇率/新闻等事实型 query。
    对游戏资讯类（实体不在摘要里）效果差，调用方应判断后降级到 LLM 自行筛选。

    返回浓缩后的摘要文本；若没有高价值行，原样返回。
    """
    if not search_result:
        return ""
    lines = [ln.strip() for ln in search_result.split("\n") if ln.strip()]
    if not lines:
        return ""
    # 噪音过滤：词典/百科释义、门户与栏目介绍、旅游攻略——这些页面没有事实内容，
    # 喂给模型只会换来"没查到具体内容"。用户明确在问相关主题时不过滤。
    _q_low = (query or "").lower()
    _keep_dict = any(k in _q_low for k in ("意思", "读音", "拼音", "怎么读", "含义", "字义", "部首"))
    _keep_travel = any(k in _q_low for k in ("旅游", "景点", "攻略", "好玩", "玩什么", "去哪"))
    # 新闻类查询才过滤门户/栏目介绍与城市百科（改写后可能只剩"天津新闻"，不再含"今天"）
    _news_query = any(k in _q_low for k in ("新闻", "资讯", "热点", "时事", "头条"))
    # 办事/政策类查询：百科与站点介绍页都不是答案，顺手一起滤掉
    _howto_query = any(k in _q_low for k in (
        "规定", "新规", "政策", "通知", "公告", "流程", "办理", "额度", "标准",
        "费用", "材料", "条件", "申报", "缴费", "申请", "要求", "多少",
    ))
    _filtered = []
    for ln in lines:
        # 已标记为官方/机构来源的行不做噪声过滤
        # （政府站标题常写"XX市人民政府门户网站"，会被门户噪声规则误伤）
        if ln.startswith(_OFFICIAL_TAG.strip()):
            _filtered.append(ln)
            continue
        if not _keep_dict and _DICT_NOISE_RE.search(ln):
            continue
        if not _keep_travel and _TRAVEL_NOISE_RE.search(ln):
            continue
        if (_news_query or _howto_query) and _PORTAL_NOISE_RE.search(ln):
            continue
        if (_news_query or _howto_query) and "百度百科" in ln:
            continue
        _filtered.append(ln)
    if _filtered:
        lines = _filtered
    # 去掉主语被截断的句子，再进入打分
    _sanitized = [s for s in (_sanitize_snippet(ln) for ln in lines) if s.strip()]
    if _sanitized:
        lines = _sanitized
    scored = []
    for idx, ln in enumerate(lines):
        score = 0
        # 官方/机构来源更可信，优先进入浓缩结果
        if ln.startswith(_OFFICIAL_TAG.strip()):
            score += 15
        # 匹配高价值模式
        score += len(_HIGH_VALUE_RE.findall(ln)) * 10
        # 与 query 重叠的实体加权（用二元字组，避免贪婪正则把词切成 4 字块）
        if query:
            q_clean = _re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', query)
            q_bigrams = {q_clean[i:i + 2] for i in range(len(q_clean) - 1)}
            overlap = sum(1 for bg in q_bigrams if bg in ln)
            score += overlap * 5
        # 过长的摘要行惩罚（信息密度低）
        if len(ln) > 150:
            score -= 10
        scored.append((score, idx, ln))
    # 按分数选出 top N，并保持分数降序输出：
    # 相关性（与问题实体的重合度）优先，官方来源加分只做同等相关时的加权，
    # 避免「官方但无关」的站点首页新闻挤掉真正相关的那一条
    top = [t for t in sorted(scored, key=lambda x: -x[0])[:limit] if t[0] > 0]
    if top:
        picked = [ln for _, _, ln in sorted(top, key=lambda x: -x[0])]
    else:
        picked = []
    if not picked:
        # 没有高价值行，原样返回
        return search_result
    return "\n".join(picked)
# ────────────────────────────────────────
# 实时热搜榜（泛热点问句专用，方向 A）
# 免 key / 免登录的公开接口，泛问句（"今天有什么新闻""最近有什么热点"）
# 直接返回榜单，避免通用网页搜索给的陈旧摘要
# ────────────────────────────────────────

# 与 denia_llm 的地点限定保持一致的常用城市表
_CITY_LIST = (
    "北京","上海","广州","深圳","天津","重庆","成都","杭州","武汉","西安",
    "南京","苏州","长沙","郑州","青岛","大连","沈阳","哈尔滨","昆明","厦门",
    "福州","济南","合肥","南昌","贵阳","南宁","兰州","太原","石家庄","乌鲁木齐",
    "呼和浩特","银川","西宁","拉萨","海口","三亚","无锡","宁波","温州","佛山",
    "东莞","珠海","中山","惠州","常州","徐州","烟台","潍坊","洛阳","唐山","保定",
)
_HOT_KW = ("新闻", "热点", "热搜", "头条", "要闻", "快讯", "大事")
_HOT_DOMAIN_KW = (
    "科技","科学","体育","财经","经济","娱乐","军事","国际","国内","数码",
    "汽车","游戏","教育","健康","时尚","文化","历史","股票",
)
_HOT_NOISE = (
    "今天","今日","最近","近日","现在","最新","有什么","有啥","一下","一些",
    "多少","哪些","哪些","什么","的","了","么","吗","呢","啊","呀","哦","吧","讲","说说",
)
_HOT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
)


def is_generic_hot_query(text: str) -> bool:
    """泛热点问句判定：含"新闻/热点/热搜…"，但去掉填充词/领域词/城市后无具体实体。

    True → 直接返回实时热搜榜；
    False → 具体话题（"今天的科技新闻"含领域词、"今日天津新闻"含城市）走网页搜索。
    """
    if not text:
        return False
    if not any(k in text for k in _HOT_KW):
        return False
    if any(k in text for k in _HOT_DOMAIN_KW):
        return False
    if any(c in text for c in _CITY_LIST):
        return False
    core = text
    for _w in _HOT_NOISE:
        core = core.replace(_w, "")
    for _w in _HOT_KW:
        core = core.replace(_w, "")
    core = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', core)
    return len(core) < 3


def _fetch_baidu_hot(timeout: int = 8) -> Optional[str]:
    """百度实时热搜榜（data.cards[].content[].content[] → index/word/isTop）"""
    _t = time.time()
    url = "https://top.baidu.com/api/board"
    headers = {"User-Agent": _HOT_UA, "Referer": "https://top.baidu.com/board?tab=realtime"}
    try:
        resp = requests.get(url, params={"platform": "wise", "tab": "realtime"},
                            headers=headers, timeout=timeout)
        if resp.status_code != 200:
            print(f"[百度热搜] HTTP {resp.status_code}")
            return None
        cards = ((resp.json().get("data") or {}).get("cards")) or []
        lines = []
        for card in cards:
            for group in (card.get("content") or []):
                for item in (group.get("content") or []):
                    word = (item.get("word") or "").strip()
                    if not word:
                        continue
                    if item.get("isTop"):
                        lines.append(f"置顶 | {word}")
                        continue
                    idx = item.get("index")
                    label = (item.get("labelTagName") or "").strip()
                    seg = f"{word}"
                    if label:
                        seg += f"（{label}）"
                    lines.append(f"{idx}. {seg}" if idx else seg)
        if not lines:
            print("[百度热搜] 解析为空")
            return None
        print(f"[百度热搜] 返回 {len(lines)} 条，耗时 {time.time()-_t:.2f}s")
        return "\n".join(lines[:20])
    except Exception as e:
        print(f"[百度热搜] 异常: {e}")
        return None


def _fetch_bilibili_hot(timeout: int = 8) -> Optional[str]:
    """B站热搜（data.trending.list[] → keyword/heat_score）"""
    _t = time.time()
    url = "https://api.bilibili.com/x/web-interface/search/square"
    headers = {
        "User-Agent": _HOT_UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        resp = requests.get(url, params={"limit": 20}, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            print(f"[B站热搜] HTTP {resp.status_code}")
            return None
        data = resp.json()
        if data.get("code") != 0:
            print(f"[B站热搜] API 错误: code={data.get('code')}")
            return None
        lst = (((data.get("data") or {}).get("trending") or {}).get("list")) or []
        lines = []
        for i, item in enumerate(lst, 1):
            kw = (item.get("keyword") or item.get("show_name") or "").strip()
            if not kw:
                continue
            heat = item.get("heat_score") or 0
            try:
                heat_s = f"{int(heat) / 10000:.1f}万" if heat else ""
            except Exception:
                heat_s = ""
            lines.append(f"{i}. {kw}" + (f"（热度 {heat_s}）" if heat_s else ""))
        if not lines:
            print("[B站热搜] 解析为空")
            return None
        print(f"[B站热搜] 返回 {len(lines)} 条，耗时 {time.time()-_t:.2f}s")
        return "\n".join(lines[:20])
    except Exception as e:
        print(f"[B站热搜] 异常: {e}")
        return None


def fetch_hot_board(timeout: int = 8) -> Optional[str]:
    """并发抓百度 + B站实时热搜榜，合并成"排名. 关键词（热度）"行。

    全部失败返回 None，调用方应降级到通用网页搜索。
    """
    _t = time.time()
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as _ex:
        _fb = _ex.submit(_fetch_baidu_hot, timeout)
        _fl = _ex.submit(_fetch_bilibili_hot, timeout)
        try:
            baidu = _fb.result(timeout=timeout + 2) or ""
        except Exception:
            baidu = ""
        try:
            bili = _fl.result(timeout=timeout + 2) or ""
        except Exception:
            bili = ""

    if not baidu and not bili:
        print("[热搜榜] 无可用数据源")
        return None
    parts = []
    if baidu:
        parts.append("【百度实时热搜榜】\n" + baidu)
    if bili:
        parts.append("【B站热搜榜】\n" + bili)
    print(f"[热搜榜] 命中 {len([p for p in (baidu, bili) if p])} 个榜单，耗时 {time.time()-_t:.2f}s")
    return "\n\n".join(parts)


# ────────────────────────────────────────
# 结果后处理（方向 B）
# 1) 摘要抽取日期 → 时效排序（把最新放前面，解决"日期参差/旧闻"）
# 2) 多引擎并发合并去重（Bing + 百度，提升召回）
# ────────────────────────────────────────

_DATE_DASH_RE = re.compile(r'(20\d{2})-(\d{2})-(\d{2})')
_DATE_CN_RE = re.compile(r'(\d{2})月(\d{2})[日號]')
_AGO_RE = re.compile(r'(\d+)\s*(天|日|小时|分钟|分|秒)\s*(前|以前|之前)')


def _parse_date(line: str, now=None) -> Optional["datetime.datetime"]:
    """从摘要行抽取日期，返回 datetime；抽不到返回 None。

    支持：2026-10-19 / 10月19日 / X天前 / X小时前 / 昨天 / 今天 / 前天。
    """
    import datetime

    if not line:
        return None
    _now = now or datetime.datetime.now()

    m = _DATE_DASH_RE.search(line)
    if m:
        try:
            return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    if "前天" in line:
        return _now - datetime.timedelta(days=2)
    if "昨天" in line:
        return _now - datetime.timedelta(days=1)
    if "今天" in line or "今日" in line:
        return _now

    m = _AGO_RE.search(line)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in ("天", "日"):
            return _now - datetime.timedelta(days=n)
        if unit == "小时":
            return _now - datetime.timedelta(hours=n)
        if unit in ("分钟", "分"):
            return _now - datetime.timedelta(minutes=n)
        if unit == "秒":
            return _now - datetime.timedelta(seconds=n)

    m = _DATE_CN_RE.search(line)
    if m:
        try:
            _d = datetime.datetime(_now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        # 抽到的"月日"晚于今天 → 只可能是去年的
        if _d > _now + datetime.timedelta(days=2):
            try:
                _d = _d.replace(year=_now.year - 1)
            except ValueError:
                pass
        return _d

    return None


def sort_by_recency(search_result: str, now=None) -> str:
    """按摘要里的日期做时效排序：有日期的按时间倒序靠前，无日期的保持原序垫底。

    无任何日期命中时原样返回（顺序不变），对非新闻类查询无副作用。
    """
    if not search_result:
        return search_result
    lines = [ln.strip() for ln in search_result.split("\n") if ln.strip()]
    if not lines:
        return search_result
    dated, undated = [], []
    for idx, ln in enumerate(lines):
        _dt = _parse_date(ln, now)
        if _dt is None:
            undated.append((idx, ln))
        else:
            dated.append((_dt.timestamp(), idx, ln))
    if not dated:
        return search_result
    dated.sort(key=lambda x: (-x[0], x[1]))
    ordered = [ln for _, _, ln in dated] + [ln for _, ln in undated]
    return "\n".join(ordered)


def _merge_dedup(*chunks) -> Optional[str]:
    """合并多个搜索结果文本并去重（按去标点后的前 20 字作为去重键，保留先出现者）"""
    seen = set()
    out = []
    for chunk in chunks:
        if not chunk:
            continue
        for ln in chunk.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            key = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', ln)[:20]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(ln)
    return "\n".join(out) if out else None


def search_multi(query: str, timeout: int = 6) -> Optional[str]:
    """多引擎并发搜索：Bing + 百度 + 官方定向，合并去重（提升召回）。

    - 查询里没有「官方/官网/公告」字样时，额外并发一路「query 官网 公告」，
      让自媒体说法旁边始终有官方页面可对照
    - 合并后给官方/机构来源打 [官方] 标记并前置，供模型优先采信

    Bing 内部失败时不再自行降级百度（避免重复请求）；全部失败返回 None。
    """
    _t = time.time()
    from concurrent.futures import ThreadPoolExecutor

    _official_q = ""
    if not any(k in query for k in ("官方", "官网", "公告")):
        _official_q = f"{query} 官网 公告"

    with ThreadPoolExecutor(max_workers=3) as _ex:
        _fb = _ex.submit(search_bing_lite, query, timeout, False)
        # 百度常被反爬/超时（实测 Read timed out），单独给短超时：
        # 它只是补充召回源，不该把整体延迟拖到 6s 以上
        _fd = _ex.submit(_search_baidu_lite, query, min(timeout, 3))
        _fo = (_ex.submit(search_bing_lite, _official_q, min(timeout, 4), False)
               if _official_q else None)
        try:
            bing = _fb.result(timeout=timeout + 2) or ""
        except Exception:
            bing = ""
        try:
            baidu = _fd.result(timeout=timeout + 2) or ""
        except Exception:
            baidu = ""
        official = ""
        if _fo is not None:
            try:
                official = _fo.result(timeout=timeout + 2) or ""
            except Exception:
                official = ""

    if not bing and not baidu and not official:
        # 冷启动时首次 DNS/TLS 握手容易让两个引擎同时超时（实测首次必然为空、后续正常），
        # 这里加长超时重试一次，避免直接降级到更慢的 summarize 路径
        _rt = max(timeout + 4, 10)
        print(f"[多引擎搜索] 双引擎均空，冷启动重试（timeout={_rt}s）")
        with ThreadPoolExecutor(max_workers=2) as _ex:
            _fb = _ex.submit(search_bing_lite, query, _rt, False)
            _fd = _ex.submit(_search_baidu_lite, query, _rt)
            try:
                bing = _fb.result(timeout=_rt + 2) or ""
            except Exception:
                bing = ""
            try:
                baidu = _fd.result(timeout=_rt + 2) or ""
            except Exception:
                baidu = ""

    if not bing and not baidu and not official:
        return None
    # 官方定向结果放前面参与去重，再统一打标记 + 前置排序
    merged = _merge_dedup(official, bing, baidu)
    merged = mark_and_rank_official(merged) if merged else None
    _official_n = sum(1 for ln in (merged or "").splitlines() if ln.startswith(_OFFICIAL_TAG.strip()))
    print(f"[多引擎搜索] Bing {len(bing.splitlines()) if bing else 0} 条 / 百度 "
          f"{len(baidu.splitlines()) if baidu else 0} 条 / 官方定向 "
          f"{len(official.splitlines()) if official else 0} 条 → 合并 "
          f"{len(merged.splitlines()) if merged else 0} 条（官方 {_official_n} 条），"
          f"耗时 {time.time()-_t:.2f}s")
    return merged


_search_queue = queue.Queue(maxsize=20)  # 搜索请求队列
_browser_thread = None
_browser_thread_running = False
_browser_thread_lock = threading.Lock()  # 保护 _browser_thread 的创建（防并发重复启动）
_browser = None      # Playwright 浏览器实例（仅在浏览器线程内使用）
_playwright = None   # Playwright 驱动（仅在浏览器线程内使用）


def _close_browser():
    """关闭搜索专用浏览器并释放 Playwright（必须在浏览器线程内调用）"""
    global _browser, _playwright
    try:
        if _browser:
            _browser.close()
            print("[搜索] 空闲超时，已关闭搜索浏览器")
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _browser = None
    _playwright = None


def _browser_worker():
    """专用浏览器线程：所有 Playwright 操作都在这个线程执行"""
    global _browser, _playwright
    _browser = None
    _playwright = None

    print("[搜索] 浏览器专用线程已启动")

    last_task_ts = time.time()
    while _browser_thread_running:
        try:
            # 等待任务，超时1秒后检查是否需要退出
            try:
                task = _search_queue.get(timeout=1)
            except queue.Empty:
                # 空闲 2 分钟就关掉搜索专用浏览器，避免和常驻抓取浏览器同时占内存
                if _browser is not None and time.time() - last_task_ts > 120:
                    _close_browser()
                continue

            last_task_ts = time.time()

            if task is None:
                # 关闭信号
                break

            cmd = task.get("cmd")
            event = task.get("event")
            result_key = task.get("result_key", "result")

            if cmd == "search":
                # 执行搜索
                try:
                    result = _search_impl(task["query"], task.get("deep"))
                    task[result_key] = result
                except Exception as e:
                    print(f"[搜索线程错误] {e}")
                    task[result_key] = None
            elif cmd == "cleanup":
                # 关闭浏览器
                try:
                    if _browser:
                        _browser.close()
                except Exception:
                    pass
                try:
                    if _playwright:
                        _playwright.stop()
                except Exception:
                    pass
                _browser = None
                _playwright = None
                task[result_key] = True

            event.set()

        except Exception as e:
            print(f"[搜索线程] 未知错误: {e}")

    # 线程退出时清理
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    print("[搜索] 浏览器专用线程已退出")


def _ensure_browser_thread():
    """确保浏览器专用线程已启动（加锁防止多线程并发重复启动）"""
    global _browser_thread, _browser_thread_running
    with _browser_thread_lock:
        if _browser_thread is None or not _browser_thread.is_alive():
            _browser_thread_running = True
            _browser_thread = threading.Thread(target=_browser_worker, daemon=True, name="browser-worker")
            _browser_thread.start()


def _submit_task(cmd, **kwargs) -> dict:
    """提交任务到浏览器线程并等待结果"""
    _ensure_browser_thread()
    event = threading.Event()
    task = {"cmd": cmd, "event": event, **kwargs}
    _search_queue.put(task)
    event.wait(timeout=30)  # 最多等30秒
    return task


def _get_browser():
    """获取或复用 Playwright Edge 浏览器实例（必须在浏览器线程内调用）"""
    global _browser, _playwright
    if _browser is not None:
        try:
            if _browser.is_connected():
                return _browser
            else:
                print("[搜索] 浏览器已断开，重新启动")
        except Exception as e:
            print(f"[搜索] 浏览器检查异常: {e}，重新启动")
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None

    try:
        if _playwright is None:
            _playwright = sync_playwright().start()

        launch_args = [
            '--no-sandbox',
            '--disable-blink-features=AutomationControlled',
            '--window-size=1280,800',
        ]

        if os.path.exists(_EDGE_PATH):
            print(f"[搜索] 使用系统 Edge: {_EDGE_PATH}")
            _browser = _playwright.chromium.launch(
                executable_path=_EDGE_PATH,
                headless=True,
                args=launch_args,
            )
        else:
            print("[搜索] 未找到系统 Edge，使用 Playwright Chromium")
            _browser = _playwright.chromium.launch(
                headless=True,
                args=launch_args,
            )
        print("[搜索] Edge 浏览器启动成功（无界面模式）")
        return _browser
    except Exception as e:
        print(f"[搜索] Playwright 启动失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def _new_page():
    """创建一个新的浏览器页面（带反检测配置）"""
    browser = _get_browser()
    if not browser:
        return None
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
        viewport={'width': 1280, 'height': 800},
        locale='zh-CN',
        timezone_id='Asia/Shanghai',
        java_script_enabled=True,
        ignore_https_errors=True,
    )
    # 阻止图片加载加速搜索
    ctx.route('**/*.{png,jpg,jpeg,gif,svg,webp,ico}', lambda route: route.abort())
    page = ctx.new_page()
    # 反检测：覆盖 navigator.webdriver 等自动化标志
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });"
        "window.chrome = { runtime: {} };"
    )
    page.set_default_timeout(8000)
    return page


# ────────────────────────────────────────
# 搜索判断相关常量
# ────────────────────────────────────────

# 不适合深度爬取的域名（视频/社交平台，正文没有文字攻略）
_SKIP_DOMAINS = [
    "bilibili.com", "youtube.com", "douyin.com", "weibo.com",
    "twitter.com", "tiktok.com", "zhihu.com/video",
]

# 优先深度爬取的域名
_PREFERRED_DOMAINS = [
    "wiki.biligame.com", "game8.co.jp", "prydwen.gg",
    "nga.178.com", "tieba.baidu.com", "bbs.mihoyo.com",
    "kurogames.com", "wuthering-waves.fandom.com",
]


def _should_deep_crawl(query: str) -> bool:
    """根据查询词判断是否需要深度爬取正文（默认都深度爬取）"""
    if len(query) < 3:
        return False
    if re.match(r'^[\d\s\W]+$', query):
        return False
    return True


def _is_crawlable_url(url: str) -> bool:
    """判断URL是否值得深度爬取"""
    if not url:
        return False
    for domain in _SKIP_DOMAINS:
        if domain in url:
            return False
    return True


def _score_url(url: str) -> int:
    """优先级打分，优先域名得分高"""
    for i, domain in enumerate(_PREFERRED_DOMAINS):
        if domain in url:
            return 10 - i
    return 0


# 主题相关性判断时忽略的泛用词（「版本/更新/内容」这类不说明主题）
_TOPIC_STOP = {
    "最新", "更新", "版本", "内容", "消息", "新闻", "资讯", "什么", "怎么", "如何",
    "现在", "最近", "今天", "详情", "公告", "活动", "介绍", "相关", "大全", "攻略",
}


def _topic_terms(query: str):
    """把查询拆成实词，用于判断抓到的页面是不是跑题"""
    terms = []
    for t in re.split(r"[\s,，、/\\|+]+", query or ""):
        t = t.strip("　:：？！?。.~～")
        if len(t) < 2 or t in _TOPIC_STOP:
            continue
        terms.append(t)
    return terms


def _topic_ok(text: str, query: str, extra: str = "") -> bool:
    """正文/标题/URL 至少出现一个查询实词，否则判定跑题（如「鸣潮」被抓成汉字「鸣」）"""
    terms = _topic_terms(query)
    if not terms:
        return True
    low = f"{text}\n{extra}".lower()
    return any(t.lower() in low for t in terms)


def crawl_page(url: str, timeout: int = 8000, query: str = "") -> Optional[str]:
    """进入指定 URL，提取正文内容（统一走常驻持久化浏览器，复用登录态）"""
    print(f"[深度爬取] 进入: {url[:60]}...")
    try:
        import browser_session
    except Exception as e:
        print(f"[深度爬取] 持久化浏览器不可用: {e}")
        return None

    try:
        # 常驻会话要复用上下文，给足导航时间（旧实现 8s 是为了单页 goto）
        res = browser_session.fetch(url, timeout=max(int(timeout), 15000))
    except Exception as e:
        print(f"[深度爬取] 失败: {e}")
        return None

    if not res.get("ok") or not res.get("text"):
        print(f"[深度爬取] 未取到正文: {res.get('error') or '空页面'}")
        return None

    noise_words = ["广告", "copyright", "©", "隐私政策", "用户协议",
                   "登录", "注册", "下载", "APP", "关注我们"]

    lines = []
    for raw in res["text"].splitlines():
        t = _clean_text(raw)
        if not t or len(t) < 8:
            continue
        if any(n in t.lower() for n in noise_words):
            continue
        lines.append(t)

    if not lines:
        print("[深度爬取] 未找到有效正文")
        return None

    result = re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()

    # 栏目索引页（论坛版块列表、分类导航）：行多但每行都很短，没有可读内容
    line_list = result.splitlines()
    if len(line_list) >= 15 and sum(len(x) for x in line_list) / len(line_list) < 18:
        print(f"[深度爬取] 疑似栏目索引页（{len(line_list)} 行，平均 {sum(len(x) for x in line_list) / len(line_list):.0f} 字），跳过")
        return None

    # 整页跑题（比如把「鸣潮」抓成汉字「鸣」的百科页）直接判废，换下一个候选
    haystack_extra = f"{res.get('title', '')}\n{url}"
    if not _topic_ok(result, query, haystack_extra):
        print(f"[深度爬取] 页面与查询无关，跳过（{len(result)} 字）")
        return None

    print(f"[深度爬取] 提取 {len(result)} 字")
    return result[:600]


def should_search(text: str) -> bool:
    """是否应该搜索"""
    return True


def _search_impl(query: str, deep: bool = None, retry_count: int = 0) -> Optional[str]:
    """
    搜索实现（在浏览器线程内执行）
    包含搜索结果提取、深度爬取、重试逻辑
    """
    page = _new_page()
    if not page:
        return None

    try:
        if deep is None:
            deep = _should_deep_crawl(query)

        url = f"https://cn.bing.com/search?q={query}"
        print(f"[搜索] {'深度' if deep else '摘要'}模式: {query[:40]}...")

        # 使用 networkidle 等待完整JS渲染（Bing结果由JS动态生成）
        try:
            page.goto(url, wait_until='networkidle', timeout=15000)
        except PWTimeout:
            print(f"[搜索] networkidle超时，尝试继续提取结果")
        except Exception as e:
            if retry_count < 2:
                print(f"[搜索] 页面加载失败: {e}，重试 ({retry_count + 1}/2)")
                page.close()
                return _search_impl(query, deep, retry_count + 1)
            return None

        # Bing结果由JS渲染，需要额外等待
        page.wait_for_timeout(2000)

        # 智能等待：等搜索结果出现（最多10秒）
        try:
            page.wait_for_selector('li.b_algo, #b_results', timeout=10000)
            print("[搜索] 搜索结果已加载")
        except PWTimeout:
            items_check = page.query_selector_all('li.b_algo')
            if items_check:
                print(f"[搜索] wait_for_selector超时但实际已找到{len(items_check)}条结果，继续")
            else:
                body_snippet = _clean_text(page.inner_text('body'))[:200]
                print(f"[搜索] 页面内容预览: {body_snippet}")
                if retry_count < 2:
                    print(f"[搜索] 页面无结果，重试 ({retry_count + 1}/2)")
                    page.close()
                    time.sleep(1)
                    return _search_impl(query, deep, retry_count + 1)
                print(f"[搜索] 页面无结果，已重试2次，放弃")
                return None
        except Exception as e:
            print(f"[搜索] 等待异常: {e}")

        # ── 提取 Bing 搜索结果（多重选择器备选）──
        results_meta = []

        for selector in ['li.b_algo', '#b_results > li', '.b_algo']:
            items = page.query_selector_all(selector)
            if items:
                break
        else:
            items = []

        if not items:
            print("[搜索] 未找到任何搜索结果条目")
            if retry_count < 2:
                print(f"[搜索] 尝试重试 ({retry_count + 1}/2)")
                time.sleep(1)
                page.close()
                return _search_impl(query, deep, retry_count + 1)
            return None

        clean = lambda s: re.sub(r'\s+', ' ', s).strip()

        for item in items[:8]:
            try:
                title = ""
                snippet = ""
                link = ""

                h2 = item.query_selector('h2')
                if h2:
                    title = _clean_text(h2.inner_text())
                    a = h2.query_selector('a')
                    if a:
                        link = a.get_attribute('href') or ""
                else:
                    a = item.query_selector('a')
                    if a:
                        title = _clean_text(a.inner_text())
                        link = a.get_attribute('href') or ""

                for sel in ['p', '.b_caption p', '.b_algo_slug p', 'div[class*="caption"]']:
                    el = item.query_selector(sel)
                    if el:
                        snippet = _clean_text(el.inner_text())
                        if snippet:
                            break

                if title and any(x in title for x in ['查看次数', '播放']):
                    continue
                if not title and not snippet:
                    continue

                results_meta.append((clean(title), clean(snippet), link))
            except Exception:
                continue

        if not results_meta:
            print("[搜索] 无结果")
            # 尝试从页面直接提取文本作为降级
            try:
                body_text = _clean_text(page.inner_text('body'))
                # 提取前500字作为降级结果
                if len(body_text) > 100:
                    # 过滤掉导航、广告等噪声
                    lines = body_text.split('\n')
                    useful_lines = []
                    for line in lines:
                        line = line.strip()
                        if len(line) < 10:
                            continue
                        if any(x in line for x in ['登录', '注册', '广告', '隐私', '版权', '©', 'Microsoft', 'Bing']):
                            continue
                        useful_lines.append(line)
                        if len(useful_lines) >= 5:
                            break
                    if useful_lines:
                        result = "\n".join(useful_lines)
                        print(f"[搜索] 降级提取页面文本 ({len(result)}字)")
                        return result
            except Exception as e:
                print(f"[搜索] 降级提取失败: {e}")
            return None

        # ── 仅摘要模式 ──
        if not deep:
            lines = []
            for title, snippet, _ in results_meta[:3]:
                if title and snippet:
                    lines.append(f"{title}：{snippet}"[:200])
                elif title:
                    lines.append(title[:200])
                elif snippet:
                    lines.append(snippet[:200])
            print(f"[搜索] 返回 {len(lines)} 条摘要")
            return "\n".join(lines) if lines else None

        # ── 深度爬取模式 ──
        fallback_lines = []
        for title, snippet, _ in results_meta[:3]:
            text = f"{title}：{snippet}" if title and snippet else (title or snippet)
            if text:
                fallback_lines.append(text[:200])

        candidates = [(score, title, link)
                      for title, snippet, link in results_meta
                      if _is_crawlable_url(link)
                      for score in [_score_url(link)]]
        candidates.sort(key=lambda x: x[0], reverse=True)

        # 关闭搜索页，释放资源（深度爬取会开新页面）
        page.close()
        page = None

        deep_result = None
        for score, title, link in candidates[:3]:
            if not link:
                continue
            deep_result = crawl_page(link, query=query)
            if deep_result and len(deep_result) > 50:
                print(f"[深度爬取] 成功获取正文，来自: {link[:50]}")
                break

        if deep_result:
            header = fallback_lines[0] if fallback_lines else ""
            if header:
                return f"{header}\n\n【详细内容】\n{deep_result}"
            else:
                return deep_result
        else:
            print("[深度爬取] 未获取到正文，降级为摘要")
            return "\n".join(fallback_lines) if fallback_lines else None

    except Exception as e:
        print(f"[搜索错误] {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def search_and_summarize(query: str, deep: bool = None, retry_count: int = 0) -> Optional[str]:
    """
    搜索并返回格式化的结果（线程安全，从任意线程调用）

    Args:
        query: 搜索词
        deep: 是否深度爬取正文。None=自动判断（根据关键词）
        retry_count: 当前重试次数（内部使用，外部忽略）

    Returns:
        格式化的搜索结果文本
    """
    task = _submit_task("search", query=query, deep=deep)
    return task.get("result")


def cleanup():
    """关闭浏览器，释放资源"""
    task = _submit_task("cleanup")
    _browser_thread_running = False


if __name__ == "__main__":
    print("=== 摘要模式测试 ===")
    result = search_and_summarize("鸣潮 更新 最新", deep=False)
    if result:
        print(f"结果 ({len(result)}字):\n{result}")
    else:
        print("无结果")

    print("\n=== 深度爬取测试 ===")
    result2 = search_and_summarize("鸣潮 达妮娅 攻略 配队", deep=True)
    if result2:
        print(f"结果 ({len(result2)}字):\n{result2}")
    else:
        print("无结果")

    cleanup()
    print("\n[完成] 浏览器已关闭")
