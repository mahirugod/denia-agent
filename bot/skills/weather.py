#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：天气（weather）
查询实时天气、气温、降雨/雪时段。数据源 wttr.in j1，本地零依赖。
"""
import json
import re
import urllib.parse
import urllib.request

from skills import skill
from tool_registry import tool

skill("weather", "查询天气/气温/降雨时段", tools=["query_weather"])

# 暴露给 skills 包（供 _META 注册）
SKILLS = ["query_weather"]
SKILL_DESCRIPTION = "查询天气/气温/降雨时段"


# 默认城市读取统一在 text_utils（用户档案「城市」优先，回退 settings.weather_city）
from text_utils import CITY_LIST, get_default_city as _get_default_city


def _load_weather_city() -> str:
    """默认城市（用户档案优先，回退 settings.json）"""
    return _get_default_city()


def _fetch_weather_text(city: str = "") -> str:
    """同步抓 wttr.in j1，返回中文天气摘要（含今天逐小时降雨/雪时段）"""
    try:
        if city:
            url = "https://wttr.in/%s?format=j1&lang=zh" % urllib.parse.quote(city)
        else:
            url = "https://wttr.in/?format=j1&lang=zh"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
    except Exception as e:
        return "（天气获取失败：%s）" % e

    try:
        from weather import WEATHER_CODE
    except Exception:
        WEATHER_CODE = {}

    def _desc(code):
        return WEATHER_CODE.get(int(code), ("未知", ""))[0]

    cur = data.get("current_condition", [{}])[0]
    temp = cur.get("temp_C", "--")
    humidity = cur.get("humidity", "")
    wind = cur.get("windspeedKmph", "")
    cur_desc = _desc(cur.get("weatherCode", 0))

    city_name = city
    if not city_name:
        try:
            city_name = (data.get("nearest_area", [{}])[0]
                         .get("areaName", [{}])[0].get("value", ""))
        except Exception:
            city_name = "当地"

    if cur_desc in ("", "未知"):
        head = "%s当前气温%s°C" % (city_name, temp)
    else:
        head = "%s今天%s，当前气温%s°C" % (city_name, cur_desc, temp)
    if humidity:
        head += "，湿度%s%%" % humidity
    if wind:
        head += "，风速%s km/h" % wind

    extra = ""
    has_rain_all_day = False
    try:
        today = data.get("weather", [{}])[0]
        hourly = today.get("hourly", []) or []
        hits = []
        for h in hourly:
            d = _desc(h.get("weatherCode", 0))
            if "雨" in d or "雪" in d:
                try:
                    t = int(h.get("time", 0))
                    hh, mm = divmod(t, 100)
                    label = "%02d:%02d" % (hh, mm)
                except Exception:
                    label = str(h.get("time", ""))
                hits.append(label + d)
        if hits:
            has_rain_all_day = True
            extra = "；预计今天降雨雪时段：" + "、".join(hits)
        else:
            maxt = today.get("maxtempC", "")
            mint = today.get("mintempC", "")
            if maxt and mint:
                extra = "；今天最高%s°C、最低%s°C，全天无降雨雪" % (maxt, mint)
    except Exception:
        pass

    # 明确告知模型「全天是否晴天」，避免模型自行脑补
    full_day_hint = ""
    if has_rain_all_day:
        full_day_hint = "（今天有降雨/雪）"
    else:
        full_day_hint = "（今天无降雨雪）"

    return "（天气信息：%s%s%s。用达妮娅的语气自然回答，不要提及“未知/没拿到数据”）" % (
        head, extra, full_day_hint
    )


def _city_from_query(query: str) -> str:
    """从「北京今天天气怎么样」里抽出城市名；抽不出就返回空（回落到配置城市）。

    此前 query_weather 完全忽略入参，问任何城市都返回 settings.weather_city 的天气。
    """
    s = (query or "").strip()
    # 1) 直接命中已知城市表（最可靠，且与检索词本地化共用同一份城市名单）
    hit = next((c for c in CITY_LIST if c in s), "")
    if hit:
        return hit
    # 2) 剥离口语词后若剩下 2-4 字中文，视作城市名
    for w in ("查询", "查一下", "查查", "帮我", "请问", "看看", "今天", "明天", "后天",
              "现在", "目前", "最近", "天气", "气温", "温度", "怎么样", "咋样", "如何",
              "怎样", "什么", "啥", "多少度", "几度", "预报", "未来", "三天", "几天",
              "一周", "七天", "的", "呢", "吗", "啊", "呀", "情况",
              "？", "?", "，", ",", "。", "！", "!", " ", "\t"):
        s = s.replace(w, "")
    s = s.strip()
    if 2 <= len(s) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5]+", s):
        return s
    return ""


@tool(
    name="query_weather",
    description="查询天气、气温、下雨、下雪等实时天气信息",
    keywords=["天气", "气温", "温度", "下雨", "下雪", "降雨", "降雪",
              "刮风", "降温", "升温", "台风", "暴雨", "天气预报", "起雾"],
)
def query_weather(query: str) -> str:
    return _fetch_weather_text(_city_from_query(query) or _load_weather_city())
