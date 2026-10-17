#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户画像（User Profile）：为达妮娅提供关于"你"的持久化上下文。

画像文件位于 bot/data/user_profile.json，支持自动学习与手动编辑。
每次对话初始化时调用 load_profile() 获取画像文本，注入 system prompt。
"""
import json
import os
import re
import time
from datetime import datetime
from typing import Optional

_PROFILE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "user_profile.json"
)

# 各字段上限（防止越写越长撑爆上下文）
_FIELD_MAX = {
    "昵称": 20,
    "城市": 20,
    "工作学习": 40,
    "喜欢的游戏": 60,
    "喜欢的食物": 60,
    "常用语气词": 30,
    "作息习惯": 40,
    "兴趣话题": 60,
    "其他": 80,
}

# 学习关键词 → 目标字段映射
_LEARN_RULES = [
    # (正则, 目标字段, 提取模板)
    (r"我(住|在)\s*([一-龥]{2,10})", "城市", None),
    (r"我(?:喜欢|爱|好)[的]?(?:游戏)?是?\s*([一-龥a-zA-Z0-9，、\s]{1,40})", "喜欢的游戏", None),
    (r"我(?:喜欢|爱|好)[的]?(?:食物|甜品|甜点)?(?:是)?\s*([一-龥a-zA-Z0-9，、\s]{1,40})", "喜欢的食物", None),
    (r"我(?:是|在做|上|读)\s*([一-龥a-zA-Z0-9，、\s]{1,30})", "工作学习", None),
    (r"(?:称呼我|叫我|我叫)\s*([一-龥a-zA-Z0-9]{1,10})", "昵称", None),
    (r"我(?:的)?(?:作息|习惯)[是是]\s*([一-龥a-zA-Z0-9，、\s]{1,40})", "作息习惯", None),
]

# 用户确认语句（触发学习）
_CONFIRM_PATTERNS = re.compile(
    r"(?:对的|没错|是的|是呀|对呀|嗯对|确实|差不多)"
)


class UserProfile:
    """用户画像：读取 / 学习 / 注入"""

    def __init__(self):
        self._data = self._load_file()

    @staticmethod
    def _load_file() -> dict:
        try:
            if os.path.exists(_PROFILE_FILE):
                with open(_PROFILE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        # 默认空画像
        return {
            "昵称": "",
            "城市": "",
            "工作学习": "",
            "喜欢的游戏": "",
            "喜欢的食物": "",
            "常用语气词": "",
            "作息习惯": "",
            "兴趣话题": "",
            "其他": "",
            "_auto_learned": False,
            "_last_updated": "",
        }

    @staticmethod
    def _save_file(data: dict):
        os.makedirs(os.path.dirname(_PROFILE_FILE), exist_ok=True)
        data["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(_PROFILE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, key: str) -> str:
        return str(self._data.get(key, "")).strip()

    def set(self, key: str, value: str):
        if key not in _FIELD_MAX:
            return
        max_len = _FIELD_MAX[key]
        self._data[key] = value[:max_len]
        self._data["_auto_learned"] = True
        self._save_file(self._data)

    def build_injection_text(self) -> str:
        """生成注入 system prompt 的画像文本（≤ 200 字）"""
        parts = []
        if self.get("昵称"):
            parts.append(f"用户称呼：{self.get('昵称')}")
        if self.get("城市"):
            parts.append(f"用户所在城市：{self.get('城市')}（天气/时间类问题优先用这个城市）")
        if self.get("工作学习"):
            parts.append(f"用户从事：{self.get('工作学习')}")
        if self.get("喜欢的游戏"):
            parts.append(f"喜欢的游戏：{self.get('喜欢的游戏')}")
        if self.get("喜欢的食物"):
            parts.append(f"喜欢的食物：{self.get('喜欢的食物')}")
        if self.get("作息习惯"):
            parts.append(f"作息：{self.get('作息习惯')}")
        if self.get("兴趣话题"):
            parts.append(f"兴趣话题：{self.get('兴趣话题')}")
        if self.get("其他"):
            parts.append(f"其他：{self.get('其他')}")
        if not parts:
            return ""
        return "【用户画像】\n" + "；".join(parts) + "。"

    def try_learn_from_input(self, user_input: str) -> bool:
        """
        从用户输入中提取画像信息。
        返回 True 表示本次有新学习，画像已更新。
        """
        _updated = False
        _text = user_input.strip()

        # 规则匹配：城市
        m = re.search(r"我(?:住|在)\s*([一-龥]{2,10})", _text)
        if m and self.get("城市") != m.group(1):
            self.set("城市", m.group(1))
            _updated = True

        # 称呼
        m = re.search(r"(?:称呼我|叫我|我叫)\s*([一-龥a-zA-Z0-9]{1,10})", _text)
        if m and self.get("昵称") != m.group(1):
            self.set("昵称", m.group(1))
            _updated = True

        # 喜欢（游戏 / 食物 / 兴趣）
        m = re.search(r"我(?:喜欢|爱|好)\s*([一-龥a-zA-Z0-9，、\s]{1,40})", _text)
        if m:
            val = m.group(1).strip()
            # 判断是游戏还是食物（简单启发：含"原神/鸣潮/星穹/游戏"等 → 游戏，否则 → 兴趣）
            if re.search(r"(原神|鸣潮|星穹|游戏|王者|吃鸡|LOL|DOTA|塞尔达|动森|艾尔登法环)", val):
                if self.get("喜欢的游戏") != val:
                    self.set("喜欢的游戏", val)
                    _updated = True
            else:
                if self.get("喜欢的食物") != val:
                    self.set("喜欢的食物", val)
                    _updated = True

        # 作息
        m = re.search(r"我(?:一般|通常)?(?:早上|凌晨)\s*(\d{1,2})\s*点", _text)
        if m:
            hour = m.group(1)
            if "早上" in _text:
                self.set("作息习惯", f"早上{hour}点起床")
                _updated = True

        if _updated:
            self._data["_auto_learned"] = True
            self._save_file(self._data)
        return _updated


# 模块级单例
_profile_instance: Optional[UserProfile] = None


def load_profile() -> UserProfile:
    global _profile_instance
    if _profile_instance is None:
        _profile_instance = UserProfile()
    return _profile_instance


def get_injection_text() -> str:
    """获取画像注入文本（快速入口）"""
    return load_profile().build_injection_text()


def learn_from_input(user_input: str) -> bool:
    """从用户输入学习画像（快速入口）"""
    return load_profile().try_learn_from_input(user_input)
