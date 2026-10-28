"""
知识库检索模块
自动根据用户输入检索相关游戏知识，注入到对话 prompt 中
"""

import json
import os
import re
from typing import Optional

# 知识库文件目录（相对于本文件所在目录）
KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")
# 书本笔记目录（book_intake 拆书后写在这里，作为动态知识源）
BOOKS_DIR = os.path.join(os.path.dirname(__file__), "books")

# 关键词 → 知识库文件映射
KEYWORD_MAP = {
    "wuthering_waves.txt": [
        "鸣潮",
        "漂泊者",
        "索拉里斯",
        "悲鸣",
        "声骸",
        "共鸣者",
        "卡卡罗",
        "安可",
        "维里奈",
        "凌阳",
        "长离",
        "今汐",
        "吟霖",
        "忌炎",
        "鉴心",
        "桃祈",
        "秧秧",
        "炽霞",
        "散华",
        "白芷",
        "绯雪",
        "秋水",
        "丹瑾",
        "莫特斐",
        "渊武",
        "相里要",
        "釉瑚",
        "折枝",
        "守岸人",
        "灯灯",
        "椿",
        "珂莱塔",
        "洛可可",
        "菲比",
        "布兰特",
        "坎特蕾拉",
        "赞妮",
        "夏空",
        "西格莱卡",
        "爱弥斯",
        "莫宁",
        "琳奈",
        "千咲",
        "星炬学院",
        "虚质科学",
        "气动",
        "导电",
        "冷凝",
        "热熔",
        "衍射",
        "湮灭",
        "长刃",
        "臂铠",
        "迅刀",
        "佩枪",
        "音感仪",
        "飞廉之猩",
        "鸣钟之龟",
        "协奏",
        "共鸣结晶",
        "数据坞",
        "今州",
        "无音区",
        "七丘",
        # 2026-10 更新：3.4~3.7 新增地区/角色/名词
        "梦州",
        "玄方",
        "玄方城",
        "玄元境",
        "梦枢天罗",
        "心之集域",
        "夜之城",
        "岁主",
        "禁锁十契",
        "谛天鉴",
        "锁暝",
        "瑝珑",
        "复刻",
        "残星会",
        "深空联合",
        "新角色",
        "新地图",
        "版本更新",
        "3.3",
        "3.4",
        "3.5",
        "3.6",
        "3.7",
        "3.8",
    ],
    "dania_scenarios.txt": [
        # 达妮娅场景化反应指南
        "达妮娅",
        "丹妮娅",
        "DENIA",
        "denia",
        # 问候场景
        "早安",
        "早上好",
        "晚安",
        "你好",
        "嗨",
        # 情感场景
        "夸奖",
        "喜欢",
        "关心",
        "难过",
        "孤独",
        "困难",
        # 话题场景
        "过去",
        "西格莉卡",
        "西西",
        "莫宁",
        "飞行雪绒",
        "甜食",
        "蛋糕",
        "睡眠",
        "睡觉",
        "生肉",
        "虚无",
        "意义",
        "谎言",
        "真相",
        "幸福",
        # 战斗场景
        "战斗",
        "能力",
        "泡泡",
        # 跨次元场景
        "现实",
        "世界",
        "食物",
        "科技",
        # 特殊场景
        "生日",
        "道歉",
        "谢谢",
        "感谢",
    ],
    "dania_profile.txt": [
        # 达妮娅角色设定档案
        "达妮娅",
        "丹妮娅",
        "DENIA",
        "denia",
        # 基本信息
        "粉色",
        "蓝色",
        "星炬学院",
        "残星会",
        "学生",
        # 性格相关
        "性格",
        "温柔",
        "傲娇",
        "孤独",
        "疏离",
        "防备",
        # 能力相关
        "泡泡",
        "虚质",
        "防护",
        "能力",
        # 人际关系
        "西格莉卡",
        "西西",
        "莫宁",
        "飞行雪绒",
        "阿列夫一",
        # 喜好
        "喜欢",
        "讨厌",
        "甜食",
        "睡觉",
        "蛋糕",
        "生肉",
        # 2026-10 更新：实装/复刻/近期动态
        "复刻",
        "实装",
        "英雄造物",
        "人类伪装指南",
        "坠入虚无",
        "直到下次再见",
        "予明日以谎言",
        "集谐",
        "陆·赫斯",
        # 身份/档案类提问，避免被场景指南抢走
        "身份",
        "出身",
        "档案",
        "设定",
        "年龄",
        "几岁",
        "身高",
        # 近期动态类提问
        "动态",
        "近况",
        "复刻时间",
        "什么时候复刻",
    ],
    "dania_dialogues.txt": [
        # 达妮娅官方台词样本
        "达妮娅",
        "DENIA",
        "denia",
        # 角色特质
        "睡觉",
        "睡眠",
        "午睡",
        "甜食",
        "蛋糕",
        "食物",
        "虚无",
        "谎言",
        "真相",
        "孤独",
        "幸福",
        # 相关角色
        "阿列夫一",
        "西格莉卡",
        "西西",
        "飞行雪绒",
        "莫宁",
        "苇原",
        "残星会",
        "星炬学院",
        "深空联合",
        # 常用语气
        "呼呼",
        "哈哈",
        "嗯",
        "呀",
        "呢",
        "哦",
        "嘛",
        "啦",
        # 战斗相关
        "战斗",
        "力量",
        "虚质",
        "泡泡",
        # 情感相关
        "信任",
        "防备",
        "朋友",
        "生日",
    ],
}


class KnowledgeBase:
    """本地知识库，按关键词命中后返回相关文本片段"""

    def __init__(self):
        self._cache: dict[str, str] = {}
        self._note_cache: dict[str, tuple] = {}

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
        """从知识库内容中提取与查询最相关的段落（2-gram 中文分词，召回更准）"""
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", content) if p.strip()]
        # 去掉段落里的注释/分隔线（# ---- / # 【xx】），只保留正文；
        # 不能整段丢弃，否则以分隔线开头的"基本信息""近期动态"会被一起删掉
        _cleaned = []
        for p in paragraphs:
            body = "\n".join(l for l in p.splitlines() if not l.lstrip().startswith("#")).strip()
            if body:
                _cleaned.append(body)
        paragraphs = _cleaned

        # 提取 query 的 2-gram 词（对中文有效；单字匹配噪声太大）
        q = re.sub(r"\s+", "", query)
        query_grams = set()
        for i in range(len(q) - 1):
            g = q[i : i + 2]
            if not re.match(r"[\u4e00-\u9fff]{2}", g):
                continue
            query_grams.add(g)
        # 没有 2-gram 时退化为单字
        if not query_grams:
            query_grams = {c for c in q if "\u4e00" <= c <= "\u9fff"}

        scored = []
        for p in paragraphs:
            score = sum(1 for g in query_grams if g in p)
            scored.append((score, p))
        scored.sort(key=lambda x: -x[0])

        result_parts = []
        total = 0
        for score, p in scored:
            if total >= max_chars:
                break
            if score == 0:
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
        if filename:
            content = self._load_file(filename)
            if content:
                relevant = self._extract_relevant(content, user_input)
                if relevant:
                    # 给出来源标注
                    source_name = {
                        "wuthering_waves.txt": "鸣潮",
                    }.get(filename, filename)
                    return f"[知识库·{source_name}]\n{relevant}"
        # 固定知识库没命中 → 找书上的笔记（书没有关键词表，只能靠相似度召回）
        return self._query_book_notes(user_input)

    # ---------- 书本笔记检索 ----------
    def _note_text(self, path: str) -> str:
        """读一篇笔记正文（去表头），带 mtime 缓存"""
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            return ""
        hit = self._note_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            return ""
        # 文件格式：# 标题 \n\n 记录时间/原文块 \n\n 正文
        body = raw.split("\n\n", 2)[-1].strip()
        self._note_cache[path] = (mtime, body)
        return body

    def _book_title(self, notes_path: str) -> str:
        meta_path = os.path.join(os.path.dirname(os.path.dirname(notes_path)), "meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f).get("title") or ""
        except Exception:
            return ""

    def _query_book_notes(self, user_input: str, max_chars: int = 900) -> Optional[str]:
        """在 bot/books/*/notes/*.md 里找与提问最相关的笔记段落"""
        if not os.path.isdir(BOOKS_DIR):
            return None
        q = re.sub(r"\s+", "", user_input or "")
        query_grams = {q[i : i + 2] for i in range(len(q) - 1)
                       if re.match(r"[\u4e00-\u9fff]{2}", q[i : i + 2])}
        if not query_grams:
            query_grams = {c for c in q if "\u4e00" <= c <= "\u9fff"}

        scored = []  # (score, title, index, body)
        for slug in sorted(os.listdir(BOOKS_DIR)):
            notes_root = os.path.join(BOOKS_DIR, slug, "notes")
            if not os.path.isdir(notes_root):
                continue
            title = self._book_title(os.path.join(notes_root, "x.md"))
            title_hit = bool(title) and (title in user_input or slug in user_input)
            for name in sorted(os.listdir(notes_root)):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(notes_root, name)
                body = self._note_text(path)
                if not body:
                    continue
                score = sum(1 for g in query_grams if g in body)
                # 提问里直接点名这本书 → 给足权重，保证能召回
                if title_hit:
                    score += 10
                # 一段都没撞上就不算命中（笔记短，1 个 bigram 噪声太大）
                if score >= (2 if not title_hit else 1):
                    scored.append((score, title or slug, name, body))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])

        parts, total, used = [], 0, set()
        for score, title, name, body in scored:
            if total >= max_chars:
                break
            if name in used:
                continue
            used.add(name)
            try:
                idx = int(os.path.splitext(name)[0])
            except ValueError:
                idx = 0
            parts.append(f"《{title}》第 {idx} 块：{body}")
            total += len(body)
            if len(parts) >= 4:
                break
        if not parts:
            return None
        return "[书本笔记]\n" + "\n\n".join(parts)


# 单例
_kb = KnowledgeBase()



def get_knowledge(user_input: str) -> Optional[str]:
    """对外接口：传入用户消息，返回知识片段或 None"""
    return _kb.query(user_input)
