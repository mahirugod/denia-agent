#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能搜索词理解模块
将用户的简短输入理解为完整的搜索意图
"""

import re
from typing import Optional, List, Tuple

class QueryUnderstanding:
    """理解用户搜索意图"""
    
    def __init__(self):
        # 游戏相关的上下文关键词
        self.game_pool_keywords = ["卡池", "角色池", "武器池", "祈愿", "抽卡", "下一次"]
        self.game_names = ["原神", "鸣潮", "崩坏", "绝区零", "星铁"]
        
        # 常见的"简化问题"到"完整问题"的映射
        self.context_patterns = {
            # (当前说的话, 上一轮是否提到游戏) -> 完整搜索词
            "呢": "应该继续上一个话题",
            "下一次": "应该是在问卡池信息",
            "什么时候": "应该是问活动时间或卡池时间",
        }
    
    def analyze_conversation(self, current_input: str, history: List[Tuple[str, str]]) -> Tuple[str, str]:
        """
        分析对话上下文，理解用户真实意图
        
        Args:
            current_input: 用户当前输入
            history: 对话历史 [(user_msg, bot_reply), ...]
        
        Returns:
            (理解的意图, 生成的搜索词)
        """
        
        # 1. 检查是否是"简化问题" - 依赖前文的问题
        intent, search_query = self._detect_follow_up(current_input, history)
        if intent:
            return intent, search_query
        
        # 2. 检查是否是游戏相关问题
        intent, search_query = self._detect_game_query(current_input, history)
        if intent:
            return intent, search_query
        
        # 3. 通用问题处理
        search_query = self._extract_keywords(current_input)
        return "通用问题", search_query
    
    def _detect_follow_up(self, current_input: str, history: List[Tuple[str, str]]) -> Tuple[Optional[str], Optional[str]]:
        """检测是否是"继续上一个话题"的简化问题"""
        
        if not history or len(history) == 0:
            return None, None
        
        last_user_input, last_bot_reply = history[-1]
        current_input_stripped = current_input.strip()
        
        # 情况1: 用户只说"呢"、"呢?"、"那呢"等
        if re.match(r'^(那?呢\??|呢\??|那?个?)$', current_input_stripped):
            return self._extend_from_context(last_user_input, last_bot_reply, "继续同一话题")
        
        # 情况2: 用户说"X呢" - 可能在问另一个游戏/人物的相同问题
        match = re.match(r'^(\S+)呢\??$', current_input_stripped)
        if match:
            topic = match.group(1)
            # 检查是否是游戏名称
            if any(game in topic for game in self.game_names):
                return self._extend_with_topic(last_user_input, topic, history)
        
        # 情况3: 代词疑问句 - "她什么时候"、"他怎么样"、"它是什么"等
        # 这些应该继续上一个话题，用前一个话题的实体替换代词
        pronoun_question_pattern = r'^(她|他|它|这|那|这个|那个)(.*?(什么|怎么|哪|几|多少|时候|情况|样子|功能|技能|属性).*)$'
        if re.search(pronoun_question_pattern, current_input_stripped):
            # 代词疑问句 - 替换代词为上一条消息中的实体
            return self._extend_from_context(last_user_input, last_bot_reply, "代词疑问继续")
        
        # 情况4: 用户请求详细信息或继续上一个话题的指令
        # 这些都应该继续上一个话题
        follow_up_keywords = [
            "是啊", "对", "嗯", "好的", "明白了",
            "讲讲呗", "讲讲", "说说呗", "说说", "讲讲吧", "说说吧",
            "详细说说", "详细讲讲", "具体说说", "具体讲讲",
            "怎么回事", "什么意思", "是什么",
            "告诉我", "能说说吗", "能讲讲吗", "给我讲讲", "给我说说",
            # 【新增】媒体发送请求
            "发一下", "你发给我", "发给我", "给我", "发", "发发", "发看看"
        ]

        # 只匹配较短的输入（>=6字的完整句子不是追问）
        if (any(current_input_stripped.endswith(kw) or current_input_stripped == kw for kw in follow_up_keywords)
                and len(current_input_stripped) <= 10):
            return self._extend_from_context(last_user_input, last_bot_reply, "继续同一话题")
        
        # 情况5: 用户只说了感叹词、疑问、表情等简短回应
        # 这些通常表示继续上一个话题或请求更多信息
        short_responses = [
            "？", "?", "！", "!",  # 单个标点
            "？？", "??", "！！", "!!",  # 双标点
            "啊", "哦", "呃", "额", "嗯", "唔",  # 感叹词
            "我不知道", "不知道", "不清楚",  # 表示不清楚
            "为什么", "怎么", "什么",  # 疑问词
        ]
        
        if current_input_stripped in short_responses or \
           len(current_input_stripped) <= 4 and any(c in "？?！!呃啊" for c in current_input_stripped):
            # 长度很短且只有标点/感叹词，很可能是继续上一话题或请求更多信息
            # 但要小心：如果上一条机器人只是简单的"是"、"否"回答，那"？"可能是疑问
            # 判断标准：上一条回复是不是很短且只是简单是/否回答
            last_reply_is_simple_answer = last_bot_reply.strip() in ["是", "否", "对", "不对", "有", "没有", "对的", "不对的"]
            
            if last_reply_is_simple_answer:
                return None, None  # 这种情况让LLM直接处理
            # 否则继续上一话题（请求详细信息或继续同一话题）
            return self._extend_from_context(last_user_input, last_bot_reply, "继续同一话题")
        
        return None, None
    
    def _extend_from_context(self, last_user_input: str, last_bot_reply: str, reason: str) -> Tuple[str, str]:
        """从上一条消息的上下文扩展搜索词"""
        
        # 提取上一条问题中的关键词
        last_keywords = self._extract_keywords(last_user_input)
        
        # 【重要】如果机器人最后一条消息以"疑问句"结尾（特别是选择题风格），
        # 用户回答"对"应该被理解为"确认"而不是新搜索
        # 例如：机器人问"你是不是想问... ?" -> 用户回"对" -> 应该直接回复，不搜索
        if last_bot_reply.rstrip().endswith("？") or last_bot_reply.rstrip().endswith("?"):
            # 这是一个疑问句，用户的"对"是确认，不需要新搜索
            # 返回原始的上一条输入和回复，让LLM直接处理确认
            return reason, last_keywords
        
        # 检查机器人回复中是否提到了什么
        if "卡池" in last_bot_reply or "祈愿" in last_bot_reply:
            # 上一条在讨论卡池，这次也应该是卡池相关
            return reason, f"{last_keywords}下一次卡池"
        
        if "活动" in last_bot_reply:
            return reason, f"{last_keywords}下一次活动"
        
        # 如果机器人最后一条提到了"时间"、"上线"等，用户回"对"可能是在确认信息
        # 而不是要求新搜索
        if any(word in last_bot_reply for word in ["上线", "下线", "时间", "时间段", "期间", "活动"]):
            # 用户确认后，不需要重复搜索，直接返回上一条的信息
            return reason, last_keywords
        
        # 默认扩展为"XX的Y"的形式
        return reason, f"{last_keywords}相关信息"
    
    def _extend_with_topic(self, last_user_input: str, new_topic: str, history: List[Tuple[str, str]]) -> Tuple[str, str]:
        """
        用户说"X呢" - 用新话题（如另一个游戏）替换上一条问题中的话题
        
        例如:
        上一条: "原神下一次卡池是什么"
        当前: "鸣潮呢"
        结果: "鸣潮下一次卡池"
        """
        
        if not history:
            return "话题转换", f"{new_topic}相关信息"
        
        last_user_input, last_bot_reply = history[-1]
        
        # 替换上一条问题中的游戏名称
        modified_query = last_user_input
        
        # 找出上一条中提到的游戏
        for game in self.game_names:
            if game in modified_query:
                modified_query = modified_query.replace(game, new_topic, 1)
                break
        
        # 如果没找到游戏名，就直接拼接
        if modified_query == last_user_input:
            modified_query = f"{new_topic}的{self._extract_keywords(last_user_input)}"
        
        return "话题转换", modified_query
    
    def _detect_game_query(self, current_input: str, history: List[Tuple[str, str]]) -> Tuple[Optional[str], Optional[str]]:
        """检测是否是游戏相关问题"""
        
        # 检查是否提到游戏名
        game_mentioned = None
        for game in self.game_names:
            if game in current_input:
                game_mentioned = game
                break
        
        # 如果当前输入没有提到游戏名，尝试从历史中推断
        if not game_mentioned:
            # 检查当前输入是否包含游戏相关关键词
            has_game_keywords = any(keyword in current_input for keyword in self.game_pool_keywords)
            has_activity_keywords = "活动" in current_input or "副本" in current_input or "秘境" in current_input
            
            # 如果有游戏相关关键词，尝试从最近的对话历史中推断游戏名
            if has_game_keywords or has_activity_keywords:
                # 从最近的历史中寻找提到的游戏
                for user_input, bot_reply in reversed(history[-5:]):  # 看最近5条
                    for game in self.game_names:
                        if game in user_input or game in bot_reply:
                            game_mentioned = game
                            break
                    if game_mentioned:
                        break
        
        if not game_mentioned:
            return None, None
        
        # 检查是否是在问卡池信息
        if any(keyword in current_input for keyword in self.game_pool_keywords):
            search_query = f"{game_mentioned}下一次卡池"
            return "游戏卡池问题", search_query
        
        # 检查是否是在问活动
        if "活动" in current_input or "副本" in current_input or "秘境" in current_input:
            search_query = f"{game_mentioned}最新活动"
            return "游戏活动问题", search_query
        
        # 检查是否是在问角色/武器
        if "角色" in current_input or "武器" in current_input or "强度" in current_input:
            search_query = f"{game_mentioned}角色强度排行"
            return "游戏内容问题", search_query
        
        return None, None
    
    def _extract_keywords(self, text: str) -> str:
        """提取文本中的核心关键词"""
        
        text = text.strip()
        
        # 特殊处理：保留重要的时间疑问词组合
        if "什么时候" in text:
            # "什么时候上线" -> "上线时间"
            cleaned = re.sub(r'什么时候(.+?)([？?。，、]*)$', r'\1时间', text)
            if cleaned == text:
                # 倒装句式："鸣潮下个版本是什么时候" -> "鸣潮下个版本时间"
                cleaned = re.sub(r'(.+?)是什么时候([？?。，、]*)$', r'\1时间', text)
            if cleaned != text:
                return cleaned
        
        # 特殊处理：保留"为什么"的实质
        if text.startswith("为什么"):
            # "为什么下线" -> "下线原因"
            cleaned = re.sub(r'^为什么(.+?)([？?。，、]*)$', r'\1原因', text)
            if cleaned != text:
                return cleaned
        
        # 【新增】特殊处理"介绍一下"、"说说"这类请求
        # "介绍一下卡提西亚" -> "卡提西亚"
        cleaned = re.sub(r'^(介绍一下|说说|讲讲|告诉我关于|关于)(.+?)([？?。，、]*)$', r'\2', text)
        if cleaned != text:
            text = cleaned
        
        # 去掉常见的疑问词和虚词（但不是所有"什么"）
        cleaned = re.sub(
            r'^(你|请|帮|帮我|告诉我|什么是|怎样|怎么|如何|能|可以|是不是|有没有|怎的)',
            '',
            text
        )
        cleaned = cleaned.strip()
        
        # 去掉结尾的疑问符号
        cleaned = re.sub(r'[？?。，、]+$', '', cleaned)
        
        # 去掉尾部的语气词但保留核心
        cleaned = re.sub(r'(呀|吗|吧|呢|啊|哦)$', '', cleaned)
        
        # 如果清理后为空或太短，保留原文
        return cleaned if cleaned and len(cleaned) > 1 else text.strip()


# 测试用例
if __name__ == "__main__":
    qu = QueryUnderstanding()
    
    # 测试案例1: "呢"的理解
    history = [
        ("原神下一次卡池是什么", "原神的下个卡池是6.6到7.0的，有新四星角色。")
    ]
    intent, query = qu.analyze_conversation("呢", history)
    print(f"测试1 - 输入: '呢'")
    print(f"  意图: {intent}")
    print(f"  搜索词: {query}")
    print()
    
    # 测试案例2: "游戏名呢"的理解
    history = [
        ("原神下一次卡池是什么", "原神的下个卡池是6.6到7.0的。")
    ]
    intent, query = qu.analyze_conversation("鸣潮呢", history)
    print(f"测试2 - 输入: '鸣潮呢'")
    print(f"  意图: {intent}")
    print(f"  搜索词: {query}")
    print()
    
    # 测试案例3: 普通游戏问题
    intent, query = qu.analyze_conversation("原神最新活动是什么", [])
    print(f"测试3 - 输入: '原神最新活动是什么'")
    print(f"  意图: {intent}")
    print(f"  搜索词: {query}")
    print()
    
    # 测试案例4: 普通问题
    intent, query = qu.analyze_conversation("Python是什么", [])
    print(f"测试4 - 输入: 'Python是什么'")
    print(f"  意图: {intent}")
    print(f"  搜索词: {query}")
