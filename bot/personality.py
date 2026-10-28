"""
角色个性系统 - 让达妮娅更生动

功能：
1. 时间感知 - 根据时间改变回复风格
2. 天气感知 - 根据天气调整语气
3. 话题延续 - 记住上一轮话题并延续
4. 主动互动 - 空闲时主动搭话
"""
import os
import json
import time
import random
from datetime import datetime
from typing import Optional, Dict


class PersonalitySystem:
    """角色个性系统"""

    def __init__(self):
        self._data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "personality.json")
        self._load_data()

        # 当前话题追踪
        self._current_topic = ""
        self._topic_history = []
        # 话题持久化（跨重启继承，供"帮我搜"等短 query 继承上一轮话题）
        self._topic_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "last_topic.json")
        self._load_persisted_topic()
        # 话题写盘防抖（高频 update_topic 只落盘一次）
        self._topic_save_timer = None
        
        # 对话状态（仅用于记录，不再驱动主动搭话）
        self._last_chat_time = time.time()
        
    def _load_data(self):
        """加载个性数据"""
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {
                "greetings": {},
                "weather_reactions": {
                    "sunny": ["天气真好呀！", "阳光好温暖呢~", "适合出门走走呢~"],
                    "rainy": ["下雨天适合睡觉呢...", "雨声真好听...", "记得带伞哦~"],
                    "cloudy": ["阴天呢...", "天气有点阴沉呢..."],
                    "snowy": ["下雪了呢！", "好漂亮的雪呀~", "注意保暖哦~"],
                    "hot": ["好热呀...", "好想喝冷饮...", "注意防暑哦~"],
                    "cold": ["好冷呀...", "要多穿点哦~", "好想待在温暖的房间里..."]
                }
            }
    
    def _save_data(self):
        """保存个性数据"""
        os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
        with open(self._data_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
    
    # ========== 时间感知 ==========
    
    def get_time_hint(self) -> str:
        """生成时间相关提示词"""
        hour = datetime.now().hour
        hints = []
        
        if 6 <= hour < 9:
            hints.append("现在是早晨，达妮娅可能还没睡醒，语气慵懒")
        elif 12 <= hour < 14:
            hints.append("现在是中午，达妮娅可能想午睡")
        elif 22 <= hour:
            hints.append("现在是深夜，达妮娅可能犯困")
        
        return "\n".join(hints)
    
    # ========== 天气感知 ==========
    
    def set_weather(self, weather_info: str):
        """设置当前天气信息"""
        self._current_weather = weather_info
    
    def get_weather_reaction(self) -> str:
        """根据天气返回反应"""
        if not hasattr(self, "_current_weather"):
            return ""
        
        weather = self._current_weather.lower()
        
        if "晴" in weather or "太阳" in weather:
            return random.choice(self._data["weather_reactions"]["sunny"])
        elif "雨" in weather:
            return random.choice(self._data["weather_reactions"]["rainy"])
        elif "阴" in weather:
            return random.choice(self._data["weather_reactions"]["cloudy"])
        elif "雪" in weather:
            return random.choice(self._data["weather_reactions"]["snowy"])
        elif "热" in weather or "高温" in weather:
            return random.choice(self._data["weather_reactions"]["hot"])
        elif "冷" in weather or "低温" in weather:
            return random.choice(self._data["weather_reactions"]["cold"])
        
        return ""
    
    def _load_persisted_topic(self):
        """启动时从上次的持久化文件恢复话题"""
        try:
            if os.path.exists(self._topic_file):
                with open(self._topic_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._current_topic = data.get("topic", "") or ""
                    self._topic_history = data.get("history", []) or []
                    if self._current_topic:
                        print(f"[话题] 恢复上一轮话题: {self._current_topic}")
        except Exception as e:
            print(f"[话题] 恢复话题失败: {e}")

    def _save_persisted_topic(self):
        """把当前话题 + 历史持久化到磁盘（跨重启继承）"""
        try:
            os.makedirs(os.path.dirname(self._topic_file), exist_ok=True)
            with open(self._topic_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"topic": self._current_topic, "history": self._topic_history},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            print(f"[话题] 保存话题失败: {e}")

    # ========== 话题延续 ==========

    def update_topic(self, user_input: str):
        """更新当前话题"""
        # 纯确认词/回应词（"可以"、"好的"、"嗯"等）不更新话题，避免污染话题继承
        import re
        _CONFIRM_ONLY = re.compile(
            r'^[，。！!??\s]*(可以|好的|好的呀|好呀|行|嗯|嗯嗯|对|对的|是的|没问题|知道了|了解|收到|明白|明白啦|好嘞|ok|OK)[，。！!??\s]*$'
        )
        if _CONFIRM_ONLY.match(user_input.strip()):
            return
        keywords = self._extract_keywords(user_input)
        if keywords:
            topic = "".join(keywords[:2])
            if topic and len(topic) >= 4 and topic not in self._topic_history:
                self._current_topic = topic
                self._topic_history.append(topic)
                self._topic_history = self._topic_history[-5:]
                self._schedule_topic_save()

    def _schedule_topic_save(self):
        """防抖写盘：1.5s 内多次 update_topic 只落盘一次"""
        import threading
        if self._topic_save_timer is not None:
            self._topic_save_timer.cancel()
        self._topic_save_timer = threading.Timer(1.5, self._save_persisted_topic)
        self._topic_save_timer.daemon = True
        self._topic_save_timer.start()
    
    def get_topic_continuation(self) -> Optional[str]:
        """获取话题延续提示（已禁用，避免回答过于发散）"""
        return None

    def get_last_topic(self) -> str:
        """取上一轮记住的话题（纯指令型短 query 时用来继承检索实体）"""
        return self._current_topic or ""

    def _extract_keywords(self, text: str) -> list:
        """从文本中提取关键词"""
        import re
        keywords = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
        common_words = {
            "什么", "怎么", "为什么", "你", "我", "他", "她", "它", 
            "这个", "那个", "今天", "明天", "昨天", "差不多", "还行",
            "是的", "不是", "好的", "对的", "嗯", "哦", "啊", "呢",
            "呢", "呀", "哦", "嗯", "哈", "哈哈", "嘿嘿", "呵呵",
            "有", "没有", "要", "不要", "可以", "不可以", "好", "不好",
            "现在", "时候", "一下", "一点", "东西", "时候", "这么",
            "那么", "真的", "觉得", "知道", "应该", "需要", "能够",
            "能够", "其实", "因为", "所以", "但是", "然后", "这样",
            "那样", "只是", "只是", "已经", "仍然", "还是", "或者",
            "还是", "以及", "对于", "关于", "通过", "进行", "使用",
            "使用", "提供", "表示", "说明", "意义", "内容", "问题",
            "问题", "事情", "方面", "情况", "方式", "方法", "可能",
            "可能", "比较", "比较", "非常", "特别", "十分", "相当",
        }
        return [k for k in keywords if k not in common_words and len(k) >= 2]
    
    # ========== 综合提示 ==========
    
    def get_personality_prompt(self) -> str:
        """生成个性相关提示词"""
        parts = []
        
        # 时间提示
        time_hint = self.get_time_hint()
        if time_hint:
            parts.append(f"【时间】{time_hint}")
        
        # 天气反应
        weather_reaction = self.get_weather_reaction()
        if weather_reaction:
            parts.append(f"【天气】{weather_reaction}")
        
        return "\n".join(parts) if parts else ""


# 单例
_personality = None


def get_personality_system() -> PersonalitySystem:
    global _personality
    if _personality is None:
        _personality = PersonalitySystem()
    return _personality