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


# WMO weather_code → 中文描述：Open-Meteo 备用源使用（与 weather.py 的 WMO_CODE 同步）
_WMO_CODE = {
    0: "晴", 1: "大部晴朗", 2: "局部多云", 3: "阴",
    45: "有雾", 48: "浓雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴冰雹",
}


def _open_meteo_text(city: str) -> str:
    """Open-Meteo 备用源：wttr.in 连不上时仍能回答，输出格式与 wttr 版一致"""
    city = (city or _load_weather_city()).strip()
    if not city:
        raise ValueError("未配置城市")
    geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
               + urllib.parse.urlencode(
                   {"name": city, "count": 1, "language": "zh", "format": "json"}))
    with urllib.request.urlopen(
            urllib.request.Request(geo_url, headers={"User-Agent": "curl/8"}), timeout=8) as resp:
        geo = json.load(resp)
    hit = (geo.get("results") or [None])[0]
    if not hit:
        raise ValueError("找不到城市：%s" % city)

    params = {
        "latitude": hit["latitude"],
        "longitude": hit["longitude"],
        "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "weather_code,wind_speed_10m"),
        "hourly": "weather_code",
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1,
        "timezone": "auto",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "curl/8"}), timeout=8) as resp:
        data = json.load(resp)
    cur = data.get("current") or {}
    temp = cur.get("temperature_2m")
    if temp is None:
        raise ValueError("天气数据为空")

    code = int(cur.get("weather_code") or 0)
    desc = _WMO_CODE.get(code, "未知")
    city_name = city or hit.get("name") or "当地"
    if desc == "未知":
        head = "%s当前气温%s°C" % (city_name, round(temp))
    else:
        head = "%s今天%s，当前气温%s°C" % (city_name, desc, round(temp))
    humidity = cur.get("relative_humidity_2m")
    if humidity is not None:
        head += "，湿度%s%%" % round(humidity)
    wind = cur.get("wind_speed_10m")
    if wind is not None:
        head += "，风速%s km/h" % round(wind)

    hourly = data.get("hourly") or {}
    codes = hourly.get("weather_code") or []
    times = hourly.get("time") or []
    hits = []
    for t, c in zip(times[:24], codes[:24]):
        d = _WMO_CODE.get(int(c), "未知")
        if "雨" in d or "雪" in d:
            hits.append(str(t).split("T")[-1] + d)

    extra = ""
    full_day_hint = ""
    if hits:
        extra = "；预计今天降雨雪时段：" + "、".join(hits)
        full_day_hint = "（今天有降雨/雪）"
    else:
        daily = data.get("daily") or {}
        maxt = (daily.get("temperature_2m_max") or [None])[0]
        mint = (daily.get("temperature_2m_min") or [None])[0]
        if maxt is not None and mint is not None:
            extra = "；今天最高%s°C、最低%s°C，全天无降雨雪" % (round(maxt), round(mint))
        full_day_hint = "（今天无降雨雪）"

    return "（天气信息：%s%s%s。用达妮娅的语气自然回答，不要提及“未知/没拿到数据”）" % (
        head, extra, full_day_hint
    )


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
        # wttr.in 在部分网络下完全连不上（实测 Connection refused），改用 Open-Meteo
        try:
            return _open_meteo_text(city)
        except Exception as e2:
            return "（天气获取失败：%s；备用源也失败：%s）" % (e, e2)

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


# 全国降雪/降雨速查：主要城市坐标（Open-Meteo 支持一次请求传多组经纬度，返回数组）
_CITY_COORDS = {
    "哈尔滨": (45.80, 126.53), "长春": (43.88, 125.32), "沈阳": (41.80, 123.43),
    "大连": (38.91, 121.61), "呼和浩特": (40.84, 111.75), "乌鲁木齐": (43.83, 87.62),
    "北京": (39.90, 116.40), "天津": (39.13, 117.20), "石家庄": (38.04, 114.51),
    "太原": (37.87, 112.55), "济南": (36.65, 117.12), "青岛": (36.07, 120.38),
    "郑州": (34.75, 113.62), "西安": (34.34, 108.94), "兰州": (36.06, 103.83),
    "西宁": (36.62, 101.78), "银川": (38.48, 106.23), "拉萨": (29.65, 91.12),
    "成都": (30.57, 104.06), "重庆": (29.56, 106.55), "武汉": (30.59, 114.30),
    "南京": (32.06, 118.79), "合肥": (31.82, 117.22), "上海": (31.23, 121.47),
    "杭州": (30.27, 120.15), "南昌": (28.68, 115.86), "长沙": (28.22, 112.93),
    "贵阳": (26.65, 106.63), "昆明": (25.04, 102.72), "福州": (26.07, 119.29),
    "广州": (23.13, 113.26), "深圳": (22.54, 114.05), "南宁": (22.82, 108.32),
    "海口": (20.04, 110.32),
}

_SNOW_CODES = {71, 73, 75, 77, 85, 86}
_RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}

# 「哪些地方下雪 / 全国哪里下雨」这类区域性提问
_NATIONAL_HINTS = ("哪些地方", "哪些城市", "哪里", "哪儿", "什么地方", "全国", "哪边")


def _wants_national(query: str) -> bool:
    """问题是否在问「全国范围内哪里在下雪/下雨」"""
    q = query or ""
    if not any(k in q for k in ("雪", "雨")):
        return False
    return any(k in q for k in _NATIONAL_HINTS)


def _national_precip_text() -> str:
    """全国主要城市「哪里在下雪/下雨」速查（一次请求查完 34 个城市）"""
    lats = ",".join("%.2f" % v[0] for v in _CITY_COORDS.values())
    lons = ",".join("%.2f" % v[1] for v in _CITY_COORDS.values())
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lats,
        "longitude": lons,
        "current": "temperature_2m,weather_code",
        "timezone": "Asia/Shanghai",
    })
    with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "curl/8"}), timeout=10) as resp:
        data = json.load(resp)
    rows = data if isinstance(data, list) else [data]

    snow, rain = [], []
    for name, row in zip(_CITY_COORDS, rows):
        cur = (row or {}).get("current") or {}
        code = int(cur.get("weather_code") or 0)
        temp = cur.get("temperature_2m")
        desc = _WMO_CODE.get(code, "")
        temp_txt = ("%d°C" % round(temp)) if temp is not None else ""
        if code in _SNOW_CODES:
            snow.append("%s（%s%s）" % (name, desc, ("，" + temp_txt) if temp_txt else ""))
        elif code in _RAIN_CODES:
            rain.append("%s（%s）" % (name, desc))

    if snow:
        body = "正在下雪的城市：" + "、".join(snow)
        if rain:
            body += "；正在下雨的城市：" + "、".join(rain[:8])
    elif rain:
        body = "全国主要城市当前没有降雪；正在下雨的城市：" + "、".join(rain[:10])
        if len(rain) > 10:
            body += " 等"
    else:
        body = "全国主要城市当前都没有降雪或降雨"

    return ("（天气信息：%s。这是全国 34 个主要城市的实时监测，回答时请列举具体城市，"
            "不要提及数据来源或“没查到”）" % body)


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
    description="查询天气、气温、下雨、下雪等实时天气信息；问「哪些地方/全国哪里在下雪或下雨」时返回具体城市列表",
    keywords=["天气", "气温", "温度", "下雨", "下雪", "降雨", "降雪",
              "刮风", "降温", "升温", "台风", "暴雨", "天气预报", "起雾",
              "哪些地方", "哪些城市", "哪里下", "全国天气"],
)
def query_weather(query: str) -> str:
    # 区域性提问（「今天哪些地方下雪了」）走全国速查；问题里点明了城市时仍查该城市
    city = _city_from_query(query)
    if not city and _wants_national(query):
        try:
            return _national_precip_text()
        except Exception:
            pass
    return _fetch_weather_text(city or _load_weather_city())
