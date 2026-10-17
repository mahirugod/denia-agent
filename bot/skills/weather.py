#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：天气（weather）
查询实时天气、气温、降雨/雪时段。数据源 wttr.in j1，本地零依赖。
"""
import json
import os
import urllib.parse
import urllib.request

from skills import skill
from tool_registry import tool

skill("weather", "查询天气/气温/降雨时段", tools=["query_weather"])

# 暴露给 skills 包（供 _META 注册）
SKILLS = ["query_weather"]
SKILL_DESCRIPTION = "查询天气/气温/降雨时段"


def _load_weather_city() -> str:
    """从项目根 settings.json 读取 weather_city（与主程序天气卡片保持一致）"""
    try:
        # bot/skills/weather.py -> skills -> bot -> 项目根（3 层）
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "settings.json"), "r", encoding="utf-8") as f:
            return (json.load(f).get("weather_city") or "").strip()
    except Exception:
        return ""


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

    head = "%s当前%s，气温%s°C" % (city_name, cur_desc, temp)
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
        full_day_hint = "（注：今天预计有降雨/雪）"
    else:
        full_day_hint = "（注：今天全天无降雨雪，是晴天）"

    return "（天气信息：%s%s%s。请用达妮娅的语气自然回答，根据资料判断是否晴天/是否需要带伞）" % (
        head, extra, full_day_hint
    )


@tool(
    name="query_weather",
    description="查询天气、气温、下雨、下雪等实时天气信息",
    keywords=["天气", "气温", "温度", "下雨", "下雪", "降雨", "降雪",
              "刮风", "降温", "升温", "台风", "暴雨", "天气预报", "起雾"],
)
def query_weather(query: str) -> str:
    return _fetch_weather_text(_load_weather_city())
