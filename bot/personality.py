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
        
        # 对话状态
        self._last_chat_time = time.time()
        self._active_lines = [
            "呼呼...你在忙什么呢？",
            "有点无聊呢...陪我说说话吧~",
            "（伸懒腰）你还好吗？",
            "今天过得怎么样呀？",
            "要不要聊聊什么呢？",
            "有点想找人说话呢...",
        ]
        
    def _load_data(self):
        """加载个性数据"""
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {
                "greetings": {
                    "morning": ["呼呼...早上好...好困啊...", "嗯...早安呢...", "哈啊——早上好...", "早上好呀~"],
                    "noon": ["中午好~", "要一起午睡吗？", "午饭吃了吗？", "中午好呀~"],
                    "afternoon": ["下午好呢~", "今天过得怎么样？", "下午好呀~"],
                    "evening": ["晚上好~", "今晚想吃什么？", "晚上好呀~"],
                    "night": ["夜深了呢...", "要早点休息哦...", "晚安呢..."]
                },
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
    
    def get_time_greeting(self) -> str:
        """根据时间返回问候语"""
        hour = datetime.now().hour
        
        if 5 <= hour < 9:
            return random.choice(self._data["greetings"]["morning"])
        elif 9 <= hour < 12:
            return random.choice(self._data["greetings"]["morning"])
        elif 12 <= hour < 14:
            return random.choice(self._data["greetings"]["noon"])
        elif 14 <= hour < 18:
            return random.choice(self._data["greetings"]["afternoon"])
        elif 18 <= hour < 22:
            return random.choice(self._data["greetings"]["evening"])
        else:
            return random.choice(self._data["greetings"]["night"])
    
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
    
    # ========== 话题延续 ==========
    
    def update_topic(self, user_input: str):
        """更新当前话题"""
        keywords = self._extract_keywords(user_input)
        if keywords:
            topic = "".join(keywords[:2])
            if topic and len(topic) >= 4 and topic not in self._topic_history:
                self._current_topic = topic
                self._topic_history.append(topic)
                self._topic_history = self._topic_history[-5:]
    
    def get_topic_continuation(self) -> Optional[str]:
        """获取话题延续提示（已禁用，避免回答过于发散）"""
        return None
    
    def _extract_keywords(self, text: str) -> list:
        """从文本中提取关键词"""
        import re
        keywords = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
        common_words = {
            "什么", "怎么", "为什么", "你", "我", "他", "她", "它", 
            "这个", "那个", "今天", "明天", "昨天", "差不多", "还行",
            "是的", "不是", "好的", "对的", "嗯", "哦", "啊", "呢",
            "呢", "呀", "哦", "嗯", "哈", "哈哈", "嘿嘿", "呵呵",
            "有", "没有", "要", "不要", "可以", "不可以", "好", "不好"
        }
        return [k for k in keywords if k not in common_words and len(k) >= 2]
    
    # ========== 主动互动 ==========
    
    def update_chat_time(self):
        """更新最后对话时间"""
        self._last_chat_time = time.time()
    
    def should_initiate_chat(self, idle_timeout: int = 300) -> bool:
        """判断是否应该主动发起对话"""
        now = time.time()
        idle_time = now - self._last_chat_time
        if idle_time >= idle_timeout:
            # 30% 概率主动搭话
            return random.random() < 0.3
        return False
    
    def get_initiation_line(self) -> str:
        """获取主动搭话的句子"""
        return random.choice(self._active_lines)
    
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