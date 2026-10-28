#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NapCat WebSocket 适配层
通过 WebSocket 接收消息，HTTP API 发送消息
"""

import json
import asyncio
import collections
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import websockets
import requests as _requests

# 主人白名单：只有这些 QQ 号能让达妮娅操作本机（打开应用/截屏/UI 自动化/文件…）
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")
_OWNER_CACHE = {"mtime": 0, "uids": set()}


def _owner_uids() -> set:
    """读取 settings.json 中的 owner_qq_uids（按 mtime 缓存，改配置无需重启）。"""
    try:
        mt = os.path.getmtime(_SETTINGS_PATH)
        if _OWNER_CACHE["mtime"] == mt:
            return _OWNER_CACHE["uids"]
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        raw = cfg.get("owner_qq_uids") or []
        if isinstance(raw, (str, int)):
            raw = [raw]
        uids = {str(x).strip() for x in raw if str(x).strip()}
        _OWNER_CACHE["mtime"] = mt
        _OWNER_CACHE["uids"] = uids
        return uids
    except Exception as e:
        print(f"[权限] 读取主人白名单失败: {e}")
        return set()


def _is_owner(user_id) -> bool:
    owners = _owner_uids()
    if not owners:
        print("[权限] 未配置 owner_qq_uids，所有请求按访客处理（本机工具全部禁用）")
        return False
    return str(user_id) in owners


_SETTINGS_CACHE = {"mtime": 0, "cfg": {}}


def _setting(key: str, default=None):
    """读取 settings.json 单项配置（按 mtime 缓存，改配置无需重启）。"""
    try:
        mt = os.path.getmtime(_SETTINGS_PATH)
        if _SETTINGS_CACHE["mtime"] != mt:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                _SETTINGS_CACHE["cfg"] = json.load(f)
            _SETTINGS_CACHE["mtime"] = mt
        return _SETTINGS_CACHE["cfg"].get(key, default)
    except Exception:
        return default


# 消息处理线程池（限制最大并发线程数，防止消息洪泛）
_MSG_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="msg_handler")

# 会话级串行队列：同一会话（私聊 QQ 号 / 群号）的消息严格按到达顺序逐条处理，
# 不同会话之间仍可并发。避免连发消息时回复交错、历史写入竞争。
_SESSION_SLOTS = {}      # key -> {"q": deque, "running": bool}
_SESSION_SLOTS_LOCK = threading.Lock()


def _session_key(data: dict) -> str:
    """会话标识：私聊按 QQ 号，群聊按群号（同群共享一份历史）"""
    msg_type = data.get("message_type") or ""
    if msg_type == "group":
        return f"group:{data.get('group_id')}"
    return f"private:{data.get('user_id')}"


def _enqueue_message(data: dict):
    """把消息放进所属会话的队列；该会话空闲时立刻起一个消费线程"""
    try:
        key = _session_key(data)
    except Exception:
        _MSG_POOL.submit(process_message, data)
        return
    with _SESSION_SLOTS_LOCK:
        slot = _SESSION_SLOTS.get(key)
        if slot is None:
            slot = {"q": collections.deque(), "running": False}
            _SESSION_SLOTS[key] = slot
        slot["q"].append(data)
        if slot["running"]:
            return
        slot["running"] = True
    _MSG_POOL.submit(_drain_session, key)


def _drain_session(key: str):
    """逐条消费某个会话的队列，队列空了就让出运行权"""
    while True:
        with _SESSION_SLOTS_LOCK:
            slot = _SESSION_SLOTS.get(key)
            if slot is None or not slot["q"]:
                if slot is not None:
                    slot["running"] = False
                    # 队列已空，回收槽位避免长期堆积
                    if not slot["q"]:
                        _SESSION_SLOTS.pop(key, None)
                return
            data = slot["q"].popleft()
        # 标记该会话的 bot 正在使用，避免清理逻辑把它当空闲实例回收
        sid = _bot_sid_for(data)
        with _BOT_BUSY_LOCK:
            _BOT_BUSY.add(sid)
        try:
            process_message(data)
        except Exception as e:
            print(f"[消息队列] 处理出错({key}): {e}")
        finally:
            with _BOT_BUSY_LOCK:
                _BOT_BUSY.discard(sid)


from denia_chatbot import DeniaChatbot
from config_napcat import *

try:
    from image_recognizer import parse_cq_images, process_image_message
    HAS_IMAGE_RECOGNITION = True
except ImportError:
    HAS_IMAGE_RECOGNITION = False

# 全局存储
bots: dict[str, DeniaChatbot] = {}
_bot_last_used: dict[str, float] = {}  # 记录每个 bot 最后使用时间
_MAX_BOTS = 50  # 最大 bot 实例数量
_BOT_TTL = 3600  # bot 空闲超过1小时清理
_BOT_BUSY = set()  # 正在处理消息的 bot session_id，清理时跳过
_BOT_BUSY_LOCK = threading.Lock()


def _bot_sid_for(data: dict) -> str:
    """与 get_bot() 的 session_id 命名保持一致，用于标记 bot 忙闲"""
    if (data.get("message_type") or "") == "group":
        return f"group_{data.get('group_id')}_{data.get('user_id')}"
    return f"private_{data.get('user_id')}"

# 回复频率控制
_last_reply_time = {"private": {}, "group": {}}  # 记录每个用户/群最后回复时间

# 群聊限流（开群后生效）：单用户冷却 + 单群每分钟回复上限，主人豁免
GROUP_USER_COOLDOWN = 15          # 同一人在同一群两次触发的最小间隔（秒）
GROUP_RATE_WINDOW = 60            # 统计窗口（秒）
GROUP_MAX_REPLIES = 8             # 单群每个窗口内最多回复次数
_group_user_last = {}             # (群号, QQ号) -> 时间戳
_group_hits = {}                  # 群号 -> deque[时间戳]
_group_rate_lock = threading.Lock()


def _group_rate_ok(group_id, user_id) -> bool:
    """群聊回复频率检查：单用户冷却 + 单群每分钟上限。主人不受限。"""
    if _is_owner(user_id):
        return True
    _prune_rate_maps()
    now = time.time()
    with _group_rate_lock:
        key = (group_id, user_id)
        last = _group_user_last.get(key, 0)
        if now - last < GROUP_USER_COOLDOWN:
            print(f"[限流] 群{group_id} 用户{user_id} 冷却中，剩余 {GROUP_USER_COOLDOWN - (now - last):.0f}s")
            return False
        hits = _group_hits.setdefault(group_id, collections.deque())
        while hits and now - hits[0] > GROUP_RATE_WINDOW:
            hits.popleft()
        if len(hits) >= GROUP_MAX_REPLIES:
            print(f"[限流] 群{group_id} 已达每分钟上限 {GROUP_MAX_REPLIES} 条，丢弃")
            return False
        _group_user_last[key] = now
        hits.append(now)
        return True


# 限流时间表的清理节流（每 5 分钟最多扫一次）
_RATE_PRUNE = {"at": 0.0}


def _prune_rate_maps():
    """定期清理限流/回复时间表，避免群成员多了以后字典只增不减"""
    now = time.time()
    if now - _RATE_PRUNE["at"] < 300:
        return
    _RATE_PRUNE["at"] = now
    with _group_rate_lock:
        for k, ts in list(_group_user_last.items()):
            if now - ts > GROUP_USER_COOLDOWN * 4:
                _group_user_last.pop(k, None)
        for gid, hits in list(_group_hits.items()):
            while hits and now - hits[0] > GROUP_RATE_WINDOW:
                hits.popleft()
            if not hits:
                _group_hits.pop(gid, None)
    # 普通回复冷却只看最近一小段时间，1 小时前的记录已无意义
    for bucket in _last_reply_time.values():
        for k, ts in list(bucket.items()):
            if now - ts > 3600:
                bucket.pop(k, None)

# 点歌功能：默认音乐平台（NapCat music CQ 码支持的音源）
# 网易云音乐 → music type=163，QQ音乐 → music type=qq
_DEFAULT_MUSIC_PLATFORM = "163"  # NapCat CQ 码里的 type 值
_MUSIC_PLATFORM_CQ_TYPES = {
    "qq": "qq",      # QQ音乐
    "163": "163",   # 网易云音乐
    "kugou": "kugou",
    "migu": "migu",
    "kuwo": "kuwo",
}
_MUSIC_DISPLAY = {
    "qq": "QQ音乐",
    "163": "网易云音乐",
    "kugou": "酷狗音乐",
    "migu": "咪咕音乐",
    "kuwo": "酷我音乐",
}

# 点歌关键词（命中即走点歌快捷路径，跳过 LLM）
import re as _re

_MUSIC_TRIGGERS = ("点歌", "来一首", "来一曲", "来首", "放首歌", "放个歌", "放首", "放一",
                   "播放", "唱首歌", "唱首", "唱一", "听一", "听首",
                   "我要听", "想听", "要听", "听听", "听个", "听点")
_MUSIC_EXCLUDE = ("搜索", "搜一下", "查查", "查询", "查一下", "找歌", "找歌曲", "歌词")
# 疑问/求教类意图：问"功能怎么用/是什么/为什么"时绝不能当成点歌
_MUSIC_QUESTION_RE = _re.compile(
    r"(怎么用|怎样用|如何用|怎么弄|怎么弄的|怎么使用|是什么|为什么|干嘛|能干嘛|"
    r"功能|教学|介绍|在哪|有没有|支持吗|怎么点|怎么发)"
)

# 点歌前缀模式（有序匹配，先长后短，避免"唱首歌"被"歌"抢占）
_SONG_PREFIX_PAT = _re.compile(
    r"(?:帮我|给我|请|来|放|播放|唱|听|要)\s*"
    r"(?:一[首曲段])?\s*"
    r"(?P<name>[\u4e00-\u9fa5a-zA-Z0-9_··]{2,20})"
)


def _search_song_id(song_name: str, platform: str = "163") -> dict | None:
    """搜索歌曲，返回 {id, title, singer} 或 None。

    支持网易云(163)、QQ音乐(qq)、酷狗(kugou) 等音源。
    """
    import requests as _req

    # 网易云音乐
    if platform == "163":
        try:
            resp = _req.get(
                "https://music.163.com/api/search/get/web",
                params={"s": song_name, "type": 1, "limit": 1, "offset": 0},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            songs = data.get("result", {}).get("songs", [])
            if songs:
                s = songs[0]
                artists = s.get("artists") or []
                singer = "/".join(
                    a.get("name", "") for a in artists if a.get("name"))
                return {"id": str(s["id"]), "title": s.get("name", song_name),
                        "singer": singer or song_name}
            return None
        except Exception as e:
            print(f"[点歌] 网易云搜索失败: {e}")
            return None

    # QQ音乐（简单搜索接口）
    elif platform == "qq":
        try:
            resp = _req.get(
                "https://c.y.qq.com/soso/fcgi_bin/search_sg_by_key_word",
                params={"keyWord": song_name, "perpage": 1, "page": 1},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            resp.raise_for_status()
            text = resp.text
            # 返回数据在 js 回调里，取 JSON 部分
            start = text.find("{")
            end = text.rfind("}") + 1
            data = _req.json.loads(text[start:end]) if start >= 0 else {}
            songs = data.get("data", {}).get("song", {}).get("list", [])
            if songs:
                s = songs[0]
                mid = s.get("mid", "")
                info = s.get("songinfo", [{}])
                return {
                    "id": mid,
                    "title": info[0].get("name", song_name) if info else song_name,
                    "singer": song_name,
                }
            return None
        except Exception as e:
            print(f"[点歌] QQ音乐搜索失败: {e}")
            return None

    # 酷狗
    elif platform == "kugou":
        try:
            resp = _req.get(
                "https://www.kugou.com/api/v3/search/song",
                params={"keyword": song_name, "page": 1, "pagesize": 1},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            songs = data.get("data", {}).get("info", [])
            if songs:
                s = songs[0]
                return {"id": str(s.get("hash", "")), "title": s.get("name", song_name), "singer": s.get("artists_name", song_name)}
            return None
        except Exception as e:
            print(f"[点歌] 酷狗搜索失败: {e}")
            return None

    return None


def get_bot(session_id: str, user_id: int = 0, is_group: bool = False) -> DeniaChatbot:
    """获取或创建指定会话的 DeniaChatbot 实例"""
    if session_id not in bots:
        _cleanup_idle_bots()
        bot = DeniaChatbot(
            use_llm=USE_LLM,
            llm_model=LLM_MODEL,
            llm_api_key=LLM_API_KEY,
            llm_base_url=LLM_BASE_URL if LLM_BASE_URL else None,
            user_id=user_id,
            history_key=session_id,
            # QQ 机器人是无 GUI 子进程，确认弹窗会崩，统一走直接执行
            allow_confirm=False,
        )
        bot.is_group_chat = is_group
        bots[session_id] = bot
    _bot_last_used[session_id] = time.time()
    return bots[session_id]


def _cleanup_idle_bots():
    """清理空闲过期的 bot 实例，防止内存泄漏（正在处理消息的不动）"""
    now = time.time()
    with _BOT_BUSY_LOCK:
        busy = set(_BOT_BUSY)
    # 清理超过 TTL 的
    expired = [sid for sid, t in _bot_last_used.items() if now - t > _BOT_TTL and sid not in busy]
    for sid in expired:
        bots.pop(sid, None)
        _bot_last_used.pop(sid, None)
    # 如果仍然超量，清理最久未使用的
    if len(bots) > _MAX_BOTS:
        excess = len(bots) - _MAX_BOTS
        candidates = [sid for sid in sorted(_bot_last_used, key=_bot_last_used.get) if sid not in busy]
        for sid in candidates[:excess]:
            bots.pop(sid, None)
            _bot_last_used.pop(sid, None)


def _flatten_reply(text: str) -> str:
    """把 LLM 回复里的换行折叠成单行，避免 QQ 把每条换行渲染成独立消息"""
    if not text:
        return text
    import re
    # 多个换行合并为一个空格
    text = re.sub(r'\n+', ' ', text)
    # 去掉行首空白
    return text.strip()


_TRACE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "qq_trace.jsonl")


def _trace_turn(chat_type: str, chat_id, user_text: str, reply_text: str):
    """记一轮对话，供控制台「最近对话」复盘（直连工具 = 快路径命中的工具）"""
    try:
        import dania_tools  # noqa: F401  确保技能包已注册（没加载过时会自动加载）
        from tool_registry import match_tool
        _info = match_tool(user_text or "")
        tool = _info.name if _info else ""
    except Exception:
        tool = ""
    rec = {
        "ts": time.strftime("%H:%M:%S"),
        "chat": "私聊" if chat_type == "private" else "群聊",
        "chat_id": str(chat_id),
        "text": (user_text or "")[:120],
        "tool": tool,
        "reply": (reply_text or "")[:160],
    }
    try:
        os.makedirs(os.path.dirname(_TRACE_FILE), exist_ok=True)
        with open(_TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.getsize(_TRACE_FILE) > 256 * 1024:
            with open(_TRACE_FILE, encoding="utf-8") as f:
                tail = f.readlines()[-200:]
            with open(_TRACE_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except Exception:
        pass


def _split_reply(text: str, max_parts: int = 3, max_len: int = 35) -> list:
    """把整段回复拆成 2~3 条短消息，模仿真人连发短句

    - 太短、含链接/代码块的不拆，避免把有用信息割断
    - 按句号/叹号/省略号/换行切，再把相邻短句合并到 max_len 以内
    """
    t = (text or "").strip()
    if not t or len(t) <= 40 or "http://" in t or "https://" in t or "```" in t:
        return [t]
    pieces = [p.strip() for p in _re.split(r"(?<=[。！？!?…~～])\s*|\n+", t) if p.strip()]
    if len(pieces) <= 1:
        return [t]
    parts, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) > max_len:
            parts.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        parts.append(cur)
    if len(parts) > max_parts:
        parts = parts[:max_parts - 1] + ["".join(parts[max_parts - 1:])]
    return parts or [t]


def send_reply_split(chat_type: str, chat_id, text: str):
    """按真人节奏分条发送回复（条间随机停顿）"""
    for i, part in enumerate(_split_reply(text)):
        if i:
            time.sleep(random.uniform(0.35, 0.85))
        if chat_type == "group":
            send_group_msg(chat_id, part)
        else:
            send_private_msg(chat_id, part)


def send_private_msg(user_id: int, message: str, voice_cq: str = None):
    """向私聊用户发送消息和语音

    有语音时只发语音条，避免文字和语音重复；
    语音合成失败时退化为发文字，保证至少有回复。
    """
    url = f"{NAPCAT_HTTP_URL}/send_private_msg"

    if voice_cq:
        full_message = voice_cq        # 只发语音，不再附带文字
    else:
        full_message = _flatten_reply(message)  # 折叠换行，单行发送

    payload = {"user_id": user_id, "message": full_message}
    try:
        resp = _requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        if voice_cq:
            print(f"[私聊] → {user_id}: [语音消息]")
        else:
            print(f"[私聊] → {user_id}: {message[:50]}...")
    except Exception as e:
        print(f"[私聊发送失败] {e}")


def send_group_msg(group_id: int, message: str, voice_cq: str = None):
    """向群聊发送消息和语音

    有语音时只发语音条，避免文字和语音重复；
    语音合成失败时退化为发文字。
    """
    url = f"{NAPCAT_HTTP_URL}/send_group_msg"

    if voice_cq:
        full_message = voice_cq        # 只发语音，不再附带文字
    else:
        full_message = _flatten_reply(message)  # 折叠换行，单行发送

    payload = {"group_id": group_id, "message": full_message}
    try:
        resp = _requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        if voice_cq:
            print(f"[群聊] → 群{group_id}: [语音消息]")
        else:
            print(f"[群聊] → 群{group_id}: {message[:50]}...")
    except Exception as e:
        print(f"[群聊发送失败] {e}")


def strip_cq_at(text: str, self_id: int) -> str:
    """去除消息中的 CQ:at 标签，返回纯文本"""
    import re
    pattern = rf"\[CQ:at,qq={self_id}\]"
    return re.sub(pattern, "", text).strip()


def _extract_song_name(text: str) -> str | None:
    """从消息中提取点歌歌曲名。

    匹配点歌关键词后提取歌曲名，未提取到则返回 None。
    """
    import re
    # 先检查是否包含点歌关键词
    found_kw = None
    for kw in _MUSIC_TRIGGERS:
        if kw in text:
            found_kw = kw
            break
    if not found_kw:
        return None

    # 排除"搜索"类意图
    for ex in _MUSIC_EXCLUDE:
        if ex in text:
            return None

    # 排除求教/疑问意图（"音乐卡片功能我该怎么用""点歌怎么用"）
    if _MUSIC_QUESTION_RE.search(text):
        print(f"[点歌] 判定为求教/疑问句，跳过点歌: {text[:30]}")
        return None

    # 去掉 CQ 标签后的纯文本
    clean = re.sub(r"\[CQ:[^\]]*\]", "", text).strip()

    # 尝试用前缀模式提取（关键词后面的内容）
    # 先定位关键词位置，从关键词后开始匹配
    idx = clean.find(found_kw)
    after = clean[idx + len(found_kw):].strip()

    # 如果关键词是"点歌"，后跟歌名
    if found_kw == "点歌":
        if not after:
            # 纯"点歌"无歌名 → 让 LLM 处理
            return None
        m = _SONG_PREFIX_PAT.match(after)
        if m:
            return m.group("name")
        # 直接跟歌名
        if re.match(r"^[\u4e00-\u9fa5a-zA-Z0-9_·]{2,20}$", after):
            return after
        return None

    # 如果关键词含"一"（来一首/来一曲/放首/唱首等），歌名紧跟其后
    # 关键词本身 = "来一首"，after = "小情歌"
    # 关键词本身 = "播放"，after = "周杰伦"
    m = _SONG_PREFIX_PAT.match(after)
    if m:
        return m.group("name")

    # 去掉"的"前缀和语气词
    if after:
        after = re.sub(r"^(?:的|那|这|首|曲)+", "", after)
        after = re.sub(r"(?:的|吧|嘛|呢|呀|啊|哦|把)$", "", after).strip()
        # 「我想听你说」这类不是点歌
        if after in ("你说", "我说", "你唱", "我唱", "你说说"):
            return None
        if 2 <= len(after) <= 20 and re.match(r"^[\u4e00-\u9fa5a-zA-Z0-9_·]+$", after):
            return after

    return None


def _netease_cover(song_id: str) -> str:
    """按网易云歌曲 ID 取专辑封面（失败返回空串）"""
    try:
        r = _requests.get(
            "https://music.163.com/api/song/detail",
            params={"ids": f"[{song_id}]"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        songs = (r.json() or {}).get("songs") or []
        return ((songs[0].get("album") or {}).get("picUrl") or "") if songs else ""
    except Exception:
        return ""


def _music_card_data(platform: str, result: dict) -> dict:
    """组装 music 消息段的数据

    NapCat 会把整个 data 原样转发给外部的音乐卡片签名服务，服务端
    要求至少带 title；只给 type+id 会返回 400「缺少 title」，NapCat
    退回备用地址超时后直接丢弃卡片，表现就是「文字发了、卡片没出来」。
    """
    data = {
        "type": platform,
        "id": str(result.get("id") or ""),
        "title": result.get("title") or "",
        "content": result.get("singer") or "",
    }
    if platform == "163" and data["id"]:
        cover = _netease_cover(data["id"])
        if cover:
            data["image"] = cover
    return data


def _handle_song_request(text: str, chat_type: str, chat_id: int, user_id: int) -> bool:
    """处理点歌请求。成功处理返回 True，跳过（非点歌）返回 False。

    通过 NapCat music CQ 码发送音乐卡片，QQ 聊天界面可直接播放。
    """
    song_name = _extract_song_name(text)
    if not song_name:
        return False

    print(f"[点歌] {chat_type} {chat_id}: 歌曲={song_name}")

    # 识别用户指定了哪个平台（默认网易云）
    platform = _DEFAULT_MUSIC_PLATFORM
    for p in _MUSIC_PLATFORM_CQ_TYPES:
        if p in text.lower():
            platform = p
            break
    display = _MUSIC_DISPLAY.get(platform, "网易云音乐")

    # 搜索歌曲获取 ID
    result = _search_song_id(song_name, platform)
    if not result or not result.get("id"):
        reply = f"嗯……我搜了搜，{display}里暂时没找到《{song_name}》呢。换个歌名或歌手我再搜一次？"
        if chat_type == "private":
            send_private_msg(user_id, reply)
            _last_reply_time["private"][user_id] = time.time()
        else:
            send_group_msg(chat_id, reply)
            _last_reply_time["group"][chat_id] = time.time()
        return True

    # 构建 CQ:music 卡片 + 文字说明
    cq_music = f"[CQ:music,type={platform},id={result['id']}]"
    title = result.get("title", song_name)
    singer = result.get("singer", "")
    prefix = f"好的呢，找到《{title}》了～"
    if singer:
        prefix += f" {singer}"
    prefix += f"（{display}）"

    if chat_type == "private":
        # 先发文字说明，再发音乐卡片（OneBot 11 标准 message 段格式）
        url = f"{NAPCAT_HTTP_URL}/send_private_msg"
        msg_arr = [{"type": "text", "data": {"text": prefix}},
                   {"type": "music", "data": _music_card_data(platform, result)}]
        try:
            _resp = _requests.post(url, json={"user_id": user_id, "message": msg_arr}, timeout=30)
            _data = _resp.json() if _resp.content else {}
            if _data.get("retcode") not in (0, None):
                raise RuntimeError(f"retcode={_data.get('retcode')} {_data.get('message') or _data.get('wording')}")
            print(f"[私聊] → {user_id}: [音乐卡片] 《{title}》 raw={msg_arr}")
        except Exception as e:
            # 降级：发纯文本
            print(f"[私聊音乐卡片发送失败] {e}，降级为文字")
            send_private_msg(user_id, prefix + "\n" + f"https://music.163.com/song?id={result['id']}")
        _last_reply_time["private"][user_id] = time.time()
    else:
        url = f"{NAPCAT_HTTP_URL}/send_group_msg"
        msg_arr = [{"type": "text", "data": {"text": prefix}},
                   {"type": "music", "data": _music_card_data(platform, result)}]
        try:
            _resp = _requests.post(url, json={"group_id": chat_id, "message": msg_arr}, timeout=30)
            _data = _resp.json() if _resp.content else {}
            if _data.get("retcode") not in (0, None):
                raise RuntimeError(f"retcode={_data.get('retcode')} {_data.get('message') or _data.get('wording')}")
            print(f"[群聊] → 群{chat_id}: [音乐卡片] 《{title}》 raw={msg_arr}")
        except Exception as e:
            print(f"[群聊音乐卡片发送失败] {e}，降级为文字")
            send_group_msg(chat_id, prefix + "\n" + f"https://music.163.com/song?id={result['id']}")
        _last_reply_time["group"][chat_id] = time.time()
    return True


def _is_voice_only(raw_msg: str) -> bool:
    """检测是否是纯语音消息（不包含任何文字）"""
    import re
    # 去掉所有 CQ 码
    stripped = re.sub(r'\[CQ:[^\]]*\]', '', raw_msg).strip()
    # 去掉空白
    return not stripped


def _handle_voice_only_message(chat_type: str, chat_id: int, user_id: int):
    """处理纯语音消息（无文字）—— 发提示让用户打字，不发语音条"""
    voice_replies = [
        "嗯……你发了条语音呢……可我现在还听不太懂语音……",
        "啊……语音呀……我现在只看得懂文字和图片呢，打字跟我说嘛。",
        "唔……语音我听不到啦，打字告诉我好不好。",
        "唔……我还不会听语音呢，用文字跟我说话嘛。",
    ]
    reply = random.choice(voice_replies)
    print(f"[语音] 收到语音消息（无识别模块），回复提示")
    if chat_type == "private":
        send_private_msg(user_id, reply)
        _last_reply_time["private"][user_id] = time.time()
    else:
        send_group_msg(chat_id, reply)
        _last_reply_time["group"][chat_id] = time.time()
    return "SKIP"  # 告诉上层跳过后续处理


def send_image_files(chat_id, paths, is_group: bool) -> int:
    """发送图片（http 链接直接让 NapCat 下载，本地路径走 file://，失败退 base64）"""
    if not paths:
        return 0
    url = f"{NAPCAT_HTTP_URL}/{'send_group_msg' if is_group else 'send_private_msg'}"
    key = "group_id" if is_group else "user_id"
    ok = 0
    for p in paths[:6]:
        if p.startswith(("http://", "https://")):
            # 直接把远端链接交给 NapCat，不在本地落盘
            try:
                seg = {"type": "image", "data": {"file": p}}
                resp = _requests.post(url, json={key: chat_id, "message": [seg]}, timeout=25)
                data = resp.json() if resp.content else {}
                if data.get("retcode") in (0, 1):
                    ok += 1
                    continue
                raise RuntimeError(str(data.get("message") or data)[:80])
            except Exception as e:
                print(f"[图片] 链接发送失败（{e}），改为下载后发送")
                try:
                    import media_downloader
                    local = media_downloader.MediaDownloader().download_image(p, keyword="qq_send")
                except Exception as e2:
                    print(f"[图片] 链接下载失败: {e2}")
                    continue
                if not local:
                    continue
                p = local
        seg = None
        try:
            seg = {"type": "image", "data": {"file": "file:///" + p.replace("\\", "/")}}
            resp = _requests.post(url, json={key: chat_id, "message": [seg]}, timeout=20)
            data = resp.json() if resp.content else {}
            if data.get("retcode") not in (0, 1):
                raise RuntimeError(str(data.get("message") or data)[:80])
            ok += 1
            continue
        except Exception as e:
            print(f"[图片] 本地路径发送失败（{e}），改用 base64")
        try:
            import base64
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            seg = {"type": "image", "data": {"file": "base64://" + b64}}
            resp = _requests.post(url, json={key: chat_id, "message": [seg]}, timeout=40)
            data = resp.json() if resp.content else {}
            if data.get("retcode") in (0, 1):
                ok += 1
            else:
                print(f"[图片] base64 发送失败: {str(data.get('message') or data)[:80]}")
        except Exception as e:
            print(f"[图片] 发送失败: {e}")
    print(f"[图片] 已发送 {ok}/{len(paths[:6])} 张 → {key}={chat_id}")
    return ok


def _flush_images(chat_type: str, chat_id):
    """把工具排队在当前会话里的图片发出去（没有就跳过）"""
    try:
        import qq_outbox
        imgs = qq_outbox.take_images()
    except Exception:
        imgs = []
    if imgs:
        send_image_files(chat_id, imgs, is_group=(chat_type == "group"))


def should_reply(chat_type: str, chat_id: int, is_mentioned: bool, has_image: bool) -> bool:
    """
    判断是否应该回复，综合概率 + 冷却时间 + 特殊情况

    Args:
        chat_type: "private" 或 "group"
        chat_id: 用户ID（私聊）或群ID（群聊）
        is_mentioned: 是否被@了（群聊）
        has_image: 是否包含图片

    Returns:
        True=应该回复，False=跳过
    """
    _prune_rate_maps()
    # 私聊几乎总是回，除非在冷却期
    if chat_type == "private":
        if has_image:
            return True  # 有图片必回
        # 检查冷却
        last = _last_reply_time["private"].get(chat_id, 0)
        now = time.time()
        cooldown = REPLY_COOLDOWN_PRIVATE
        if now - last < cooldown:
            print(f"[限流] 私聊冷却中，剩余 {cooldown - (now - last):.0f}s")
            return False
        # 概率判断
        if random.random() < REPLY_PROBABILITY_PRIVATE:
            return True
        else:
            print(f"[限流] 私聊概率跳过 ({REPLY_PROBABILITY_PRIVATE*100:.0f}%)")
            return False

    # 群聊：被@或有图片必回
    if chat_type == "group":
        if is_mentioned or has_image:
            return True

        # 检查冷却
        last = _last_reply_time["group"].get(chat_id, 0)
        now = time.time()
        cooldown = REPLY_COOLDOWN_GROUP
        if now - last < cooldown:
            print(f"[限流] 群聊冷却中，剩余 {cooldown - (now - last):.0f}s")
            return False

        # 概率判断
        if random.random() < REPLY_PROBABILITY_GROUP:
            return True
        else:
            print(f"[限流] 群聊概率跳过 ({REPLY_PROBABILITY_GROUP*100:.0f}%)")
            return False

    return True


def process_message(data: dict):
    """处理收到的消息"""
    post_type = data.get('post_type')
    if post_type != 'message':
        return

    msg_type = data.get('message_type')
    raw_msg = data.get('raw_message', '') or data.get('message', '')
    sender = data.get('sender', {})
    user_id = sender.get('user_id') or data.get('user_id')
    self_id = data.get('self_id', 0)

    if user_id == self_id:
        return

    print(f"[收到消息] {msg_type} from {user_id}: {raw_msg}")

    if msg_type == 'private':
        text = strip_cq_at(raw_msg, self_id)
        has_image = '[CQ:image' in raw_msg
        has_voice = '[CQ:record' in raw_msg or '[CQ:voice' in raw_msg

        # 语音消息处理：纯语音 → 提示打字（不再做 Whisper 转写，不发语音条）
        if has_voice and _is_voice_only(raw_msg):
            result = _handle_voice_only_message("private", chat_id=user_id, user_id=user_id)
            if result == "SKIP":
                return

        # 频率限制检查
        if not should_reply("private", user_id, is_mentioned=False, has_image=has_image):
            return

        # 图片识别：提取图片 + 文字分别处理
        image_desc = None
        if HAS_IMAGE_RECOGNITION and has_image:
            text, image_desc = process_image_message(raw_msg)

        if not text and not image_desc:
            return

        # 图片识别结果不再包装成标记文本，而是通过参数传递给LLM
        display_text = text or ""
        if image_desc:
            print(f"[识图] 识别结果: {image_desc[:80]}...")

        print(f"[私聊] ← {user_id}: {display_text[:80]}...")
        import qq_outbox as _outbox
        _outbox.set_current_chat("private", user_id)
        try:
            # 点歌快捷路径：命中点歌关键词直接找视频，不走 LLM
            if _handle_song_request(display_text, "private", user_id, user_id):
                return
            bot = get_bot(f"private_{user_id}", user_id=user_id, is_group=False)
            if bot.llm:
                bot.llm.is_group_chat = False
                bot.llm.user_id = user_id
            # 权限门：非主人禁用本机操作类工具，仅保留搜索/天气等纯查询能力
            from tool_registry import set_remote_restricted
            _restricted = not _is_owner(user_id)
            set_remote_restricted(_restricted)
            try:
                reply = bot.get_response(display_text, play_voice=False, image_desc=image_desc)
            finally:
                set_remote_restricted(False)
            _last_reply_time["private"][user_id] = time.time()
            # 语音功能已关闭：只发文字；本轮已排图时只发图，不输出文字
            if _outbox.pending_count() == 0:
                send_reply_split("private", user_id, reply)
            _flush_images("private", user_id)
            _trace_turn("private", user_id, display_text, reply)
        except Exception as e:
            print(f"[错误] 处理私聊消息失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            _outbox.clear_current_chat()

    elif msg_type == 'group':
        # 群聊默认关闭，需在 settings.json 设 "group_enabled": true 才启用
        if not _setting("group_enabled", False):
            return
        text = strip_cq_at(raw_msg, self_id)
        group_id = data.get('group_id')
        has_image = '[CQ:image' in raw_msg
        has_voice = '[CQ:record' in raw_msg or '[CQ:voice' in raw_msg

        # 检测是否被@
        is_mentioned = f"[CQ:at,qq={self_id}]" in raw_msg

        # 语音消息处理（群聊中仅处理被@时的纯语音）
        if has_voice and _is_voice_only(raw_msg) and is_mentioned:
            result = _handle_voice_only_message("group", chat_id=group_id, user_id=user_id)
            if result == "SKIP":
                return

        # 发言时机判断（多信号打分：不是每条群消息都要接）
        from speak_timing import should_speak, note_spoken
        _speak, _why = should_speak(group_id, user_id, text, is_mentioned, has_image)
        if not _speak:
            print(f"[发言时机] 群{group_id} 跳过（{_why}）")
            return

        # 单用户冷却 + 单群每分钟上限（硬约束，防止被连刷）
        if not _group_rate_ok(group_id, user_id):
            return

        # 图片识别：群聊也支持
        image_desc = None
        if HAS_IMAGE_RECOGNITION and has_image:
            text, image_desc = process_image_message(raw_msg)

        if not text and not image_desc:
            return

        # 图片识别结果不再包装成标记文本，而是通过参数传递给LLM
        display_text = text or ""
        if image_desc:
            print(f"[识图] 识别结果: {image_desc[:80]}...")

        group_id = data.get('group_id')
        print(f"[群聊] ← 群{group_id} 用户{user_id}: {display_text[:80]}...")
        import qq_outbox as _outbox
        _outbox.set_current_chat("group", group_id)
        try:
            # 点歌快捷路径（群聊）
            if _handle_song_request(display_text, "group", group_id, user_id):
                return
            bot = get_bot(f"group_{group_id}_{user_id}", user_id=group_id, is_group=True)
            if bot.llm:
                bot.llm.is_group_chat = True
                bot.llm.user_id = group_id
            # 权限门：群里只有主人能让达妮娅操作本机，其他人只能用查询类工具
            from tool_registry import set_remote_restricted
            set_remote_restricted(not _is_owner(user_id))
            try:
                reply = bot.get_response(display_text, play_voice=False, image_desc=image_desc)
            finally:
                set_remote_restricted(False)
            _last_reply_time["group"][group_id] = time.time()
            note_spoken(group_id)
            # 语音功能已关闭：只发文字；本轮已排图时只发图，不输出文字
            if _outbox.pending_count() == 0:
                send_reply_split("group", group_id, reply)
            _flush_images("group", group_id)
            _trace_turn("group", group_id, display_text, reply)
        except Exception as e:
            print(f"[错误] 处理群聊消息失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            _outbox.clear_current_chat()


async def ws_client():
    """WebSocket 客户端，连接 NapCat 接收消息。
    NapCat 未就绪时自动等待（轮询端口），不退出进程。
    """
    import socket as _socket

    ws_url = NAPCAT_WS_URL  # NapCat WS Server 在根路径推送消息，不加 /event

    # --- 等待 NapCat HTTP 端口就绪（扫码登录前 3000 可能未监听） ---
    print(f"[启动] 等待 NapCat 就绪 ({NAPCAT_HTTP_URL})...")
    napcat_ready = False
    for _attempt in range(36):  # 最多等 3 分钟（36 * 5s）
        s = _socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", 3000))
            napcat_ready = True
            print("[启动] NapCat 已就绪")
            break
        except Exception:
            pass
        finally:
            s.close()
        await asyncio.sleep(5)

    if not napcat_ready:
        print("[启动] ⚠️  NapCat 端口 3000 未在 3 分钟内就绪，继续重试连接中...")

    print(f"[WebSocket] 正在连接 {ws_url}...")
    retry_delay = 5  # 初始重试间隔（秒）

    while True:
        try:
            async with websockets.connect(ws_url) as websocket:
                print("[WebSocket] 连接成功！")
                retry_delay = 5  # 连接成功，重置退避
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        _enqueue_message(data)
                    except json.JSONDecodeError:
                        print(f"[WebSocket] 收到非 JSON 消息: {message[:100]}")
                    except Exception as e:
                        print(f"[WebSocket] 处理消息出错: {e}")
        except websockets.exceptions.ConnectionClosed:
            print(f"[WebSocket] 断开，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)
        except websockets.exceptions.InvalidStatusCode as e:
            if e.status_code == 403:
                print(f"[WebSocket] NapCat 未就绪（403），{retry_delay}s 后重试...")
            else:
                print(f"[WebSocket] 错误{e.status_code}，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)
        except OSError as e:
            if "拒绝" in str(e) or "refused" in str(e).lower():
                print(f"[WebSocket] NapCat 未监听，{retry_delay}s 后重试...")
            else:
                print(f"[WebSocket] 网络错误，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)
        except Exception as e:
            print(f"[WebSocket] 连接失败，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)

        # 指数退避：5 → 10 → 20 → 40 → 60（最大60秒）
        retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    print("=" * 50)
    print("  达妮娅 NapCat WebSocket 适配层")
    print(f"  NapCat WebSocket: {NAPCAT_WS_URL}")
    print(f"  NapCat HTTP API: {NAPCAT_HTTP_URL}")
    print("  群聊模式: 见话就接")
    print(f"  回复限流: 群聊{REPLY_PROBABILITY_GROUP*100:.0f}%/冷{REPLY_COOLDOWN_GROUP}s, 私聊{REPLY_PROBABILITY_PRIVATE*100:.0f}%/冷{REPLY_COOLDOWN_PRIVATE}s")
    print("  语音模式: 已关闭（只发文字）")
    print(f"  图片识别: {'已启用' if HAS_IMAGE_RECOGNITION else '未启用'}")
    print(f"  点歌功能: 音乐卡片（NapCat CQ:music，默认网易云，支持QQ/酷狗/咪咕/酷我）")
    print("=" * 50)

    try:
        asyncio.run(ws_client())
    except KeyboardInterrupt:
        print("\n[退出] 收到中断信号，正在关闭...")
