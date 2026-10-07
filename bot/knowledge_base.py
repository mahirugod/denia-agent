"""
知识库检索模块
自动根据用户输入检索相关游戏知识，注入到对话 prompt 中
"""
import os
import re
from typing import Optional

# 知识库文件目录（相对于本文件所在目录）
KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")

# 关键词 → 知识库文件映射
KEYWORD_MAP = {
    "wuthering_waves.txt": [
        "鸣潮", "漂泊者", "索拉里斯", "悲鸣", "声骸", "共鸣者",
        "卡卡罗", "安可", "维里奈", "凌阳", "长离", "今汐", "吟霖",
        "忌炎", "鉴心", "桃祈", "秧秧", "炽霞", "散华", "白芷",
        "绯雪", "秋水", "丹瑾", "莫特斐", "渊武", "相里要", "釉瑚", "折枝",
        "守岸人", "灯灯", "椿", "珂莱塔", "洛可可", "菲比", "布兰特",
        "坎特蕾拉", "赞妮", "夏空", "西格莱卡", "爱弥斯", "莫宁", "琳奈", "千咲",
        "星炬学院", "虚质科学",
        "气动", "导电", "冷凝", "热熔", "衍射", "湮灭",
        "长刃", "臂铠", "迅刀", "佩枪", "音感仪",
        "飞廉之猩", "鸣钟之龟", "协奏", "共鸣结晶", "数据坞",
        "今州", "无音区", "七丘",
    ],
    "dania_scenarios.txt": [
        # 达妮娅场景化反应指南
        "达妮娅", "丹妮娅", "DENIA", "denia",
        # 问候场景
        "早安", "早上好", "晚安", "你好", "嗨",
        # 情感场景
        "夸奖", "喜欢", "关心", "难过", "孤独", "困难",
        # 话题场景
        "过去", "西格莉卡", "西西", "莫宁", "飞行雪绒",
        "甜食", "蛋糕", "睡眠", "睡觉", "生肉",
        "虚无", "意义", "谎言", "真相", "幸福",
        # 战斗场景
        "战斗", "能力", "泡泡",
        # 跨次元场景
        "现实", "世界", "食物", "科技",
        # 特殊场景
        "生日", "道歉", "谢谢", "感谢",
    ],
    "dania_profile.txt": [
        # 达妮娅角色设定档案
        "达妮娅", "丹妮娅", "DENIA", "denia",
        # 基本信息
        "粉色", "蓝色", "星炬学院", "残星会", "学生",
        # 性格相关
        "性格", "温柔", "傲娇", "孤独", "疏离", "防备",
        # 能力相关
        "泡泡", "虚质", "防护", "能力",
        # 人际关系
        "西格莉卡", "西西", "莫宁", "飞行雪绒", "阿列夫一",
        # 喜好
        "喜欢", "讨厌", "甜食", "睡觉", "蛋糕", "生肉",
    ],
    "dania_dialogues.txt": [
        # 达妮娅官方台词样本
        "达妮娅", "丹妮娅", "DENIA", "denia",
        # 角色特质
        "睡觉", "睡眠", "午睡", "甜食", "蛋糕", "食物",
        "虚无", "谎言", "真相", "孤独", "幸福",
        # 相关角色
        "阿列夫一", "西格莉卡", "西西", "飞行雪绒", "莫宁", "苇原",
        "残星会", "星炬学院", "深空联合",
        # 常用语气
        "呼呼", "哈哈", "嗯", "呀", "呢", "哦", "嘛", "啦",
        # 战斗相关
        "战斗", "力量", "虚质", "泡泡",
        # 情感相关
        "信任", "防备", "朋友", "生日",
    ],
}


class KnowledgeBase:
    """本地知识库，按关键词命中后返回相关文本片段"""

    def __init__(self):
        self._cache: dict[str, str] = {}

    def _load_file(self, filename: str) -> str:
        if filename in self._cache:
            return self._cache[filename]
        path = os.path.join(KNOWLEDGE_DIR, filename)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self._cache[filename] = content
        return content

    def _match_file(self, text: str) -> Optional[str]:
        """根据用户输入判断命中哪个知识库文件，返回文件名或 None"""
        text_lower = text.lower()
        best_file = None
        best_score = 0
        for filename, keywords in KEYWORD_MAP.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > best_score:
                best_score = score
                best_file = filename
        return best_file if best_score > 0 else None

    def _extract_relevant(self, content: str, query: str, max_chars: int = 800) -> str:
        """从知识库内容中提取与查询最相关的段落"""
        # 按段落切分
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", content) if p.strip()]

        # 对每个段落打分：包含查询关键词越多得分越高
        query_words = list(query)  # 中文逐字匹配
        scored = []
        for p in paragraphs:
            score = sum(1 for w in query_words if w in p)
            scored.append((score, p))
        scored.sort(key=lambda x: -x[0])

        # 取得分最高的段落，凑够 max_chars
        result_parts = []
        total = 0
        for _, p in scored:
            if total >= max_chars:
                break
            result_parts.append(p)
            total += len(p)

        return "\n\n".join(result_parts[:5])  # 最多5段

    def query(self, user_input: str) -> Optional[str]:
        """
        根据用户输入查询知识库。
        若命中则返回相关文本（用于注入 prompt），否则返回 None。
        """
        filename = self._match_file(user_input)
        if not filename:
            return None
        content = self._load_file(filename)
        if not content:
            return None
        relevant = self._extract_relevant(content, user_input)
        if not relevant:
            return None
        # 给出来源标注
        source_name = {
            "wuthering_waves.txt": "鸣潮",
        }.get(filename, filename)
        return f"[知识库·{source_name}]\n{relevant}"


# 单例
_kb = KnowledgeBase()


def get_knowledge(user_input: str) -> Optional[str]:
    """对外接口：传入用户消息，返回知识片段或 None"""
    return _kb.query(user_input)
