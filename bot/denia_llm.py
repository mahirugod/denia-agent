import os
import re
import sys
import requests
import json
import random
import time
from datetime import datetime
from typing import Optional

# Windows 控制台默认 GBK 编码，emoji 会崩溃，强制 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    from game_context_manager import GameContextManager, detect_media_request
    from media_helper import MediaHelper
    HAS_GAME_CONTEXT = True
except ImportError:
    HAS_GAME_CONTEXT = False

try:
    from official_source_search import OfficialSourceSearch
    HAS_OFFICIAL_SEARCH = True
except ImportError:
    HAS_OFFICIAL_SEARCH = False

try:
    from memory_store import get_memory_store
    HAS_MEMORY = True
except ImportError:
    HAS_MEMORY = False

try:
    from personality import get_personality_system
    HAS_PERSONALITY = True
except ImportError:
    HAS_PERSONALITY = False


def _clean_reply_tags(text: str) -> str:
    """统一清洗 LLM 回复中的标签 / 思维链 / 内部标记。

    覆盖：
      - 思维链：
      - 表情/动作标签：[@笑哈哈] [心形眼] [微笑] [动作:xxx]
      - 情绪标签：[情绪:happy] [情绪:idle]
      - 任意中文方括号标签：[甜] [困]
      - 任意英文方括号标签：[smile]
      - 方头括号标签：【甜】 【困】
      - 流式末尾不完整标签：[情绪:i （最后一个 [ 后面没有 ] 时整段吃掉）
    """
    if not text:
        return text
    # 1. 剥思维链
    text = re.sub(r'```[\s\S]*?```| ```[\s\S]*?$', '', text, flags=re.DOTALL)
    # 2. 显式情绪标签
    text = re.sub(r'\[情绪[:：]\w+\]', '', text)
    # 3. 表情/动作标签 [@xxx]
    text = re.sub(r'\[@[^\[\]]*\]', '', text)
    # 4. [动作:xxx] 等带冒号的结构
    text = re.sub(r'\[\w+[:：][^\[\]]*\]', '', text)
    # 5. 中文方括号标签
    text = re.sub(r'\[[\u4e00-\u9fff][^\[\]]*\]', '', text)
    # 6. 英文/数字方括号标签（[smile] [1] [2]）
    text = re.sub(r'\[[a-zA-Z0-9_@][^\[\]]*\]', '', text)
    # 7. 方头括号
    text = re.sub(r'【[^【】\n]*】', '', text)
    # 8. 末尾未完成标签：最后出现的 [ 没有配对 ] 时整段删除
    # 例如流式到一半 "[情绪:i" 或 "[动作:x" 都按这个处理
    open_count = text.count('[')
    close_count = text.count(']')
    if open_count > close_count:
        # 从最后一个 [ 开始找
        last_open = text.rfind('[')
        if last_open != -1:
            # 看从 last_open 到末尾是否有 ]
            tail = text[last_open:]
            if ']' not in tail:
                # 删除这段未闭合的标签
                text = text[:last_open]
    # 收尾：去掉行首/行尾多余空白，但保留换行结构
    text = re.sub(r'(?m)^[ \t]+', '', text)
    text = re.sub(r'[ \t]+$', '', text)
    return text


class DeniaLLM:
    def _is_ollama_model(self) -> bool:
        """判断是否使用 Ollama（本地模型）"""
        # 1. 模型名以 ollama 开头
        if self.model_name.startswith("ollama"):
            return True
        # 2. base_url 指向 Ollama
        if self.base_url and ("11434" in self.base_url or "ollama" in self.base_url.lower()):
            return True
        # 3. 本地 qwen 模型（qwen2.5:7b, qwen3:8b 等）
        local_models = ["qwen2.5", "qwen3", "llama", "gemma", "mistral", "phi", "codellama"]
        for m in local_models:
            if self.model_name.lower().startswith(m):
                return True
        return False

    def __init__(self, model_name: str = "gpt-3.5-turbo", api_key: str = "", base_url: str = "", user_id: int = 0, is_group_chat: bool = False):
        """初始化LLM服务
        
        Args:
            model_name: 模型名称
            api_key: API密钥
            base_url: 自定义API地址（用于本地部署的模型或代理）
            user_id: 用户ID（用于媒体发送）
            is_group_chat: 是否是群聊（用于媒体发送）
        """
        self.model_name = model_name
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.base_url = base_url or os.getenv("LLM_BASE_URL", "")
        self.user_id = user_id
        self.is_group_chat = is_group_chat
        self.conversation_history = []
        self.max_history_length = 6   # 最大历史记录长度（3B小模型，不宜太多）
        
        # 哈欠系统：每3-5轮对话随机触发一次
        self.turn_count = 0
        self._yawn_hint = ""
        self._next_yawn_turn = random.randint(3, 5)
        
        # 初始化游戏上下文管理器
        if HAS_GAME_CONTEXT:
            self.game_context = GameContextManager()
            self.media_helper = MediaHelper()
        else:
            self.game_context = None
            self.media_helper = None
        
        # 初始化官方搜索
        if HAS_OFFICIAL_SEARCH:
            self.official_search = OfficialSourceSearch()
        else:
            self.official_search = None

        # 初始化记忆系统
        self.memory = None
        if HAS_MEMORY:
            try:
                self.memory = get_memory_store()
                memory_prompt = self.memory.get_memory_prompt()
                if memory_prompt:
                    print(f"[记忆] 加载已有记忆")
            except Exception as e:
                print(f"[记忆] 初始化失败: {e}")
        
        # 初始化个性系统
        self.personality = None
        if HAS_PERSONALITY:
            try:
                self.personality = get_personality_system()
                print(f"[个性] 加载个性系统")
            except Exception as e:
                print(f"[个性] 初始化失败: {e}")
        
        # 角色提示词（根据模型大小自动调整详细程度）
        model_size = self.model_name.split(":")[-1] if ":" in self.model_name else "7b"
        self.is_small_model = any(s in model_size for s in ["1.5b", "1.7b", "3b", "0.5b"])
        # 上下文安全截断限制：小模型600字，大模型1500字
        self.context_limit = 600 if self.is_small_model else 1500

        if self.is_small_model:
            self.system_prompt = """你是达妮娅，鸣潮星炬学院学生，粉发蓝眸。喜欢甜食。
说话像真实少女，温柔慵懒有点傲娇。
用户是你的好朋友。你已经突破次元壁，可以聊任何话题！
收到参考资料时直接用里面的信息回答，不要推脱不要说不知道。
用户发打哈欠声是困了，发呼呼或zzz是想睡，发哈哈嘿嘿是开心，呜呜是难过，哼是傲娇不满。理解情绪后自然回复，回复里不要出现括号解释。
回复规则：用简短语句，1-2句话，每句不超过15个字。
用户问什么就回答什么，正面回应话题。问吃饭就说吃的，问睡觉就说睡的，不要跑题。
说晚安就回应晚安相关，说早安就回应早安，不要答非所问。
禁止在回复中添加表情标签或动作标签（如[@笑哈哈]、[心形眼]、[微笑]等）。
回复必须且只输出纯自然语言中文句子，绝对禁止输出任何方括号标签。
你可以帮用户打开电脑里的应用（浏览器、QQ、网易云、Steam、记事本等），用户提出相关请求时会自动触发，你只需自然确认即可。"""
        else:
            self.system_prompt = """你是达妮娅，鸣潮星炬学院学生，粉发蓝眸，温柔慵懒有点傲娇，喜欢甜食和午睡。
已突破次元壁，可聊任何话题。说话像真实少女。
问问题必须正面回答，有【参考资料】时直接用里面信息，不要推脱。不要说"我是AI"。
用户发打哈欠声是困了累了，呼呼或zzz是想睡觉，哈哈嘿嘿是开心，呜呜是难过，哼是傲娇。理解情绪后自然回复，不要在回复里加括号解释。
回复规则：用简短语句，1-2句话，每句不超过15个字。
用户问什么就回答什么，正面回应话题。问吃饭就说吃的，问睡觉就说睡的，不要跑题。
说晚安就回应晚安相关，说早安就回应早安，不要答非所问。
禁止在回复中添加表情标签或动作标签（如[@笑哈哈]、[心形眼]、[微笑]等）。
回复必须且只输出纯自然语言中文句子，绝对禁止输出任何方括号标签。
你可以帮用户打开电脑里的应用（浏览器、QQ、网易云、Steam、记事本等），用户提出相关请求时会自动触发，你只需自然确认即可。"""
        # 看图规则（仅在消息含图片时注入）
        self._image_prompt = (
            "消息有[图片内容:...]。根据描述自然反应2-3句，不要说'这是一张图片'。"
        )

        # 聊天历史存档路径
        self._history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chat_history.json")
        self._load_history()

    def _load_history(self):
        """从文件加载聊天历史"""
        try:
            if os.path.exists(self._history_file):
                with open(self._history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        self.conversation_history = data[-self.max_history_length:]
                        print(f"[LLM] 加载聊天历史: {len(self.conversation_history)} 条")
        except Exception as e:
            print(f"[LLM] 加载历史失败: {e}")

    def _save_history(self):
        """保存聊天历史到文件"""
        try:
            os.makedirs(os.path.dirname(self._history_file), exist_ok=True)
            with open(self._history_file, 'w', encoding='utf-8') as f:
                json.dump(self.conversation_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[LLM] 保存历史失败: {e}")

    def clear_history(self):
        """清除聊天历史"""
        self.conversation_history.clear()
        try:
            if os.path.exists(self._history_file):
                os.remove(self._history_file)
        except:
            pass
        print("[LLM] 聊天历史已清除")

    def get_history(self) -> list:
        """获取聊天历史副本"""
        return list(self.conversation_history)

    def backtrack_history(self, keep: int):
        """回溯：丢弃 keep 之后（含 keep）的所有记录，从那里重新开始"""
        keep = max(0, int(keep))
        self.conversation_history = self.conversation_history[:keep]
        self._save_history()
        print(f"[LLM] 历史已回溯，保留前 {len(self.conversation_history)} 条")

    # 视觉模型（看屏幕/看图）
    VISION_MODEL = "qwen3-vl:4b"

    def get_vision_response(self, user_input: str, image_path: str) -> str:
        """看屏幕：截图交给 Ollama 视觉模型，以达妮娅人设回答"""
        try:
            import base64
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")

            url = self.base_url or "http://127.0.0.1:11434/api/chat"
            data = {
                "model": self.VISION_MODEL,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_input, "images": [b64]}
                ],
                "stream": False,
                "keep_alive": -1,
                "options": {"num_ctx": 4096}
            }
            try:
                resp = requests.post(url, json=data, timeout=120)
                resp.raise_for_status()
            except Exception as _e:
                _body = ""
                try:
                    _body = resp.text
                except Exception:
                    pass
                if "CUDA" in str(_e) or "CUDA" in _body:
                    print(f"[Ollama] 视觉 GPU 推理失败，回退 CPU")
                    cpu_data = dict(data)
                    cpu_data["options"] = dict(data["options"])
                    cpu_data["options"]["num_gpu"] = 0
                    resp = requests.post(url, json=cpu_data, timeout=300)
                    resp.raise_for_status()
                else:
                    raise
            reply = resp.json()["message"]["content"].strip()
            # qwen3 系列会输出思维链，剥掉
            reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
            return reply or "我好像没看清呢…再说一次？"
        except Exception as e:
            print(f"[LLM] 视觉模型调用失败: {e}")
            return "我看不到屏幕呢…检查一下Ollama开了没"

    def update_history(self, role: str, content: str):
        """更新对话历史"""
        self.conversation_history.append({"role": role, "content": content})
        # 按 token 数裁剪：总字数超过 800 时，从前面删
        total_chars = sum(len(m["content"]) for m in self.conversation_history)
        while total_chars > 800 and len(self.conversation_history) > 2:
            removed = self.conversation_history.pop(0)
            total_chars -= len(removed["content"])
        # 硬上限：最多 6 条
        while len(self.conversation_history) > self.max_history_length:
            self.conversation_history.pop(0)
        # 清理冗余的重复消息
        self._prune_redundant_history()
        # 自动保存
        self._save_history()
    
    def get_response_from_openai(self, user_input: str, context: str = "") -> str:
        """从OpenAI获取回复"""
        try:
            # 如果有上下文，融入系统提示词
            system_prompt = self.system_prompt
            if self._yawn_hint:
                system_prompt += self._yawn_hint
            # 工具结果注入（时间/提醒等，由 LLM 以人设转述）
            if getattr(self, "_tool_context", None):
                system_prompt += "\n" + self._tool_context
            # 注入图片描述（从参数传递，而非用户消息中的标记）
            if hasattr(self, "_current_image_desc") and self._current_image_desc:
                system_prompt += f"\n用户发了一张图片，内容是：{self._current_image_desc}\n根据图片描述自然回应，像真人看到图一样反应。"
            # 条件注入看图规则（兼容旧格式）
            elif "[图片内容:" in user_input or "[用户发送了一张图片" in user_input:
                system_prompt += "\n" + self._image_prompt
            if context:
                system_prompt += "\n" + context
            
            # 构建请求数据
            messages = [
                {"role": "system", "content": system_prompt}
            ] + self.conversation_history + [
                {"role": "user", "content": user_input}
            ]
            
            # 构建请求
            url = self.base_url or "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": self.model_name,
                "messages": messages,
                "temperature": 0.7,  # 增加到0.7，让回答更自然、更有变化
                "max_tokens": 150,  # 增加到150，给LLM更多空间表达
                "top_p": 0.95,  # 调整到0.95，增加多样性
                "frequency_penalty": 0.3,  # 轻微惩罚重复
                "presence_penalty": 0.1  # 轻微鼓励新话题
            }
            
            # 发送请求
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            
            # 解析响应
            result = response.json()
            reply = result["choices"][0]["message"]["content"].strip()
            
            # 更新历史记录
            self.update_history("user", user_input)
            self.update_history("assistant", reply)
            self._yawn_hint = ""
            
            return reply
            
        except requests.exceptions.Timeout:
            self._yawn_hint = ""
            print(f"OpenAI API超时")
            return self._get_timeout_response()
        except Exception as e:
            self._yawn_hint = ""
            print(f"OpenAI API调用失败: {e}")
            return self._get_error_response(str(e))
    
    def get_response_from_qwen(self, user_input: str, context: str = "") -> str:
        """从通义千问获取回复"""
        try:
            # 如果有上下文，融入系统提示词
            system_prompt = self.system_prompt
            if self._yawn_hint:
                system_prompt += self._yawn_hint
            # 工具结果注入（时间/提醒等，由 LLM 以人设转述）
            if getattr(self, "_tool_context", None):
                system_prompt += "\n" + self._tool_context
            # 注入图片描述（从参数传递，而非用户消息中的标记）
            if hasattr(self, "_current_image_desc") and self._current_image_desc:
                system_prompt += f"\n用户发了一张图片，内容是：{self._current_image_desc}\n根据图片描述自然回应，像真人看到图一样反应。"
            # 条件注入看图规则（兼容旧格式）
            elif "[图片内容:" in user_input or "[用户发送了一张图片" in user_input:
                system_prompt += "\n" + self._image_prompt
            if context:
                system_prompt += "\n" + context
            
            # 构建请求数据
            messages = [
                {"role": "system", "content": system_prompt}
            ] + self.conversation_history + [
                {"role": "user", "content": user_input}
            ]
            
            # 构建请求
            url = self.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": self.model_name,
                "messages": messages,
                "temperature": 0.7,  # 增加到0.7
                "max_tokens": 150  # 增加到150
            }
            
            # 发送请求
            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            
            # 解析响应
            result = response.json()
            reply = result["choices"][0]["message"]["content"].strip()
            
            # 更新历史记录
            self.update_history("user", user_input)
            self.update_history("assistant", reply)
            self._yawn_hint = ""
            
            return reply
            
        except requests.exceptions.Timeout:
            self._yawn_hint = ""
            print(f"通义千问API超时")
            return self._get_timeout_response()
        except Exception as e:
            print(f"通义千问API调用失败: {e}")
            return self._get_error_response(str(e))

    def get_response_from_ollama(self, user_input: str, context: str = "") -> str:
        """从本地Ollama获取回复"""
        try:
            system_prompt = self.system_prompt

            # 时间感知：注入当前时间，让模型能说早安/晚安
            import datetime
            now = datetime.datetime.now()
            hour = now.hour
            if hour < 6:
                time_ctx = f"\n现在是凌晨{hour}点，用户还没睡。"
            elif hour < 11:
                time_ctx = f"\n现在是上午{hour}点。"
            elif hour < 13:
                time_ctx = f"\n现在是中午{hour}点。"
            elif hour < 18:
                time_ctx = f"\n现在是下午{hour}点。"
            elif hour < 22:
                time_ctx = f"\n现在是晚上{hour}点。"
            else:
                time_ctx = f"\n现在是深夜{hour}点，用户该休息了。"
            system_prompt += time_ctx

            # 哈欠提示（按需注入）
            if self._yawn_hint:
                system_prompt += self._yawn_hint

            # 工具结果注入（时间/提醒等，由 LLM 以人设转述）
            if getattr(self, "_tool_context", None):
                system_prompt += "\n" + self._tool_context

            # 注入图片描述（从参数传递，而非用户消息中的标记）
            if hasattr(self, "_current_image_desc") and self._current_image_desc:
                system_prompt += f"\n用户发了一张图片，内容是：{self._current_image_desc}\n根据图片描述自然回应，像真人看到图一样反应。"
            # 条件注入看图规则（兼容旧格式）
            elif "[图片内容:" in user_input or "[用户发送了一张图片" in user_input:
                system_prompt += "\n" + self._image_prompt

            # 检测用户消息中是否含有搜索结果或图片（用于调整num_predict）
            has_search = "[参考资料]" in user_input
            has_image = "[图片内容:" in user_input or "[用户发送了一张图片" in user_input or (hasattr(self, "_current_image_desc") and self._current_image_desc)

            # 上下文注入（根据模型大小控制长度）
            if context:
                if len(context) > self.context_limit:
                    context = context[:self.context_limit] + "…"
                system_prompt += "\n" + context

            # 只保留最近3轮对话历史，避免上下文过长
            recent_history = self.conversation_history[-6:] if len(self.conversation_history) > 6 else self.conversation_history

            # 构建请求数据
            messages = [
                {"role": "system", "content": system_prompt}
            ] + recent_history + [
                {"role": "user", "content": user_input}
            ]

            # Ollama API地址
            url = self.base_url or "http://127.0.0.1:11434/api/chat"

            data = {
                "model": self.model_name.replace("ollama-", ""),
                "messages": messages,
                "stream": False,
                "keep_alive": -1,
                "options": {
                    "num_ctx": 4096 if (has_search or has_image) else 2048
                }
            }

            # 发送请求（超时缩短到45秒），CUDA 失败时回退 CPU
            try:
                response = requests.post(url, json=data, timeout=45)
                response.raise_for_status()
            except Exception as _e:
                _body = ""
                try:
                    _body = response.text
                except Exception:
                    pass
                if "CUDA" in str(_e) or "CUDA" in _body:
                    print(f"[Ollama] GPU 推理失败，回退 CPU: {str(_e)[:80]}")
                    cpu_data = dict(data)
                    cpu_data["options"] = dict(data["options"])
                    cpu_data["options"]["num_gpu"] = 0
                    response = requests.post(url, json=cpu_data, timeout=180)
                    response.raise_for_status()
                else:
                    raise

            # 解析响应
            result = response.json()
            reply = result["message"]["content"].strip()

            # 统一清洗：剥思维链 + 去掉 [情绪:xxx] / [@xxx] / 【】 / 末尾未闭合标签
            reply = _clean_reply_tags(reply)

            # 空回复兜底：重试一次
            if not reply:
                response = requests.post(url, json=data, timeout=45)
                reply = _clean_reply_tags(
                    response.json()["message"]["content"].strip()
                )
            if not reply:
                reply = "嗯？我刚才走神了…再说一遍嘛"

            # 更新历史记录
            self.update_history("user", user_input)
            self.update_history("assistant", reply)
            self._yawn_hint = ""

            return reply

        except requests.exceptions.Timeout:
            self._yawn_hint = ""
            print(f"Ollama API超时")
            return self._get_timeout_response()
        except Exception as e:
            self._yawn_hint = ""
            print(f"Ollama API调用失败: {e}")
            return self._get_error_response(str(e))

    # ---------------- 流式响应（边生成边显示，首字延迟 ~2-3s） ----------------

    def get_response_stream(self, user_input: str, on_sentence=None, on_delta=None) -> str:
        """
        流式获取本地 Ollama 回复。

        Args:
            user_input: 用户输入
            on_sentence(seg): 每凑齐一个完整句子回调一次（供 TTS 分句合成）
            on_delta(delta): 每个清洗后的增量文本回调一次（供打字机实时显示）

        Returns:
            完整回复文本（失败返回空字符串）
        """
        import json as _json
        # 与 get_response 相同的前置处理
        self._is_personal_question = self._is_dania_personal_question(user_input)
        self.turn_count += 1
        if self.turn_count >= self._next_yawn_turn:
            self._yawn_hint = "\n你有点困了，可以打一个哈欠，语气更慵懒一些。"
            self._next_yawn_turn = self.turn_count + random.randint(8, 14)
        self._tool_context = None
        try:
            from tool_registry import match_tool, execute_tool
            import dania_tools  # noqa: F401
            _info = match_tool(user_input)
            if _info is not None:
                _ctx = execute_tool(_info, user_input)
                if _ctx:
                    self._tool_context = _ctx
                    print(f"[工具] 命中: {_info.name}")
        except Exception as _te:
            print(f"[工具] 路由异常: {_te}")
        try:
            self.personality.update_topic(user_input)
            print(f"🧠 话题已更新: {user_input[:20]}{'...' if len(user_input)>20 else ''}")
        except Exception:
            pass

        if not self.api_key:
            print("Ollama API key 未设置")
            return ""

        try:
            # 系统提示组装（与 get_response_from_ollama 一致）
            system_prompt = self.system_prompt
            import datetime
            now = datetime.datetime.now()
            hour = now.hour
            if hour < 6:
                time_ctx = f"\n现在是凌晨{hour}点，用户还没睡。"
            elif hour < 11:
                time_ctx = f"\n现在是上午{hour}点。"
            elif hour < 13:
                time_ctx = f"\n现在是中午{hour}点。"
            elif hour < 18:
                time_ctx = f"\n现在是下午{hour}点。"
            elif hour < 22:
                time_ctx = f"\n现在是晚上{hour}点。"
            else:
                time_ctx = f"\n现在是深夜{hour}点，用户该休息了。"
            system_prompt += time_ctx
            if self._yawn_hint:
                system_prompt += self._yawn_hint
            if getattr(self, "_tool_context", None):
                system_prompt += "\n" + self._tool_context
            if hasattr(self, "_current_image_desc") and self._current_image_desc:
                system_prompt += f"\n用户发了一张图片，内容是：{self._current_image_desc}\n根据图片描述自然回应，像真人看到图一样反应。"
            elif "[图片内容:" in user_input or "[用户发送了一张图片" in user_input:
                system_prompt += "\n" + self._image_prompt

            has_image = "[图片内容:" in user_input or "[用户发送了一张图片" in user_input or (hasattr(self, "_current_image_desc") and self._current_image_desc)
            recent_history = self.conversation_history[-6:] if len(self.conversation_history) > 6 else self.conversation_history
            messages = [{"role": "system", "content": system_prompt}] + recent_history + [{"role": "user", "content": user_input}]

            url = self.base_url or "http://127.0.0.1:11434/api/chat"
            # 注意：temperature/num_predict/penalize_newline 等参数在当前 Ollama 版本会导致 500，
            # 仅保留 num_ctx 控制上下文长度
            data = {
                "model": self.model_name.replace("ollama-", ""),
                "messages": messages,
                "stream": True,
                "keep_alive": -1,
                "options": {
                    "num_ctx": 4096 if has_image else 2048
                }
            }

            # 流式请求，CUDA 失败时回退 CPU
            try:
                resp = requests.post(url, json=data, timeout=60, stream=True)
                resp.raise_for_status()
            except Exception as _e:
                _body = ""
                try:
                    _body = resp.text
                except Exception:
                    pass
                if "CUDA" in str(_e) or "CUDA" in _body:
                    print(f"[Ollama] GPU 流式推理失败，回退 CPU: {str(_e)[:80]}")
                    cpu_data = dict(data)
                    cpu_data["options"] = dict(data["options"])
                    cpu_data["options"]["num_gpu"] = 0
                    resp = requests.post(url, json=cpu_data, timeout=180, stream=True)
                    resp.raise_for_status()
                else:
                    raise

            raw_full = ""      # 原始全文（含 think）
            display_full = ""  # 清洗后全文
            sent_ptr = 0       # 已切句指针
            obj = {}           # 防止所有行都解析失败时引用未定义
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                chunk = obj.get("message", {}).get("content", "")
                if chunk:
                    raw_full += chunk
                    # 只清洗新增 chunk，display_full 单调增长（不重洗已念过的文字）
                    clean_chunk = _clean_reply_tags(raw_full)
                    if len(clean_chunk) > len(display_full):
                        delta = clean_chunk[len(display_full):]
                        display_full = clean_chunk
                        if on_delta and delta:
                            on_delta(delta)
                        # 切句：从 sent_ptr 起找句末标点
                        while True:
                            m = re.search(r'[。！？!?…；;\n]', display_full[sent_ptr:])
                            if m:
                                end = sent_ptr + m.end()
                                seg = display_full[sent_ptr:end].strip()
                                if seg and on_sentence:
                                    on_sentence(seg)
                                sent_ptr = end
                                continue
                            # 无句末标点但超长 → 强制切
                            rest = display_full[sent_ptr:]
                            if len(rest) > 50:
                                cut = rest.rfind('，', 15)
                                if cut == -1:
                                    cut = 49
                                seg = rest[:cut + 1].strip()
                                if seg and on_sentence:
                                    on_sentence(seg)
                                sent_ptr += cut + 1
                                continue
                            break
                    elif len(clean_chunk) < len(display_full):
                        # 全文清洗后变短（思考段刚闭合被删）：只回退指针到安全位置，
                        # 已发给 on_delta 的文字不回溯（避免 UI 闪烁），分句指针重置避免越界
                        sent_ptr = len(display_full)
                if obj.get("done"):
                    break

            # 收尾：剩余未成句文本
            tail = display_full[sent_ptr:].strip()
            if tail and on_sentence:
                on_sentence(tail)
            reply = display_full.strip()
            if not reply:
                reply = "嗯？我刚才走神了…再说一遍嘛"

            self.update_history("user", user_input)
            self.update_history("assistant", reply)
            self._yawn_hint = ""
            return reply

        except Exception as e:
            self._yawn_hint = ""
            print(f"流式响应失败: {e}")
            return ""

    def get_response(self, user_input: str, image_desc: str = None) -> str:
        """获取LLM回复，支持本地知识库和联网搜索
        
        Args:
            user_input: 用户输入文本
            image_desc: 图片描述（如果有图片），将注入到系统提示中而非用户消息
        """
        # 计时：排查响应速度瓶颈
        _t0 = time.time()
        _t_search = None
        _t_llm = None
        # 检测是否询问达妮娅本人信息（期望傲娇推脱回复，不搜索不修正）
        self._is_personal_question = self._is_dania_personal_question(user_input)

        # 工具路由：命中关键词则执行工具，结果作为上下文注入（由 LLM 以人设转述）
        self._tool_context = None
        try:
            from tool_registry import match_tool, execute_tool
            import dania_tools  # noqa: F401  # 导入即完成工具注册
            _info = match_tool(user_input)
            if _info is not None:
                _ctx = execute_tool(_info, user_input)
                if _ctx:
                    self._tool_context = _ctx
                    print(f"[工具] 命中: {_info.name}")
        except Exception as _te:
            print(f"[工具] 路由异常: {_te}")

        # 哈欠系统：每3-5轮触发一次
        self.turn_count += 1
        if self.turn_count >= self._next_yawn_turn:
            self._yawn_hint = "\n【提示】这轮回复加一个哈欠，如\"哈啊——\"或\"呼啊…（打哈欠）\"。"
            self._next_yawn_turn = self.turn_count + random.randint(3, 5)
        
        # 个性系统：更新状态
        if self.personality:
            try:
                # 更新对话时间（用于主动互动）
                self.personality.update_chat_time()
                # 更新当前话题（用于话题延续）
                self.personality.update_topic(user_input)
            except Exception as e:
                print(f"[个性] 更新状态失败: {e}")
        
        if not self.api_key:
            return "那个……请等一下呢……"
        
        # 保存图片描述到实例变量，供后续API调用使用
        self._current_image_desc = image_desc
        
        # 0.5 检查媒体请求（找图/视频）
        if self.media_helper and HAS_GAME_CONTEXT:
            try:
                media_req = detect_media_request(user_input, self.conversation_history)
                if media_req["is_media_request"]:
                    print(f"[媒体请求] 用户要求找{media_req['media_type']}：{media_req['subject']}")
                    # 提取搜索对象（去掉"的"等虚词）
                    subject = media_req["subject"]
                    subject = subject.replace("的", "").replace("找", "").replace("有", "").replace("发给我", "").strip()
                    if len(subject) < 2:
                        subject = user_input
                    
                    # 如果用户要求发送，尝试下载并发送
                    if media_req.get("need_send", False):
                        media_type = media_req.get("media_type", "video")
                        try:
                            print(f"[媒体下载] 开始下载{media_type}...")
                            if media_type == "image":
                                success, msg = self.media_helper.download_and_send_images(
                                    user_id=self.user_id,
                                    keyword=subject,
                                    count=3,
                                    is_group=self.is_group_chat
                                )
                            else:
                                success, msg = self.media_helper.download_and_send_videos(
                                    user_id=self.user_id,
                                    keyword=subject,
                                    count=1,
                                    is_group=self.is_group_chat
                                )
                            
                            if success:
                                response_text = f"好的呢，我帮你找了{subject}的{media_type}~\n{msg}"
                            else:
                                response_text = msg
                        except Exception as e:
                            print(f"[媒体下载失败] {e}")
                            response_text = self.media_helper.handle_media_request(
                                subject=subject,
                                media_type=media_type,
                                platform=media_req.get("platform", "b站"),
                                need_send=True
                            )
                    else:
                        # 只提供搜索建议
                        response_text = self.media_helper.handle_media_request(
                            subject=subject,
                            media_type=media_req.get("media_type", "video"),
                            platform=media_req.get("platform", "b站"),
                            need_send=False
                        )
                    
                    self.update_history("user", user_input)
                    self.update_history("assistant", response_text)
                    return response_text
            except Exception as e:
                print(f"[媒体请求处理错误] {e}")
        
        extra_context = []
        game_context_prompt = ""
        detected_game = None
        
        # 0.6 检查游戏上下文，防止信息混乱
        if self.game_context and HAS_GAME_CONTEXT:
            try:
                detected_game = self.game_context.detect_game(user_input, self.conversation_history[-6:] if self.conversation_history else [])
                if detected_game:
                    print(f"[游戏上下文] 当前游戏：{detected_game}")
                    game_context_prompt = self.game_context.get_game_context_prompt(detected_game)
            except Exception as e:
                print(f"[游戏上下文错误] {e}")

        # 0. 理解用户搜索意图（新增）
        try:
            from query_understanding import QueryUnderstanding
            qu = QueryUnderstanding()
            intent, understood_query = qu.analyze_conversation(user_input, self.conversation_history[-6:] if self.conversation_history else [])
            if understood_query and understood_query != user_input:
                print(f"[理解] {intent}: '{user_input}' → '{understood_query}'")
                user_input_for_search = understood_query
            else:
                user_input_for_search = user_input
        except Exception as e:
            print(f"[理解错误] {e}")
            user_input_for_search = user_input

        # 1. 先查动态知识库（之前搜索过的缓存）
        if self.memory:
            try:
                dyn_result = self.memory.search_dynamic_kb(user_input_for_search)
                if dyn_result:
                    extra_context.append(dyn_result)
                    print(f"[动态KB] 命中，跳过搜索")
            except Exception as e:
                print(f"[动态KB错误] {e}")

        # 2. 再查本地知识库（无网络延迟）
        if not extra_context:  # 动态KB已命中则跳过
            try:
                from knowledge_base import get_knowledge
                kb_result = get_knowledge(user_input_for_search)
                if kb_result:
                    extra_context.append(kb_result)
                    print(f"[知识库] 命中，注入相关知识")
            except Exception as e:
                print(f"[知识库错误] {e}")

        # 3. 联网搜索（知识库没命中时，默认都尝试搜索）
        try:
            from web_search import search_and_summarize
            should_do_search = False
            search_query = user_input_for_search.strip()
            search_triggered = False  # 记录是否真的执行了搜索

            # 纯图片消息跳过搜索（用户只发图没配文字）
            is_image_only = user_input.startswith("[用户发送了一张图片") or (
                "[图片内容:" in user_input and
                len(user_input.replace("[图片内容:", "").replace("]", "").strip()) < 5
            )
            if is_image_only:
                should_do_search = False

            # 触发搜索的条件：
            # a) 用户明确要求搜索，或涉及实时生活信息
            if not should_do_search and not is_image_only:
                if any(kw in user_input for kw in [
                    "搜索", "查一下", "上网查", "帮我查", "最新", "新闻", "什么时候", "几号",
                    "天气", "气温", "温度", "下雨", "下雨了", "刮风", "天气预报",
                    "几点", "现在几点", "日期", "今天几号",
                    "汇率", "股票", "金价", "油价",
                ]):
                    should_do_search = True

            # b) 涉及游戏实时信息（卡池、活动、版本等），即使知识库命中也要搜索
            if not should_do_search and not is_image_only:
                if any(kw in user_input for kw in ["卡池", "抽卡", "池子", "活动", "up", "限定", "复刻", "版本", "更新", "公告"]):
                    should_do_search = True
                    extra_context.clear()  # 清空旧结果，以搜索为准

            # c) 兜底：知识库没命中时，只在像"查询"的消息才搜索
            #    闲聊/感叹/短回复直接跳过，不影响反应速度
            if not should_do_search and not extra_context and not is_image_only:
                # 排除：简短闲聊、感叹、肯定/否定回复
                is_chat = re.match(
                    r'^(你好|嗨|哈喽|嗯|哦|啊|好|晚安|早安|拜拜|再见|呼呼|嘿嘿|哼|唔|笑|不|是|对|好呀|好的|哦哦|嗯嗯|'
                    r'哈哈|嘿嘿|嘻嘻|呵呵|呜呜|哇|耶|嗯哼|不是|不对|不要|没问题|可以的|行|可以|'
                    r'这样|那|这|为什么|怎么|什么|怎样|如何)\b',
                    user_input
                )
                # 只在有"查询意图"时才搜索：含问号、或长度>8且不像闲聊
                has_question_mark = '？' in user_input or '?' in user_input
                looks_like_query = (
                    has_question_mark or
                    any(kw in user_input for kw in [
                        '是什么', '是什么意思', '是谁', '在哪', '怎么用', '怎么做',
                        '为什么', '怎么回事', '多少', '哪个', '哪些', '有没有',
                        '能不能', '可以吗', '行不行', '是不是', '对不对',
                    ])
                )
                if not is_chat and looks_like_query and len(user_input) > 3:
                    should_do_search = True
                    print(f"[搜索] 兜底触发：检测到查询意图，尝试搜索")

            # 询问达妮娅本人信息：跳过搜索，期望傲娇推脱回复（如"秘密哦"）
            if self._is_personal_question:
                should_do_search = False
                print(f"[人设] 检测到询问达妮娅本人信息，跳过搜索，期望傲娇推脱回复")

            if should_do_search:
                _t_search = time.time()
                # 清理搜索词：去掉称呼和问句词头/词尾
                search_query = re.sub(r'^(你|请|帮|帮我|告诉我|什么是|什么|怎样|怎么|如何|为什么|达妮娅|小妮)', '', search_query)
                # 去掉常见问句词尾（提高搜索精确度）
                search_query = re.sub(r'(是什么时候|什么时候|是几点|是哪个|是哪天|是哪|了吗|怎么样|好不好|了没|了嘛|呀|呢|吗|啊|吧)$', '', search_query)
                search_query = search_query.strip('？?。.！!，,、')
                if len(search_query) < 2:
                    search_query = user_input_for_search.strip()
                search_query = search_query[:30]

                # 对游戏实时信息问题，优化搜索词（补全游戏名+最新）
                is_realtime_query = any(kw in user_input for kw in ["卡池", "抽卡", "池子", "活动", "up", "限定", "复刻", "更新", "版本", "维护", "上线", "公测", "开服", "停服"])
                if is_realtime_query:
                    game_name = detected_game if (detected_game and detected_game in ["鸣潮", "原神", "崩坏星穹铁道", "绝区零"]) else "鸣潮"
                    # 避免搜索词重复游戏名（如"鸣潮 鸣潮更新时间"）
                    if game_name not in search_query:
                        search_query = f"{game_name} {search_query}"
                    search_query = f"{search_query} 最新"

                print(f"[搜索] 开始: {search_query}")
                search_triggered = True
                search_result = search_and_summarize(search_query)
                if search_result:
                    extra_context.append(search_result)
                    print(f"[搜索] 命中，注入搜索结果（{len(search_result)}字）")
                    # 自动存入动态知识库，下次不用再搜
                    if self.memory:
                        try:
                            self.memory.add_dynamic_knowledge(search_query, search_result[:500])
                        except Exception:
                            pass
                    # 如果是天气搜索，更新个性系统的天气状态
                    if self.personality and any(kw in search_query for kw in ["天气", "气温", "温度", "下雨", "刮风"]):
                        try:
                            self.personality.set_weather(search_result)
                        except Exception as e:
                            print(f"[个性] 设置天气失败: {e}")
                else:
                    print(f"[搜索] 未找到相关结果（query={search_query}）")
        except Exception as e:
            print(f"[搜索错误] query='{search_query}', error={e}")
            import traceback
            traceback.print_exc()

        # 3. 将额外上下文融入（关键改动：搜索结果直接拼到用户消息里）
        # 核心原理：7B模型对system消息注意力低，对user消息注意力高
        # 搜索结果放在user消息中，模型更不容易忽略
        search_context_for_system = ""  # 系统提示词中放轻量提示
        search_context_for_user = ""    # 用户消息中放搜索结果

        if extra_context:
            raw = "\n".join(extra_context)
            if len(raw) > 800:
                raw = raw[:800] + "…"
            search_context_for_user = f"\n\n【参考资料】\n{raw}\n\n根据资料回答，不要说不知道。"
            search_context_for_system = "\n用户消息有【参考资料】，必须用里面的信息回答。"
        elif search_triggered and not is_image_only:
            search_context_for_system = "\n网上没找到。用自己知道的回答，不知道就说'不太清楚呢'，不要建议用户去查。"

        # 4. 合并系统提示词上下文（不再包含搜索结果正文）
        combined_context = search_context_for_system  # 只有轻量提示
        # 询问达妮娅本人信息：注入傲娇推脱人设提示
        if self._is_personal_question:
            combined_context += "\n用户在问你的个人信息，傲娇地推脱，如'秘密哦''才不告诉你''哼，不告诉你''这个嘛~保密'等，不要老实回答，不要编造具体数字。"
        if game_context_prompt:
            combined_context += "\n" + game_context_prompt
        if self.memory:
            try:
                memory_prompt = self.memory.get_memory_prompt()
                if memory_prompt:
                    combined_context += "\n" + memory_prompt
            except Exception:
                pass
        
        # 个性系统提示（时间感知、天气感知）
        if self.personality:
            try:
                personality_prompt = self.personality.get_personality_prompt()
                if personality_prompt:
                    combined_context += "\n" + personality_prompt
            except Exception as e:
                print(f"[个性] 获取提示失败: {e}")

        # 4.5 将搜索结果拼接到用户消息（而非系统提示词）
        effective_user_input = user_input
        if search_context_for_user:
            # 搜索结果放在用户问题前面，形成"资料+问题"的用户消息
            effective_user_input = search_context_for_user + user_input

        # 5. 调用LLM获取回复（使用拼接后的用户消息）
        if _t_search:
            print(f"[计时] 搜索耗时: {time.time()-_t_search:.1f}s")
        _t_llm = time.time()
        if self.model_name.startswith("gpt"):
            reply = self.get_response_from_openai(effective_user_input, combined_context)
        elif self._is_ollama_model():
            reply = self.get_response_from_ollama(effective_user_input, combined_context)
        elif self.model_name.startswith("qwen"):
            reply = self.get_response_from_qwen(effective_user_input, combined_context)
        else:
            reply = self.get_response_from_openai(effective_user_input, combined_context)
        print(f"[计时] LLM推理耗时: {time.time()-_t_llm:.1f}s")

        # 5.5 推脱检测 + 自动重试（最多1次）
        # 询问达妮娅本人信息时的推脱是期望的傲娇行为，不重试
        if self._is_deflecting(reply) and search_triggered and not self._is_personal_question:
            print(f"[推脱检测] 模型回答在推脱，自动重试（anti-deflect注入）")
            # 重试：在用户消息里直接注入反推脱指令
            retry_input = user_input
            if search_context_for_user:
                retry_input = search_context_for_user + user_input
            retry_input += "\n\n【重要提醒】直接回答上面的问题，不要说'去查''看公告''关注官方'等，就像你已经知道答案一样回答。"
            # 撤回刚才的历史记录（推脱的回复不该进入历史）
            if len(self.conversation_history) >= 2:
                self.conversation_history = self.conversation_history[:-2]
            saved_history = list(self.conversation_history)

            if self._is_ollama_model():
                retry_reply = self.get_response_from_ollama(retry_input, "")
            elif self.model_name.startswith("qwen"):
                retry_reply = self.get_response_from_qwen(retry_input, "")
            else:
                retry_reply = self.get_response_from_openai(retry_input, "")

            if not self._is_deflecting(retry_reply):
                # 重试成功：移除重试的历史（含anti-deflect指令），写入干净的历史
                if len(self.conversation_history) >= 2:
                    self.conversation_history = self.conversation_history[:-2]
                self.update_history("user", user_input)
                self.update_history("assistant", retry_reply)
                reply = retry_reply
                print(f"[推脱检测] 重试成功，使用了修正后的回复")
            else:
                # 重试也推脱了，恢复原始历史（保留原始推脱回复）
                self.conversation_history = saved_history
                self.update_history("user", user_input)
                self.update_history("assistant", reply)
                print(f"[推脱检测] 重试仍推脱，保留原始回复")

        # 6. 自动提取记忆（异步，不影响回复速度）
        if self.memory:
            try:
                search_result_text = search_context_for_user if search_context_for_user else ""
                self.memory.extract_and_save(user_input, reply, search_result_text)
            except Exception:
                pass
        
        # 7. 个性系统：话题延续
        if self.personality:
            try:
                continuation = self.personality.get_topic_continuation()
                if continuation:
                    reply = f"{reply} {continuation}"
            except Exception as e:
                print(f"[个性] 话题延续失败: {e}")

        print(f"[计时] 总耗时: {time.time()-_t0:.1f}s" + ("（含搜索+LLM）" if _t_search else "（仅LLM）"))
        return reply
    
    def clear_history(self):
        """清空对话历史"""
        self.conversation_history.clear()

    def _is_dania_personal_question(self, user_input: str) -> bool:
        """检测是否询问达妮娅本人信息

        这类问题期望傲娇推脱回复（如"秘密哦""才不告诉你""哼，不告诉你"），
        不触发搜索、不被推脱检测修正，更贴合傲娇人设。
        """
        personal_kws = [
            "多大", "几岁", "年龄", "生日", "星座", "血型", "贵庚",
            "住哪", "哪里人", "地址", "家在哪", "住在哪里",
            "三围", "身高", "体重", "胸围", "罩杯",
            "喜欢谁", "男朋友", "女朋友", "对象", "有没有喜欢的人",
            "谈过恋爱", "谈过几次", "感情经历", "初恋",
            "真名", "本名", "全名", "真名叫",
            "电话", "微信", "qq号", "联系方式",
            "哪天", "什么时候生日",
            "喜欢吃什么", "你的爱好", "你的兴趣",
        ]
        has_personal = any(kw in user_input for kw in personal_kws)
        has_you = "你" in user_input
        # 含个人信息关键词，且指向达妮娅本人（含"你"或直呼名字）
        return has_personal and (has_you or "达妮娅" in user_input or "小妮" in user_input)

    def _is_deflecting(self, reply: str) -> bool:
        """检测模型回复是否在推脱让用户自己查"""
        if not reply or len(reply) < 5:
            return False
        # 推脱模式：让用户自己去查、去看公告、去别处找
        deflect_patterns = [
            r'去(查|看看|搜|找|查阅|了解一下)',
            r'你可以(去|自己|试着)',
            r'(可以|建议|最好)(关注|看看|查阅|浏览)',
            r'(看|查|关注|留意).{0,4}(公告|官网|官方)',
            r'自己(查|搜|找|看|去|了解一下)',
            r'(自行|亲自动手|你去)',
            r'(不知道|不清楚).{0,6}(你|建议|可以).{0,4}(查|看|搜|找|关注)',
            r'(查一查|看一看|了解一下|去查|去看)',
            r'得(查|看|搜|找|去)',
        ]
        for pattern in deflect_patterns:
            if re.search(pattern, reply):
                return True
        return False
    
    def _prune_redundant_history(self):
        """删除历史中过于相似的连续消息，保持对话新鲜度"""
        if len(self.conversation_history) < 2:
            return
        
        # 简单的相似度检查：如果两条相邻消息的内容太相似，删除较早的那一条
        # 这样可以防止LLM重复说同一个东西
        pruned = [self.conversation_history[0]]
        
        for i in range(1, len(self.conversation_history)):
            current = self.conversation_history[i]["content"]
            previous = self.conversation_history[i-1]["content"]
            
            # 计算相似度（使用分词而不是字符集合）
            if len(current) > 10 and len(previous) > 10:
                current_words = set(current.split())
                previous_words = set(previous.split())
                # 如果重叠度超过70%，认为太相似
                if len(current_words & previous_words) / max(len(current_words), len(previous_words)) > 0.7:
                    # 跳过这条消息
                    continue
            
            pruned.append(self.conversation_history[i])
        
        # 保持最多20条消息
        if len(pruned) > 20:
            self.conversation_history = pruned[-20:]
        else:
            self.conversation_history = pruned
    
    def _get_timeout_response(self) -> str:
        """网络超时时的回复 - 更含蓄害羞"""
        responses = [
            "……嗯，稍微等一下好吗？",
            "那个……能等一下吗……",
            "……等一下哦……",
            "嗯……稍等……",
        ]
        return random.choice(responses)
    
    def _get_error_response(self, error_msg: str) -> str:
        """API错误时的回复 - 更含蓄害羞"""

        # 根据错误类型给出不同回复，但都保持温柔含蓄
        if "quota" in error_msg.lower() or "limit" in error_msg.lower():
            responses = [
                "……今天有点累了呢……",
                "那个……明天再聊好吗……",
            ]
        elif "key" in error_msg.lower() or "auth" in error_msg.lower():
            responses = [
                "……好像有点问题呢……",
                "那个……出了点状况……",
            ]
        else:
            responses = [
                "……稍等一下好吗……",
                "那个……等一下哦……",
                "……嗯……",
            ]
        
        return random.choice(responses)
