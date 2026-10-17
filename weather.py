#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天气模块 - wttr.in 免费接口（无需 key，按 IP 定位城市）

后台线程定时拉取，结果通过信号推给 UI；latest() 供界面首次显示时同步取缓存。
"""
import json
import threading
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, Signal, QTimer

# WWO weatherCode → (中文描述, emoji 图标)
WEATHER_CODE = {
    113: ("晴", "☀"),
    116: ("多云", "⛅"),
    119: ("阴", "☁"),
    122: ("阴", "☁"),
    125: ("局部多云", "⛅"),
    143: ("有雾", "🌫"),
    149: ("霾", "🌫"),
    176: ("小雨", "🌦"),
    179: ("阵雪", "🌨"),
    182: ("雨夹雪", "🌨"),
    185: ("冻雨", "🌧"),
    200: ("雷雨", "⛈"),
    227: ("小雪", "🌨"),
    230: ("大雪", "❄"),
    248: ("有雾", "🌫"),
    260: ("浓雾", "🌫"),
    263: ("毛毛雨", "🌦"),
    266: ("毛毛雨", "🌦"),
    281: ("冻雨", "🌧"),
    284: ("冻雨", "🌧"),
    293: ("小雨", "🌦"),
    296: ("小雨", "🌧"),
    299: ("中雨", "🌧"),
    302: ("中雨", "🌧"),
    305: ("大雨", "🌧"),
    308: ("大雨", "🌧"),
    311: ("大雨", "🌧"),
    314: ("大雨", "🌧"),
    317: ("阵雨", "🌧"),
    320: ("阵雪", "🌨"),
    323: ("小雪", "🌨"),
    326: ("小雪", "🌨"),
    329: ("中雪", "❄"),
    332: ("中雪", "❄"),
    335: ("大雪", "❄"),
    338: ("大雪", "❄"),
    350: ("冻雨", "🌧"),
    353: ("阵雨", "🌦"),
    356: ("阵雨", "🌧"),
    359: ("暴雨", "⛈"),
    362: ("雨夹雪", "🌨"),
    365: ("雨夹雪", "🌨"),
    368: ("阵雪", "🌨"),
    371: ("阵雪", "❄"),
    374: ("冻雨夹雪", "🌨"),
    377: ("冻雨夹雪", "🌨"),
    386: ("雷阵雨", "⛈"),
    389: ("雷雨夹雪", "⛈"),
    392: ("雷阵雪", "⛈"),
    395: ("雷阵雪", "⛈"),
}

_REFRESH_MS = 10 * 60 * 1000  # 10 分钟轮询（wttr.in 数据源本身约 30 分钟更新）
_THROTTLE_S = 300             # 手动触发时 5 分钟内不重复请求
_FIRST_DELAY_MS = 3000


class WeatherWorker(QObject):
    """定时拉取天气，结果经 weather_ready 信号广播"""

    weather_ready = Signal(dict)

    def __init__(self, city: str = ""):
        super().__init__()
        self.city = (city or "").strip()
        self._latest = None
        self._busy = False
        self._last_try = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)

    def start(self):
        QTimer.singleShot(_FIRST_DELAY_MS, self.refresh)
        self._timer.start(_REFRESH_MS)

    def latest(self):
        """最近一次天气（dict 或 None），供界面打开时立即显示"""
        return self._latest

    def refresh(self, force: bool = False):
        """拉取天气；force=True 跳过节流（定时器用），否则 5 分钟内不重复请求"""
        if self._busy:
            return
        if not force and time.time() - self._last_try < _THROTTLE_S:
            return
        self._last_try = time.time()
        self._busy = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            if self.city:
                url = "https://wttr.in/%s?format=j1&lang=zh" % urllib.parse.quote(self.city)
            else:
                url = "https://wttr.in/?format=j1&lang=zh"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.load(resp)

            cur = data["current_condition"][0]
            code = int(cur.get("weatherCode", 0))
            # 天气描述完全本地化：优先 weatherCode 中文映射，未知代码从 weatherDesc 提取
            desc, icon = WEATHER_CODE.get(code)
            if not desc:
                raw_desc = cur.get("weatherDesc", [{}])[0].get("value", "")
                desc = raw_desc[:8] if raw_desc else "未知"
                icon = "🌡"
            area = data.get("nearest_area", [{}])[0]
            # 固定城市时直接显示配置的城市名（wttr.in 返回的可能是最近站点名）
            city_name = self.city or area.get("areaName", [{}])[0].get("value", "")

            info = {
                "temp": cur.get("temp_C", "--"),
                "feels": cur.get("FeelsLikeC", "--"),
                "desc": desc,
                "icon": icon,
                "code": code,
                "humidity": cur.get("humidity", "--"),
                "wind": cur.get("windspeedKmph", "--"),
                "city": city_name,
                "ts": int(time.time()),
            }
            self._latest = info
            self.weather_ready.emit(info)
        except Exception as e:
            print("[天气] 获取失败: %s" % e)
            # 拉取失败也保留城市名 + 占位数据，前端胶囊不显示"未知"
            if self.city:
                fallback = {"temp": "--", "feels": "--", "desc": "获取中", "icon": "🌡",
                            "humidity": "--", "wind": "--", "city": self.city, "ts": 0}
                self._latest = fallback
                self.weather_ready.emit(fallback)
        finally:
            self._busy = False
