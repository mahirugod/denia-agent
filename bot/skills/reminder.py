#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：提醒（reminder）
定时提醒：X分钟后/小时后，或写明时间（明天下午3点、下周一9点）。
支持相对/绝对时间双模式，threading.Timer 到点回调通知 UI。
"""
import re
import threading
from datetime import datetime, timedelta

from skills import skill
from tool_registry import tool

skill("reminder", "定时提醒（相对/绝对时间）", tools=["set_reminder"])

SKILLS = ["set_reminder"]
SKILL_DESCRIPTION = "定时提醒（相对/绝对时间）"

# 提醒到点后的回调（由 UI 层注入，参数: 提醒内容）
_reminder_callback = None
# 进行中的提醒列表（任务页展示用）
_active_reminders = []


def _set_callback(cb):
    """skills.__init__ 转发用"""
    global _reminder_callback
    _reminder_callback = cb


def set_reminder_callback(callback):
    """UI 层注入提醒触发回调（线程安全：回调内部自行切线程）"""
    global _reminder_callback
    _reminder_callback = callback


# 匹配两种语序（数字支持阿拉伯与中文数字）
_NUM = r'([\d一二两三四五六七八九十半]+)'
_UNIT = r'\s*(分钟|个小时|小时|分)\s*[之以]?[后过]?'
_REMINDER_PATTERNS = [
    _NUM + _UNIT + r'提醒我.{0,4}?(.+)?$',
    r'提醒我.{0,6}?' + _NUM + _UNIT + r'.{0,4}?(.+)?$',
]

_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _parse_num(s: str):
    if s.isdigit():
        return int(s)
    if s == "半":
        return 0.5
    if "十" in s:
        ten, _, one = s.partition("十")
        if (ten and ten not in _CN_DIGIT) or (one and one not in _CN_DIGIT):
            return None
        return (_CN_DIGIT.get(ten, 1) if ten else 1) * 10 + (_CN_DIGIT.get(one, 0) if one else 0)
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    return None


def _match_reminder(query: str):
    for pat in _REMINDER_PATTERNS:
        m = re.search(pat, query)
        if m:
            return m
    return None


@tool(
    name="set_reminder",
    description="定时提醒：X分钟后/小时后，或写明时间（明天下午3点、下周一9点）提醒做某事",
    patterns=_REMINDER_PATTERNS,
    keywords=["提醒", "提醒我", "叫我看", "叫我"],
)
def set_reminder(query: str) -> str:
    now = datetime.now()
    content = ""
    abs_res = _parse_abs_time(query, now)
    if abs_res:
        target, desc = abs_res
        # 提取事项：取最后一个「时间戳/动词/日期词」之后的文本（事项总在尾部）
        content = query
        last_pos = 0
        for mm in re.finditer(
            r'(?:\d{1,2}\s*[:：.\,点时]\s*\d{0,2}\s*分?'
            r'|(?:提醒|叫我看|叫我|记得)(?:我)?'
            r'|明天|后天|今天|今日)',
            content,
        ):
            last_pos = mm.end()
        content = content[last_pos:].strip("。，, .？?！!") or "时间到啦"
        _schedule_reminder(target, content, desc, "absolute")
        return f"（系统信息：已设好提醒，{desc}提醒用户「{content}」。请用达妮娅的语气答应下来）"

    m = _match_reminder(query)
    if m:
        amount = _parse_num(m.group(1))
        if amount:
            unit = m.group(2)
            content = (m.group(3) or "时间到啦").strip("。，, .？?！!")
            if unit in ("小时", "个小时"):
                seconds = amount * 3600
                disp, unit_cn = (int(amount * 60), "分钟") if amount < 1 else (int(amount), "小时")
            else:
                seconds = amount * 60
                disp, unit_cn = int(amount), "分钟"
            if 1 <= seconds <= 86400:
                target = now + timedelta(seconds=seconds)
                desc = f"{disp}{unit_cn}后"
                _schedule_reminder(target, content, desc, "relative")
                return f"（系统信息：已设好提醒，{desc}提醒用户「{content}」。请用达妮娅的语气答应下来）"

    if re.search(r'提醒|叫我看|叫我', query):
        return "（系统信息：没有解析出提醒的时间。请告诉用户没听清具体什么时候，让她说清楚时间或事项）"
    return None


def _parse_abs_time(query: str, now: datetime):
    s = query
    _WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 6}
    # 默认今天；只有显式说了「下周X」才用下周逻辑
    day_offset = 0
    if "明天" in s or "明日" in s:
        day_offset = 1
    elif "后天" in s:
        day_offset = 2
    m_wd = re.search(r'下([一二三四五六日天末])\s*[号]?周?([一二三四五六日天末])?', s)
    if m_wd and m_wd.group(1) in _WD:
        target_wd = _WD[m_wd.group(1)]
        today_wd = now.weekday()
        diff = (target_wd - today_wd) % 7
        day_offset = 7 + diff if diff < 7 else diff

    # 支持多种时间写法：
    # 1) 1点40分 / 1时40分 → 点+数字+分钟
    # 2) 1.40 / 1,40 / 1点40（语音识别常把「一点四十」识别成 1.40）
    # 3) 14:00 / 14点00 → 冒号/点+数字
    # 4) 1点 / 14点 → 纯小时
    hour = minute = None

    m2 = re.search(r'(\d{1,2})\s*[点时]\s*(\d{1,2})\s*分?', s)
    if m2:
        hour, minute = int(m2.group(1)), int(m2.group(2))
    else:
        m2 = re.search(r'(\d{1,2})\s*[.\,]\s*(\d{1,2})(?!\d)', s)
        if m2:
            # 1.40 = 1点40分；1.5 = 1点5分（.5 补零成 .05）
            hour, minute = int(m2.group(1)), int(m2.group(2))
        else:
            m2 = re.search(r'(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)', s)
            if m2:
                hour, minute = int(m2.group(1)), int(m2.group(2))
            else:
                m2 = re.search(r'(\d{1,2})\s*[点时]', s)
                if not m2:
                    return None
                hour, minute = int(m2.group(1)), 0

    # 范围校验：防止把 1.40（小数）之类误读成时间
    if hour is None or hour > 23 or (minute is not None and minute > 59):
        return None

    if "凌晨" in s:
        if hour >= 12:
            hour -= 12
    elif ("早上" in s or "早晨" in s or "上午" in s) and hour == 12:
        hour = 0
    elif ("下午" in s or "晚上" in s or "傍晚" in s or "晚间" in s) and hour < 12:
        hour += 12

    target = now + timedelta(days=day_offset)
    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    desc = f"{target.month}月{target.day}日{' ' if target.day != now.day or target.month != now.month else ''}{hour}点{minute:02d}分"
    return target, desc


def _schedule_reminder(target: datetime, content: str, desc: str, source: str) -> dict:
    delay = (target - datetime.now()).total_seconds()
    if delay < 1:
        delay = 1
    entry = {"content": content, "fire_at": target, "desc": desc, "source": source}
    _active_reminders.append(entry)

    def _fire():
        try:
            _active_reminders.remove(entry)
        except ValueError:
            pass
        if _reminder_callback:
            try:
                now = datetime.now()
                when = f"{now.year}年{now.month}月{now.day}日 {now.hour:02d}:{now.minute:02d}"
                _reminder_callback(f"{when} · {content}")
            except Exception as e:
                print(f"[工具] 提醒回调异常: {e}")

    timer = threading.Timer(delay, _fire)
    timer.daemon = True
    timer.start()
    return entry


def get_active_reminders() -> list:
    """供 UI 任务页/首页卡片展示：返回待触发的提醒（按触发时间排序）"""
    return sorted(_active_reminders, key=lambda r: r["fire_at"])


def fire_reminder_for_test(content: str):
    """测试用：立即触发提醒回调"""
    if _reminder_callback:
        _reminder_callback(content)


def register():
    """把本包的 _active_reminders 暴露给 main.py（保持旧访问路径）"""
    import sys
    _m = sys.modules.get("dania_tools")
    if _m is not None:
        _m._active_reminders = _active_reminders
