# -*- coding: utf-8 -*-
"""
官方资源优先搜索模块
优先从游戏官网、官方wiki等可信源获取角色和游戏信息
"""

import re
from typing import Optional, Dict, List, Tuple


class OfficialSourceSearch:
    """游戏官方信息搜索"""
    
    # 各游戏的官方资源
    OFFICIAL_SOURCES = {
        "鸣潮": {
            "wiki": "https://wiki.kurogame.com/",
            "official": "https://www.kurogame.com/",
            "search_pattern": "鸣潮wiki {keyword}",
            "priority": 1,
        },
        "原神": {
            "wiki": "https://ys.mihoyo.com/",
            "official": "https://ys.mihoyo.com/",
            "search_pattern": "原神官方 {keyword}",
            "priority": 1,
        },
        "崩坏星穹铁道": {
            "wiki": "https://sr.mihoyo.com/",
            "official": "https://sr.mihoyo.com/",
            "search_pattern": "星穹铁道官方 {keyword}",
            "priority": 1,
        },
        "绝区零": {
            "wiki": "https://zenless.mihoyo.com/",
            "official": "https://zenless.mihoyo.com/",
            "search_pattern": "绝区零官方 {keyword}",
            "priority": 1,
        },
    }
    
    # 游戏内角色本地信息库（准确的参考信息）
    CHARACTER_INFO = {
        "鸣潮": {
            "达妮娅": {
                "weapon": "法杖",
                "element": "幽能",
                "rarity": "5星",
                "type": "声骸",
                "description": "鸣潮的5星角色，使用法杖战斗，属于幽能系角色"
            },
            "卡提西亚": {
                "weapon": "迅刃",
                "element": "频域",
                "rarity": "5星",
                "type": "声骸",
                "description": "鸣潮的5星角色，使用迅刃战斗，属于频域系角色"
            },
            "罗隐": {
                "weapon": "长枪",
                "element": "频域",
                "rarity": "5星",
                "type": "声骸",
                "description": "鸣潮的5星角色，使用长枪战斗"
            },
        },
    }
    
    def __init__(self):
        pass
    
    def get_official_search_query(self, keyword: str, game: str) -> Tuple[str, str]:
        """
        获取官方搜索查询
        返回: (搜索引擎查询, 搜索源提示)
        """
        if game in self.OFFICIAL_SOURCES:
            source = self.OFFICIAL_SOURCES[game]
            search_query = source["search_pattern"].format(keyword=keyword)
            return search_query, f"从{game}官方资源搜索"
        
        return f"{game} {keyword} 官方", "从官方资源搜索"
    
    def get_character_info(self, character_name: str, game: str) -> Optional[Dict]:
        """
        从本地知识库获取角色准确信息
        这是可靠的参考，优于网络搜索
        """
        if game in self.CHARACTER_INFO:
            if character_name in self.CHARACTER_INFO[game]:
                return self.CHARACTER_INFO[game][character_name]
        
        return None
    
    def detect_character_query(self, text: str, game: str) -> Optional[str]:
        """
        检测是否是角色相关查询
        返回检测到的角色名称，如果没有则返回None
        """
        # 常见的角色查询模式
        patterns = [
            r"(.*?)的武器",
            r"(.*?)的属性",
            r"(.*?)的技能",
            r"介绍一下(.*?)(?:[呀吗吧？]|$)",
            r"(.*?)怎么样",
            r"(.*?)是什么",
            r"(.*?)用什么武器",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                character_name = match.group(1).strip()
                # 检查这个角色是否存在于知识库
                if character_name in self.CHARACTER_INFO.get(game, {}):
                    return character_name
        
        return None
    
    def format_character_info_response(self, char_name: str, char_info: Dict, game: str) -> str:
        """
        格式化角色信息为自然的回复
        """
        lines = [
            f"{char_name}是{game}的角色呢~",
            f"武器：{char_info.get('weapon', '未知')}",
            f"属性：{char_info.get('element', '未知')}",
            f"稀有度：{char_info.get('rarity', '未知')}",
        ]
        
        if char_info.get('description'):
            lines.append(f"描述：{char_info['description']}")
        
        return " ".join(lines)


def create_official_search_provider():
    """创建官方搜索提供者"""
    return OfficialSourceSearch()
