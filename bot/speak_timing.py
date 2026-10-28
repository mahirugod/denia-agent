# -*- coding: utf-8 -*-
"""群聊发言时机判断（参考 MaiBot 的「该不该开口」思路）

原来的规则是「被 @ / 有图必回，否则冷却 + 固定概率」，机器感很强。
这里换成多信号打分：被点名直接回；其余按提问、话题相关性、刷屏、抢话、
刚说过话、时段、消息类型加减分，超过阈值才开口。

群聊上下文始终记录（不回复也记），用于判断「是不是在接同一个话题」。
"""
import random
import re
import threading
import time
from collections import deque

SPEAK_THRESHOLD = 70      # 达到这个分才开口
CONTEXT_KEEP = 30         # 每个群保留最近多少条消息
CONTEXT_TTL = 600         # 超过 10 分钟的消息不再参与判断

_QUESTION = re.compile(r"[?？]|吗|呢|怎么|为什么|为啥|什么|谁|哪|是不是|有没有|能不能|会不会")
_SHORT_NOISE = re.compile(r"^(哈{2,}|草+|6+|hhh+|233+|笑死|笑|？？+|！+|\.{3,}|…+|。+|嗯+|哦+|啊+)$")
_MY_NAME = re.compile(r"达妮娅|达尼亚|dania", re.I)

_recent = {}        # gid -> deque[(ts, uid, text)]
_last_speak = {}    # gid -> 上次开口时间
_lock = threading.Lock()


def _prune(now: float):
    for gid, d in list(_recent.items()):
        while d and now - d[0][0] > CONTEXT_TTL:
            d.popleft()
        if not d:
            _recent.pop(gid, None)
    for gid, ts in list(_last_speak.items()):
        if now - ts > 3600:
            _last_speak.pop(gid, None)


def _bigrams(s: str) -> set:
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def should_speak(group_id, user_id, text: str, is_mentioned: bool,
                 has_image: bool, now: float = None) -> tuple:
    """返回 (是否开口, 原因说明)。被 @ 或叫名字必定开口。"""
    now = now or time.time()
    text = text or ""

    with _lock:
        d = _recent.get(group_id)
        if d is None:
            d = _recent[group_id] = deque(maxlen=CONTEXT_KEEP)
        prev = list(d)
        d.append((now, user_id, text))

    if is_mentioned:
        return True, "被@"
    named = bool(_MY_NAME.search(text))
    if named and len(prev) < 2:
        return True, "叫名字"

    score = 40
    why = ["叫名字"] if named else []
    if named:
        score += 60
    if _QUESTION.search(text):
        score += 25
        why.append("提问")
    plain = re.sub(r"\[CQ:[^\]]+\]", "", text).strip()
    n = len(plain)
    if n >= 8:
        score += 10
        why.append("有内容")
    if n >= 20:
        score += 5
    if n <= 4 and not has_image:
        score -= 20
        why.append("太短")
    if _SHORT_NOISE.match(plain):
        score -= 25
        why.append("语气词")
    if has_image:
        score += 25
        why.append("图片")

    # 同一个人一分钟内刷了 3 条以上
    if len([1 for ts, u, _ in prev if u == user_id and now - ts < 60]) >= 3:
        score -= 20
        why.append("刷屏")
    # 别人 8 秒内才说过话，别抢
    if prev and now - prev[-1][0] < 8 and prev[-1][1] != user_id:
        score -= 15
        why.append("抢话")
    # 自己刚开过口
    gap = now - _last_speak.get(group_id, 0)
    if gap < 90:
        score -= 35
        why.append("刚说过")
    elif gap < 180:
        score -= 15
    # 群里正热闹时更容易插得上话
    if len([1 for ts, _, _ in prev if now - ts < 60]) >= 6:
        score += 10
        why.append("群热")
    # 和最近 10 条有人聊的内容重合，说明在接同一个话题
    tb = _bigrams(plain)
    if tb:
        for _, u, t in prev[-10:]:
            if u == user_id:
                continue
            if len(tb & _bigrams(t)) >= 2:
                score += 15
                why.append("接话")
                break
    hour = time.localtime(now).tm_hour
    if 0 <= hour < 7:
        score -= 30
        why.append("深夜")

    score += random.randint(-10, 10)
    if score >= SPEAK_THRESHOLD:
        return True, f"score={score} {'+'.join(why)}"
    return False, f"score={score} {'+'.join(why) or '闲聊'}"


def note_spoken(group_id, now: float = None):
    """记录一次真实开口，供下次「刚说过话」降权"""
    now = now or time.time()
    _last_speak[group_id] = now
    _prune(now)
