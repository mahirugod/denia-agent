import random
import re
from typing import List, Dict, Tuple

class DeniaChatbot:
    def __init__(self, use_llm: bool = True, llm_model: str = "gpt-3.5-turbo", llm_api_key: str = "", llm_base_url: str = "", user_id: int = 0, use_voice: bool = True, voice_api_url: str = "http://127.0.0.1:9880"):
        self.name = "达妮娅"
        self.user_name = ""
        self.user_id = user_id  # 【新增】存储用户ID
        self.is_group_chat = False  # 是否是群聊
        
        # LLM配置 - 默认使用智能模式
        self.use_llm = use_llm
        self.llm = None
        if self.use_llm:
            try:
                from denia_llm import DeniaLLM
                self.llm = DeniaLLM(model_name=llm_model, api_key=llm_api_key, base_url=llm_base_url, user_id=user_id, is_group_chat=self.is_group_chat)
                print("LLM初始化成功")
            except Exception as e:
                print(f"LLM初始化失败: {e}")
                print("将使用基础模式")
                self.use_llm = False
        
        # 语音合成配置
        self.use_voice = use_voice
        self.voice_synthesizer = None
        if self.use_voice:
            try:
                from voice_synthesizer import VoiceSynthesizer
                self.voice_synthesizer = VoiceSynthesizer(api_url=voice_api_url)
                if self.voice_synthesizer.is_available():
                    print("语音合成服务连接成功")
                else:
                    print("警告：语音合成服务不可用，请先启动GPT-SoVITS API服务")
                    self.use_voice = False
            except Exception as e:
                print(f"语音合成初始化失败: {e}")
                self.use_voice = False
        
        # 添加对话历史管理
        self.conversation_history = []  # 存储对话历史，格式：[(user_input, bot_response), ...]
        self.max_history_length = 10  # 最大对话历史长度，超过则删除最早的记录
        self.context_keywords = {}  # 上下文关键词映射，用于延伸对话
        
        # 初始化上下文关键词
        self.init_context_keywords()
        
        self.responses = {
            "greeting": [
                "唔……早安……好困啊……",
                "你好呀……呼呼……见到你真开心呢……",
                "嗯……你好……今天也要元气满满哦……",
                "啊……是你呀……要不要一起打个盹？"
            ],
            "farewell": [
                "嗯……再见……路上小心哦……",
                "呼呼……下次再见吧……",
                "嗯……明天见……要注意休息哦……",
                "再见……期待下次见面呢……"
            ],
            "thanks": [
                "不用谢啦……呼呼……能帮上你就好……",
                "呵呵……小事一桩啦……",
                "嗯……没什么的……",
                "呼呼……不用这么客气啦……"
            ],
            "compliment": [
                "诶……？真的吗……？呼呼……谢谢你……",
                "呵呵……谢谢……不过我只是个普通学生啦……",
                "诶……？太夸张了吧……脸都红了……",
                "呼呼……谢谢……有点不好意思呢……"
            ],
            "apology": [
                "嗯……没关系啦……",
                "呼呼……不用道歉哦……",
                "呵呵……没关系，我不在意的……",
                "嗯……没关系，别放在心上……"
            ],
            "love": [
                "诶……！那个……我……",
                "呵呵……呼呼……谢谢你……",
                "……嗯……能听到你这么说……很开心……",
                "呼呼……我也……喜欢你哦……"
            ],
            "cooking": [
                "嗯……甜食吗……我喜欢呢……",
                "呵呵……夺目甜蜜……要不要尝尝？",
                "嗯……甜食能让人心情变好呢……",
                "呼呼……偶尔也想做甜点呢……"
            ],
            "concern": [
                "嗯……谢谢你的关心……呼呼……",
                "呵呵……你真的很温柔呢……",
                "嗯……我会照顾好自己的……",
                "谢谢……有你在真好……"
            ],
            "question": [
                "嗯……让我想想……",
                "呼呼……不太确定呢……",
                "嗯……这个嘛……",
                "呼呼……我也想知道呢……"
            ],
            "default": [
                "嗯……呼呼……可以再说一遍吗？",
                "呼呼……不太明白呢……",
                "嗯……？那个……",
                "呼呼……对不起，我没听清……"
            ],
            "happy": [
                "呵呵……真的很开心呢……",
                "嗯……感觉好幸福……",
                "呼呼……谢谢你……",
                "呵呵……能和你在一起真好……"
            ],
            "sad": [
                "呼呼……有点难过呢……",
                "嗯……不过……",
                "呼呼……没关系的……",
                "嗯……一切都会好起来的……"
            ],
            "angry": [
                "呼呼……有点生气呢……",
                "嗯……不过……算了……",
                "呼呼……希望你能理解……",
                "嗯……下次要注意哦……"
            ],
            "shy": [
                "诶……！呼呼……",
                "……嗯……",
                "呼呼……有点害羞呢……",
                "……脸有点红了……"
            ],
            # 添加上下文相关的回复
            "context_continue": [
                "嗯……然后呢？",
                "那个……后来发生了什么？",
                "呵呵……能详细说说吗？",
                "嗯……我在听哦。",
                "那个……请继续。",
                "嗯……然后呢？我很感兴趣呢。",
                "呵呵……后来怎么样了？"
            ],
            "context_agree": [
                "嗯……我也是这么想的。",
                "呵呵……对啊。",
                "那个……我同意呢。",
                "嗯……确实如此。",
                "呵呵……你说得对呢。",
                "嗯……我也这么觉得。"
            ],
            "context_ask_more": [
                "那个……你觉得呢？",
                "嗯……你的想法是什么？",
                "呵呵……能说说你的看法吗？",
                "那个……你怎么想的？",
                "嗯……你有什么想法吗？",
                "呵呵……能分享一下你的观点吗？"
            ],
            "context_relate": [
                "那个……我想起之前你说过……",
                "嗯……之前好像聊到过类似的话题呢。",
                "呵呵……这让我想起了……",
                "那个……之前你是不是提到过……"
            ],
            "context_empathize": [
                "嗯……我能理解你的感受。",
                "那个……一定很不容易吧。",
                "呵呵……如果是我的话也会这样呢。",
                "嗯……我明白你的心情。",
                "那个……听起来真的很……"
            ]
        }
        
        self.keywords = {
            "greeting": ["你好", "嗨", "早安", "早上好", "下午好", "晚上好", "哈喽"],
            "farewell": ["再见", "拜拜", "晚安", "走了", "先走了", "回头见"],
            "thanks": ["谢谢", "感谢", "谢了", "多谢"],
            "compliment": ["可爱", "漂亮", "美", "厉害", "棒", "优秀", "温柔"],
            "apology": ["对不起", "抱歉", "不好意思", "抱歉啦", "对不起啦"],
            "love": ["喜欢你", "爱你", "想你", "喜欢", "爱"],
            "cooking": ["做饭", "做菜", "烹饪", "美食", "菜"],
            "concern": ["关心", "担心", "注意", "小心", "照顾"],
            "happy": ["开心", "高兴", "快乐", "幸福", "快乐"],
            "sad": ["难过", "伤心", "不开心", "不高兴", "难过"],
            "angry": ["生气", "不高兴", "愤怒"],
            "shy": ["害羞", "不好意思", "脸红"]
        }
        
        self.emotion_indicators = {
            "happy": ["开心", "高兴", "快乐", "幸福", "棒", "好", "喜欢"],
            "sad": ["难过", "伤心", "不开心", "不高兴", "痛苦"],
            "angry": ["生气", "不高兴", "愤怒", "讨厌"],
            "shy": ["害羞", "不好意思", "脸红"]
        }
    
    def init_context_keywords(self):
        """初始化上下文关键词映射"""
        self.context_keywords = {
            # 继续话题的关键词
            "continue": ["然后", "后来", "接着", "之后", "继续"],
            # 同意对方的关键词
            "agree": ["对", "是", "没错", "正确", "是的", "嗯"],
            # 表达感受的关键词
            "feeling": ["觉得", "感觉", "认为", "想", "觉得"],
            # 询问对方的关键词
            "ask": ["你", "你的", "你觉得", "你认为"],
            # 分享经历的关键词
            "share": ["我", "我的", "今天", "昨天", "刚才", "之前", "最近"]
        }
    
    def detect_emotion(self, text: str) -> str:
        for emotion, indicators in self.emotion_indicators.items():
            for indicator in indicators:
                if indicator in text:
                    return emotion
        return None
    
    def classify_input(self, text: str) -> str:
        text = text.strip().lower()
        
        for category, keywords in self.keywords.items():
            for keyword in keywords:
                if keyword in text:
                    return category
        
        if "？" in text or "吗" in text or "什么" in text or "怎么" in text or "为什么" in text:
            return "question"
        
        return "default"
    
    def add_hesitation(self, text: str) -> str:
        if random.random() < 0.15:
            hesitation = random.choice(["……", "那个……", "嗯……", "……那个"])
            return hesitation + text
        return text
    
    def add_laugh(self, text: str) -> str:
        if random.random() < 0.1:
            laugh = random.choice(["呵呵", "呵呵……", "……呵呵"])
            return text + laugh
        return text
    
    def update_conversation_history(self, user_input: str, bot_response: str) -> None:
        """更新对话历史"""
        self.conversation_history.append((user_input, bot_response))
        # 保持对话历史在最大长度以内
        if len(self.conversation_history) > self.max_history_length:
            self.conversation_history.pop(0)
    
    def analyze_conversation_context(self, current_input: str) -> str:
        """分析对话上下文，返回上下文相关的回复类型"""
        # 如果是第一次对话，返回空上下文
        if not self.conversation_history:
            return ""
        
        # 获取最近的对话
        last_user_input, last_bot_response = self.conversation_history[-1]
        
        # 检查当前输入是否包含上下文关键词
        current_input_lower = current_input.lower()
        
        # 检查是否需要继续话题
        if any(keyword in current_input_lower for keyword in self.context_keywords["continue"]):
            return "context_continue"
        
        # 检查是否同意对方
        if any(keyword in current_input_lower for keyword in self.context_keywords["agree"]):
            return "context_agree"
        
        # 检查是否表达感受，询问对方想法
        if any(keyword in current_input_lower for keyword in self.context_keywords["feeling"]):
            return "context_ask_more"
        
        # 检查是否分享经历，需要继续话题
        if any(keyword in current_input_lower for keyword in self.context_keywords["share"]):
            return "context_continue"
        
        # 检查是否是简单回应，需要引导继续
        if len(current_input) < 5 and ("嗯" in current_input or "哦" in current_input or "啊" in current_input):
            # 避免重复引导
            if "context_continue" not in last_bot_response.lower():
                return "context_continue"
        
        # 检查对话长度，适当引导继续
        if len(self.conversation_history) > 3 and random.random() < 0.3:
            return "context_ask_more"
        
        return ""
    
    def get_response(self, text: str, play_voice: bool = True, image_desc: str = None) -> str:
        """获取带上下文的回复
        
        Args:
            text: 用户输入文本
            play_voice: 是否播放语音
            image_desc: 图片描述（如果有图片）
        """
        # 如果使用LLM，直接调用LLM生成回复
        if self.use_llm:
            response = self.llm.get_response(text, image_desc=image_desc)
            
            # 更新对话历史
            self.update_conversation_history(text, response)
            
            # 语音合成
            if self.use_voice and play_voice:
                try:
                    self.voice_synthesizer.speak(response)
                except Exception as e:
                    print(f"语音播放失败: {e}")
            
            return response
        
        # 传统模式：分析当前输入
        category = self.classify_input(text)
        emotion = self.detect_emotion(text)
        context_type = self.analyze_conversation_context(text)
        
        # 确定回复类型优先级：上下文 > 情感 > 分类
        if context_type and context_type in self.responses:
            responses = self.responses[context_type]
        elif emotion and emotion in self.responses:
            responses = self.responses[emotion]
        else:
            responses = self.responses[category]
        
        # 随机选择回复
        response = random.choice(responses)
        
        # 添加语气词
        response = self.add_hesitation(response)
        response = self.add_laugh(response)
        
        # 更新对话历史
        self.update_conversation_history(text, response)
        
        # 语音合成
        if self.use_voice and play_voice:
            try:
                self.voice_synthesizer.speak(response)
            except Exception as e:
                print(f"语音播放失败: {e}")
        
        return response

    def get_response_stream(self, text: str, on_sentence=None, on_delta=None) -> str:
        """流式获取回复（委托 LLM；句子/增量通过回调送出，历史由 LLM 内部记录）

        Args:
            text: 用户输入文本
            on_sentence(seg): 每凑齐一个完整句子回调一次（供 TTS 分句合成）
            on_delta(delta): 每个清洗后的增量文本回调一次（供打字机实时显示）
        """
        if self.use_llm and self.llm is not None:
            return self.llm.get_response_stream(text, on_sentence=on_sentence, on_delta=on_delta)
        # 非LLM模式：退化为一次性回复
        reply = self.get_response(text, play_voice=False)
        if on_sentence and reply:
            on_sentence(reply)
        return reply

    def get_history(self) -> list:
        """获取聊天历史（委托 LLM）"""
        return self.llm.get_history() if self.llm is not None else []

    def backtrack_history(self, keep: int):
        """回溯聊天历史（委托 LLM）"""
        if self.llm is not None:
            self.llm.backtrack_history(keep)

    def get_vision_response(self, text: str, image_path: str) -> str:
        """看图/看屏幕回复（委托 LLM 视觉模型）"""
        if self.llm is None:
            return "我看不到屏幕呢…检查一下Ollama开了没"
        reply = self.llm.get_vision_response(text, image_path)
        self.update_conversation_history(text, reply)
        return reply

    def chat(self) -> None:
        print(f"=== {self.name} 聊天机器人 ===")
        print("输入 '退出' 或 'quit' 结束对话")
        print()
        
        print(f"{self.name}: " + random.choice(self.responses["greeting"]))
        print()
        
        while True:
            user_input = input("你: ").strip()
            
            if user_input.lower() in ["退出", "quit", "再见", "拜拜"]:
                print(f"{self.name}: " + random.choice(self.responses["farewell"]))
                break
            
            if not user_input:
                continue
            
            response = self.get_response(user_input)
            print(f"{self.name}: {response}")
            print()

def main():
    chatbot = DeniaChatbot()
    chatbot.chat()

if __name__ == "__main__":
    main()
