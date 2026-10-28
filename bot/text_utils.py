#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文本工具：繁简转换（懒加载 opencc，失败回退原文，不影响主流程）
         + 数量词抽取、城市名公共常量、默认城市读取
"""
import os
import re
from typing import Optional

_cc: Optional["object"] = None
_cc_loaded = False


def to_simplified(text: str) -> str:
    """繁转简。opencc 不可用时原样返回（不中断主流程）。"""
    if not text:
        return text
    global _cc, _cc_loaded
    if not _cc_loaded:
        _cc_loaded = True
        try:
            from opencc import OpenCC
            _cc = OpenCC("t2s")
        except Exception:
            _cc = None
    if _cc is None:
        return text
    try:
        return _cc.convert(text)
    except Exception:
        return text


def to_simplified_if_traditional(text: str) -> str:
    """检测文本是否含繁体字，是则转简，否则原样返回。

    用 opencc 转换前后是否相同来判断（比 marker 扫描更可靠，避免编码差异漏判）。
    """
    if not text:
        return text
    converted = to_simplified(text)
    return converted if converted != text else text


# ── 数量词抽取（image_search / x_image_search 共用）───────────────────

_CN_COUNT = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 「3张」「两张」——匹配数量+量词，供 re.sub 去掉数量词使用
COUNT_RE = r"(?:[0-9]+|[一二两三四五六七八九十])\s*[张个幅]"


def extract_count(text: str, default: int = 3, max_n: int = 10) -> int:
    """从文本提取数量（阿拉伯数字或中文数字），没写就用 default，上限 max_n"""
    m = re.search(COUNT_RE, text or "")
    if not m:
        return default
    raw = re.match(r"[0-9]+|[一二两三四五六七八九十]", m.group(0)).group(0)
    n = int(raw) if raw.isdigit() else _CN_COUNT.get(raw, default)
    return max(1, min(n, max_n))


# ── 城市（weather 技能 / denia_llm 检索词本地化共用）──────────────────

CITY_LIST = (
    "北京", "上海", "广州", "深圳", "天津", "重庆", "成都", "杭州", "武汉", "西安",
    "南京", "苏州", "长沙", "郑州", "青岛", "大连", "沈阳", "哈尔滨", "昆明", "厦门",
    "福州", "济南", "合肥", "南昌", "贵阳", "南宁", "兰州", "太原", "石家庄", "乌鲁木齐",
    "呼和浩特", "银川", "西宁", "拉萨", "海口", "三亚", "无锡", "宁波", "温州", "佛山",
    "东莞", "珠海", "中山", "惠州", "常州", "徐州", "烟台", "潍坊", "洛阳", "唐山", "保定",
)

_default_city_cache = None  # (settings_mtime, city)


def _looks_like_city(name: str) -> bool:
    """城市名合法性：2-6 个汉字（避免档案里混入整句口语被当成城市）"""
    return 2 <= len(name) <= 6 and bool(re.fullmatch(r"[\u4e00-\u9fa5]+", name))


def get_default_city() -> str:
    """默认城市：用户档案「城市」优先，回退 settings.json 的 weather_city（带 mtime 缓存）"""
    global _default_city_cache
    try:
        from user_profile import load_profile
        city = (load_profile().get("城市") or "").strip()
        if _looks_like_city(city):
            return city
    except Exception:
        pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    settings_path = os.path.join(root, "settings.json")
    try:
        mtime = os.path.getmtime(settings_path)
    except Exception:
        mtime = None
    if _default_city_cache is not None and _default_city_cache[0] == mtime:
        return _default_city_cache[1]
    try:
        import json
        with open(settings_path, "r", encoding="utf-8") as f:
            city = (json.load(f).get("weather_city") or "").strip()
    except Exception:
        city = ""
    _default_city_cache = (mtime, city)
    return city
