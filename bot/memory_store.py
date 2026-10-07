"""
记忆存储模块 - 让达妮娅拥有持久记忆

两种记忆：
1. 长期记忆 (memory.json) - 用户信息、纠错事实、偏好，启动时注入提示词
2. 动态知识库 (dynamic_kb.json) - 网络搜索到的新知识，自动复用

所有记忆跨重启持久化。
"""
import os
import json
import re
import time
from typing import Optional


class MemoryStore:
    """持久化记忆系统"""

    def __init__(self, bot_dir: str = None):
        if bot_dir is None:
            bot_dir = os.path.dirname(os.path.abspath(__file__))

        self._memory_path = os.path.join(bot_dir, "data", "memory.json")
        self._dynamic_kb_path = os.path.join(bot_dir, "data", "dynamic_kb.json")
        self._ensure_dirs()

        # 加载已有记忆
        self._memories = self._load_json(self._memory_path, {"facts": [], "user_info": {}, "corrections": []})
        self._dynamic_kb = self._load_json(self._dynamic_kb_path, {"entries": []})

    def _ensure_dirs(self):
        """确保数据目录存在"""
        os.makedirs(os.path.dirname(self._memory_path), exist_ok=True)

    def _load_json(self, path: str, default: dict) -> dict:
        """安全加载 JSON"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _save_json(self, path: str, data: dict):
        """安全保存 JSON"""
        self._ensure_dirs()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ========== 长期记忆 ==========

    def add_fact(self, fact: str, source: str = "对话"):
        """添加一条事实记忆"""
        fact = fact.strip()
        if not fact or len(fact) < 5:
            return

        # 避免重复
        for entry in self._memories["facts"]:
            if entry["text"] == fact:
                entry["count"] = entry.get("count", 1) + 1
                entry["last_seen"] = time.time()
                self._save_json(self._memory_path, self._memories)
                return

        self._memories["facts"].append({
            "text": fact,
            "source": source,
            "count": 1,
            "created": time.time(),
            "last_seen": time.time()
        })
        # 最多保留 50 条
        self._memories["facts"] = self._memories["facts"][-50:]
        self._save_json(self._memory_path, self._memories)
        print(f"[记忆] 存入事实: {fact[:40]}...")

    def add_user_info(self, key: str, value: str):
        """记录用户信息（名字、喜好等）"""
        self._memories["user_info"][key] = value
        self._save_json(self._memory_path, self._memories)
        print(f"[记忆] 用户信息: {key}={value}")

    def add_correction(self, wrong: str, right: str):
        """纠错记忆：标记错误认知"""
        for entry in self._memories["corrections"]:
            if entry["wrong"] == wrong:
                entry["right"] = right
                entry["count"] = entry.get("count", 1) + 1
                self._save_json(self._memory_path, self._memories)
                return

        self._memories["corrections"].append({
            "wrong": wrong,
            "right": right,
            "count": 1
        })
        self._save_json(self._memory_path, self._memories)
        print(f"[记忆] 纠错: '{wrong}' → '{right}'")

    def get_memory_prompt(self) -> str:
        """生成注入系统提示词的记忆文本"""
        parts = []

        if self._memories["user_info"]:
            info = "，".join(f"{k}={v}" for k, v in self._memories["user_info"].items())
            parts.append(f"[记忆·用户] {info}")

        important_corrections = [c for c in self._memories["corrections"] if c.get("count", 1) >= 2]
        if important_corrections:
            corr = "；".join(f"{c['wrong']}→{c['right']}" for c in important_corrections[:5])
            parts.append(f"[记忆·纠错] {corr}")

        important_facts = [f for f in self._memories["facts"] if f.get("count", 1) >= 2]
        if important_facts:
            fact_str = "；".join(f["text"] for f in important_facts[:8])
            parts.append(f"[记忆·知识] {fact_str}")

        return "\n".join(parts) if parts else ""

    # ========== 动态知识库 ==========

    # 轻量同义词扩展表：主题词 → 同义词集合（双向匹配用）
    _SYNONYMS = {
        "电影": {"电影", "影片", "上映", "观影", "影城", "cinema"},
        "天气": {"天气", "气温", "温度", "下雨", "降雨", "刮风", "台风", "降水"},
        "热":   {"热", "高温", "炎热", "酷暑", "闷热"},
        "冷":   {"冷", "低温", "寒冷", "降温", "寒潮"},
        "吃":   {"吃", "食物", "餐厅", "餐馆", "外卖", "菜单", "点餐"},
        "睡":   {"睡", "睡觉", "困", "瞌睡", "打盹", "晚安"},
        "工作": {"工作", "上班", "打工", "办公"},
        "学习": {"学习", "读书", "上课", "课程", "考试"},
        "游戏": {"游戏", "玩家", "通关", "电竞"},
        "音乐": {"音乐", "歌曲", "专辑", "乐队", "歌单"},
    }

    @classmethod
    def _expand_tokens(cls, tokens: set) -> set:
        """把 token 集合扩展为同义词闭包（仅查表，O(n)）"""
        expanded = set(tokens)
        for t in list(tokens):
            if t in cls._SYNONYMS:
                expanded |= cls._SYNONYMS[t]
        return expanded

    @staticmethod
    def _tokenize(text: str) -> set:
        """混合分词：英文按单词、数字按 2 字块、中文按二元组

        中文二元组（bigram）可避免贪婪切出无意义 4 字块，让重合度计算有效
        """
        text = text.lower()
        tokens = set()
        # 英文单词 / 数字
        tokens.update(re.findall(r"[a-z]{2,}|\d{2,}", text))
        # 中文二元组
        for m in re.findall(r"[\u4e00-\u9fff]+", text):
            if len(m) == 1:
                tokens.add(m)
            else:
                for i in range(len(m) - 1):
                    tokens.add(m[i:i + 2])
        return tokens

    def search_dynamic_kb(self, query: str, top_k: int = 3, threshold: float = 0.3) -> Optional[str]:
        """从动态知识库检索相关条目

        打分 = 0.6*关键词重合度 + 0.2*新鲜度 + 0.2*热度
        关键词重合度 = 双向 Jaccard，带同义词扩展
        """
        entries = self._dynamic_kb.get("entries", [])
        if not entries or not query.strip():
            return None

        q_tokens = self._tokenize(query)
        if not q_tokens:
            return None

        # 同义词扩展（查询侧）
        q_expanded = self._expand_tokens(q_tokens)

        now = time.time()
        scored = []
        for e in entries:
            text_blob = e.get("topic", "") + " " + e.get("content", "")
            blob_tokens = self._tokenize(text_blob)
            blob_expanded = self._expand_tokens(blob_tokens)
            if not blob_tokens:
                continue

            # 双向重合：原始 + 同义词扩展
            overlap = q_expanded & blob_expanded
            if not overlap:
                continue
            sim_forward = len(overlap) / max(len(q_expanded), 1)
            sim_backward = len(overlap) / max(len(blob_expanded), 1)
            similarity = 0.6 * ((sim_forward + sim_backward) / 2)

            # 新鲜度：7 天内线性衰减到 0
            age_days = max((now - e.get("created", now)) / 86400, 0)
            freshness = max(0.0, min(1.0, 1.0 - age_days / 7))

            # 热度：命中 10 次封顶
            heat = min(e.get("hits", 1) / 10, 1.0)

            total = similarity + 0.2 * freshness + 0.2 * heat
            scored.append((total, e))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        hits = [s for s in scored if s[0] >= threshold][:top_k]
        if not hits:
            return None

        for _, e in hits:
            e["hits"] = e.get("hits", 0) + 1
        self._save_json(self._dynamic_kb_path, self._dynamic_kb)

        parts = []
        for _, e in hits:
            parts.append(f"[动态KB] {e.get('topic', '')}: {e.get('content', '')[:200]}")
        print(f"[动态KB] 命中 {len(hits)} 条")
        return "\n".join(parts)

    def add_dynamic_knowledge(self, topic: str, content: str):
        """存入搜索到的新知识"""
        if not topic or not content or len(content) < 10:
            return
        topic = topic.strip()
        content = content.strip()

        # 去重：相同 topic 且内容前 40 字相同 → 更新
        for e in self._dynamic_kb["entries"]:
            if e.get("topic") == topic and e.get("content", "").startswith(content[:40]):
                e["content"] = content
                e["updated"] = time.time()
                e["hits"] = e.get("hits", 0) + 1
                self._save_json(self._dynamic_kb_path, self._dynamic_kb)
                return

        self._dynamic_kb["entries"].append({
            "topic": topic,
            "content": content,
            "created": time.time(),
            "updated": time.time(),
            "hits": 0
        })
        # 最多保留 100 条，超出按 hits 最低的淘汰
        self._dynamic_kb["entries"].sort(key=lambda x: x.get("hits", 0), reverse=True)
        self._dynamic_kb["entries"] = self._dynamic_kb["entries"][:100]
        self._save_json(self._dynamic_kb_path, self._dynamic_kb)
        print(f"[动态KB] 存入: {topic[:30]}")

    # ========== 自动提取 ==========

    def extract_and_save(self, user_input: str, bot_response: str, search_result: str = ""):
        """从对话中自动提取值得记住的信息"""
        # 1. 提取用户自我介绍
        user_info_patterns = [
            (r"我叫(.{1,6})", "名字"),
            (r"我是(.{1,6})", "身份"),
            (r"我(?:今年|岁数)(\d+)岁", "年龄"),
            (r"我(?:喜欢|爱)(.{2,10})", "喜好"),
            (r"我不(?:喜欢|想)(.{2,10})", "讨厌"),
        ]
        for pattern, key in user_info_patterns:
            match = re.search(pattern, user_input)
            if match:
                value = match.group(1).strip().rstrip("。！？")
                if len(value) >= 1:
                    self.add_user_info(key, value)

        # 2. 提取纠错（用户说"不对"、"错了"、"不是"等）
        correction_patterns = [
            r"(?:不对|错了|不是.{1,3}是)(.{2,20})",
            r"(?:应该|其实是|正确的是)(.{2,20})",
        ]
        for pattern in correction_patterns:
            match = re.search(pattern, user_input)
            if match and "不是" not in match.group(0)[:3]:
                # 简单处理：把整个纠正记下来
                correction = match.group(1).strip().rstrip("。！？")
                if len(correction) >= 3:
                    self.add_fact(f"纠错：{match.group(0)[:50]}")

        # 3. 有搜索结果时存入动态知识库
        if search_result and len(search_result) > 30:
            # 从搜索结果提取主题关键词
            topic_words = re.findall(r"[\u4e00-\u9fff]{2,4}", user_input)
            if topic_words:
                topic = "".join(topic_words[:3])
                self.add_dynamic_knowledge(topic, search_result[:500])


# 单例
_store = None


def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
