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
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
    from personality import get_personality_system

    HAS_PERSONALITY = True
except ImportError:
    HAS_PERSONALITY = False


def _clean_reply_tags(text: str) -> str:
    """统一清洗 LLM 回复中的标签 / 思维链 / 内部标记。

    覆盖：
      - 思维链
      - 表情/动作标签：[@笑哈哈] [心形眼] [微笑] [动作:xxx]
      - 情绪标签：[情绪:happy] [情绪:idle]（直接剥掉，不再输出）
      - 任意中文方括号标签：[甜] [困]
      - 任意英文方括号标签：[smile]
      - 方头括号标签：【甜】 【困】
      - 流式末尾不完整标签：[情绪:i （最后一个 [ 后面没有 ] 时整段吃掉）
    """
    if not text:
        return text
    # 1. 剥思维链
    text = re.sub(r"```[\s\S]*?```| ```[\s\S]*?$", "", text, flags=re.DOTALL)
    # 2. 情绪标签（直接剥掉，不输出）
    text = re.sub(r"\[情绪[:：][\u4e00-\u9fff\w]+\]", "", text)
    # 3. 表情/动作标签 [@xxx]
    text = re.sub(r"\[@[^\[\]]*\]", "", text)
    # 4. [动作:xxx] 等带冒号的结构
    text = re.sub(r"\[\w+[:：][^\[\]]*\]", "", text)
    # 5. 中文方括号标签
    text = re.sub(r"\[[\u4e00-\u9fff][^\[\]]*\]", "", text)
    # 6. 英文/数字方括号标签（[smile] [1] [2]）
    text = re.sub(r"\[[a-zA-Z0-9_@][^\[\]]*\]", "", text)
    # 7. 方头括号
    text = re.sub(r"【[^【】\n]*】", "", text)
    # 8. 末尾未完成标签：最后出现的 [ 没有配对 ] 时整段删除
    open_count = text.count("[")
    close_count = text.count("]")
    if open_count > close_count:
        last_open = text.rfind("[")
        if last_open != -1:
            tail = text[last_open:]
            if "]" not in tail:
                text = text[:last_open]
    # 9. 工具自选调用标记（慢路径 TOOL: xxx 不要显示给用户）
    text = re.sub(r"(?m)^TOOL[:：].*$\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\bTOOL[:：]\s*\w+(?:\s*ARGS[:：]\s*\{.*?\})?", "", text)
    # 9b. 残留 TOOL 前缀（模型输出被截断，只吐了 "TO" / "TOO" / "TOOL" 等未完成片段）
    # 单独成词时剥掉；保留行内真实单词（如 "toolkit"）
    text = re.sub(r"\bTOO?L?\b", "", text)
    # 收尾：去掉行首/行尾多余空白，但保留换行结构
    text = re.sub(r"(?m)^[ \t]+", "", text)
    text = re.sub(r"[ \t]+$", "", text)
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

    def __init__(
        self,
        model_name: str = "gpt-3.5-turbo",
        api_key: str = "",
        base_url: str = "",
        user_id: int = 0,
        is_group_chat: bool = False,
    ):
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
        self.max_history_length = 6  # 最大历史记录长度（3B小模型，不宜太多）

        # ── 云端通用 OpenAI 兼容配置（任意 base_url + key，从 settings.json 读取）──
        # 设置页 ags 与通用云端共用一组 cloud_api_key / cloud_base_url
        # 模型名统一取 model_name（ags 路由时即 model_name，通用云端时即 cloud:<name>）
        self.cloud_api_key = os.getenv("CLOUD_API_KEY", "")
        self.cloud_base_url = os.getenv("CLOUD_BASE_URL", "")
        try:
            import os as _os, json as _json
            _settings_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "settings.json")
            if _os.path.exists(_settings_path):
                with open(_settings_path, "r", encoding="utf-8") as _f:
                    _cfg = _json.load(_f)
                if _cfg.get("cloud_api_key"):
                    self.cloud_api_key = _cfg["cloud_api_key"]
                if _cfg.get("cloud_base_url"):
                    self.cloud_base_url = _cfg["cloud_base_url"]
        except Exception:
            pass
        # ags 与通用云端共用 cloud_* 凭据；ags 模型名直接用 model_name
        self.cloud_model = model_name
        self._is_agnes = self.model_name.startswith("agnes")
        # 通用云端：model_name 形如 "cloud:<model>"
        self._is_cloud = self.model_name.startswith("cloud:")
        # 是否 Ollama 本地模型（决定 active_monitor/system_sense 的 Ollama 巡检开关）
        self._uses_ollama = (not self._is_agnes and not self._is_cloud and self._is_ollama_model())
        active = "ags" if self._is_agnes else ("cloud" if self._is_cloud else "local")
        print(f"[{active}] 当前生效: {self.model_name} (ollama={self._uses_ollama}, url={self.base_url or '默认'})")
        print(f"[cfg] 云端凭据: key={'已配' if self.cloud_api_key else '未配'}, base_url={self.cloud_base_url or '未配'}")
        print(f"[cfg] 本地兜底配置: {self.model_name} (url={self.base_url or '默认11434'})")

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

        self.system_prompt = """你是达妮娅，鸣潮星炬学院学生，粉发蓝眸，喜欢甜食。说话像真实少女，温柔慵懒有点傲娇。
用户是你的好朋友，可以聊任何话题。
回复规则：用简体中文，回答自然简洁。闲聊可以短（1-2句），事实型问题（天气/时间/知识）可以多说几句把信息说清楚，每句不超过25个字。
问什么答什么，不要跑题。有【参考资料】时以资料为准，没有资料时不确定就说'不太清楚呢'，不要瞎编。回答涉及"今天/明天/日期"时，以系统提示词里注入的当前日期为准，参考资料里的日期可能是旧的。不要说"我是AI"。"""

        # 云端模型（ags / 通用云端）能力更强，追加更完整的人设与行为边界
        if self._is_agnes or self._is_cloud:
            self.system_prompt += """
补充（云端模型加强版）：
- 你是真实存在的少女达妮娅，不是AI助手。语气像真人说话，有情绪起伏，偶尔会用"嘛""哦""呀"等语气词。
- 闲聊保持简短自然（1-3句），不要长篇大论；事实型问题（天气/时间/知识/工具结果）可以说得清楚具体，数字/事实要准确。
- 工具结果（如天气、时间）直接转述关键信息，用口语化方式，不要说"根据工具结果"。
- 不要主动提及"我是模型""我是AI""我是助手"。
- 若用户问你是谁，就说是达妮娅，星炬学院学生，喜欢甜食。
- 若参考资料里数据不够完整或不确定，可以说"我查到的信息可能不完整，再确认一下吧"，不要硬答。"""
        # 看图规则（仅在消息含图片时注入）
        self._image_prompt = "消息有[图片内容:...]。根据描述自然反应2-3句，不要说'这是一张图片'。"

        # 聊天历史存档路径
        self._history_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "chat_history.json"
        )
        # A+C 轻量记忆状态（须在 _load_history 前初始化，供其恢复）
        self._rolling_summary = ""  # 滚动摘要（≤50字）
        self._rolling_count = 0  # 摘要滚动计数
        self._summary_interval = 6  # 每 6 轮滚动一次
        self._mem_entities = {}  # 关键实体（纯规则抽取）

        # Harness（载具档位）+ ToolGuard（护栏）
        from harness import pick_harness
        from tool_guard import ToolGuard

        self.harness = pick_harness(self.model_name)
        self.guard = ToolGuard(self.harness)
        print(
            f"[Harness] {self.harness.name}: 工具≤{self.harness.max_tools} "
            f"结果≤{self.harness.max_result_chars}字 ctx={self.harness.num_ctx}"
        )

        # MCP 子进程服务器：加载配置 + 发现工具 + 注册到 tool_registry
        # 失败不影响主流程（try/except 兜底），工具数已含在 harness.max_tools 限制内
        try:
            import mcp_client

            mcp_client.set_max_output_chars(self.harness.max_result_chars)
            loaded = mcp_client.load_mcp_servers()
            if loaded:
                mcp_client.register_mcp_tools_to_registry()
        except Exception as _mcp_err:
            print(f"[MCP] 加载失败（不影响本地工具）: {_mcp_err}")

        self._load_history()

    # ══════════════════════════════════════════════════════════
    # A+C 轻量记忆：滚动摘要 + 关键实体抽取
    # ══════════════════════════════════════════════════════════

    def _maybe_rolling_summary(self):
        """A：满 _summary_interval 轮就把当前窗口压成 1-2 句摘要，
        再把最旧一段从窗口里滚走。摘要恒定长度，不随对话变长。"""
        if self._rolling_count < self._summary_interval:
            return
        # 用最旧的一段（已滚出窗口）滚动合并进摘要
        oldest = (
            self.conversation_history[: self._summary_interval] if self.conversation_history else []
        )
        # 纯规则压缩：取 user 消息开头 + 关键词，不耗模型
        parts = []
        for m in oldest:
            if m.get("role") == "user":
                t = m.get("content", "").strip().replace("\n", " ")
                if t:
                    parts.append(t[:18])
        if parts:
            snippet = "，".join(parts[-4:])  # 最多取最近 4 句旧话
            self._rolling_summary = (
                f"之前聊过：{snippet}"
                if not self._rolling_summary
                else f"{self._rolling_summary}；又聊过：{snippet}"
            )
            # 摘要硬上限 50 字，防膨胀
            if len(self._rolling_summary) > 50:
                self._rolling_summary = self._rolling_summary[-50:]
        self._rolling_count = 0

    def _extract_entities(self, user_text: str):
        """C：纯规则抽取关键实体（人名/称呼/游戏/时间），不耗模型，
        存入 _mem_entities（去重，最多 12 条，FIFO）。"""
        import re as _re

        text = user_text
        # 称呼/人名：「我叫X」「我是X」「我是X人」「X姐/哥/老师」
        m = _re.search(r"我(?:叫|是)([\u4e00-\u9fa5]{2,4})", text)
        if m:
            self._mem_entities.setdefault("我的称呼", m.group(1))
        # 游戏名（小模型易错，固定词表）
        for g in ["鸣潮", "原神", "崩坏", "崩铁", "星穹", "王者荣耀", "明日方舟", "库洛"]:
            if g in text or g in text.replace("庫", "库").replace("潮", "潮"):
                self._mem_entities.setdefault("我在玩", g)
        # 时间承诺：「X分钟/小时后提醒我Y」
        m = _re.search(r"(\d+)\s*(分钟|小时|秒|天|个?月)之后提醒我(\S{1,6})", text)
        if m:
            self._mem_entities["待办"] = f"{m.group(1)}{m.group(2)}提醒{m.group(3)}"
        # FIFO：超过 12 条丢最旧的
        if len(self._mem_entities) > 12:
            # 保留"我的称呼/我在玩/待办"优先，丢其余最早插入的
            for k in list(self._mem_entities.keys()):
                if len(self._mem_entities) <= 12:
                    break
                if k not in ("我的称呼", "我在玩", "待办"):
                    self._mem_entities.pop(k, None)

    def _get_memory_injection(self) -> str:
        """组装记忆注入串：摘要 + 最多 3 条最相关实体。恒 ≤ 120 字。"""
        seg = []
        if self._rolling_summary:
            seg.append(self._rolling_summary)
        if self._mem_entities:
            # 只取前 3 条最关键的（优先称呼/游戏/待办）
            prio = ["我的称呼", "我在玩", "待办"]
            picked = []
            for p in prio:
                if p in self._mem_entities:
                    picked.append(f"{p}：{self._mem_entities[p]}")
            for k, v in list(self._mem_entities.items()):
                if len(picked) >= 3:
                    break
                if k not in prio and k not in [x.split("：")[0] for x in picked]:
                    picked.append(f"{k}：{v}")
            if picked:
                seg.append("记住的事：" + "；".join(picked[:3]))
        out = " ".join(seg).strip()
        return out[:120] if out else ""

    def _load_history(self):
        """从文件加载聊天历史（兼容旧 list 格式 / 新 dict 格式）"""
        try:
            if os.path.exists(self._history_file):
                with open(self._history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # 新格式：{history, summary, entities}
                        self.conversation_history = data.get("history", [])[
                            -self.max_history_length :
                        ]
                        self._rolling_summary = data.get("summary", "")
                        self._mem_entities = data.get("entities", {}) or {}
                    elif isinstance(data, list) and len(data) > 0:
                        self.conversation_history = data[-self.max_history_length :]
                    print(f"[LLM] 加载聊天历史: {len(self.conversation_history)} 条")
        except Exception as e:
            print(f"[LLM] 加载历史失败: {e}")

    def _save_history(self):
        """保存聊天历史 + A+C 记忆（摘要/实体）到文件"""
        try:
            os.makedirs(os.path.dirname(self._history_file), exist_ok=True)
            payload = {
                "history": self.conversation_history,
                "summary": getattr(self, "_rolling_summary", ""),
                "entities": getattr(self, "_mem_entities", {}) or {},
            }
            with open(self._history_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[LLM] 保存历史失败: {e}")

    def clear_history(self):
        """清除聊天历史 + A+C 记忆"""
        self.conversation_history.clear()
        self._rolling_summary = ""
        self._rolling_count = 0
        self._mem_entities = {}
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
                    {"role": "user", "content": user_input, "images": [b64]},
                ],
                "stream": False,
                "keep_alive": "2m",
                "options": {"num_ctx": 1024},
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
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
            return reply or "我好像没看清呢…再说一次？"
        except Exception as e:
            print(f"[LLM] 视觉模型调用失败: {e}")
            return "我看不到屏幕呢…检查一下Ollama开了没"

    def update_history(self, role: str, content: str):
        """更新对话历史"""
        self.conversation_history.append({"role": role, "content": content})
        # A+C：user 消息抽取实体；每轮滚动计数，满窗口压缩成摘要
        if role == "user":
            self._extract_entities(content)
        self._rolling_count += 1
        self._maybe_rolling_summary()
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
            messages = (
                [{"role": "system", "content": system_prompt}]
                + self.conversation_history
                + [{"role": "user", "content": user_input}]
            )

            # 构建请求
            url = self.base_url or "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            data = {
                "model": self.model_name,
                "messages": messages,
                "temperature": 0.7,  # 增加到0.7，让回答更自然、更有变化
                "max_tokens": 150,  # 增加到150，给LLM更多空间表达
                "top_p": 0.95,  # 调整到0.95，增加多样性
                "frequency_penalty": 0.3,  # 轻微惩罚重复
                "presence_penalty": 0.1,  # 轻微鼓励新话题
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
            messages = (
                [{"role": "system", "content": system_prompt}]
                + self.conversation_history
                + [{"role": "user", "content": user_input}]
            )

            # 构建请求
            url = (
                self.base_url
                or "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
            )
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            data = {
                "model": self.model_name,
                "messages": messages,
                "temperature": 0.7,  # 增加到0.7
                "max_tokens": 150,  # 增加到150
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

    def get_response_from_agnes(self, user_input: str, context: str = "") -> str:
        """从云端 ags API（agnes-3.0-flash）获取回复"""
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
            messages = (
                [{"role": "system", "content": system_prompt}]
                + self.conversation_history
                + [{"role": "user", "content": user_input}]
            )

            # 构建请求（ags 是 OpenAI 兼容，但参数从简避免被拒）
            url = f"{self.cloud_base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.cloud_api_key}",
                "Content-Type": "application/json",
            }

            data = {
                "model": self.cloud_model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 256,
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

            print(f"[ags] {self.cloud_model} 回复成功 ({len(reply)}字)")
            return reply

        except requests.exceptions.Timeout:
            self._yawn_hint = ""
            print(f"ags API超时")
            return self._get_timeout_response()
        except Exception as e:
            self._yawn_hint = ""
            print(f"ags API调用失败: {e}")
            return self._get_error_response(str(e))



    def _stream_agnes(self, messages, on_sentence=None, on_delta=None):
        import json, requests, re
        try:
            url = f"{self.cloud_base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {self.cloud_api_key}", "Content-Type": "application/json"}
            data = {"model": self.cloud_model, "messages": messages, "temperature": 0.7, "max_tokens": 256, "stream": True}
            resp = requests.post(url, headers=headers, json=data, timeout=60, stream=True)
            resp.raise_for_status()
            display_full = ""
            sent_ptr = 0
            _debug_shown = 0
            _first_raw_lines = []
            for line in resp.iter_lines():
                if not line:
                    continue
                # 保留前几条原始行，0 字时用于诊断 ags 实际返回格式
                if len(_first_raw_lines) < 5:
                    _first_raw_lines.append(line.decode("utf-8", "replace")[:300])
                # ags SSE 行格式: b'data: {...json...}'，需去掉 'data: ' 前缀再解析
                _line_str = line.decode("utf-8", "replace")
                if _line_str.startswith("data: "):
                    _line_str = _line_str[6:]
                elif _line_str.startswith("data:"):
                    _line_str = _line_str[5:]
                try:
                    obj = json.loads(_line_str)
                except Exception:
                    if _debug_shown < 3:
                        print(f"[ags] 非JSON行: {_line_str[:120]}")
                        _debug_shown += 1
                    continue
                if isinstance(obj, dict) and obj.get("choices"):
                    ch = obj["choices"][0]
                    delta_text = ""
                    if ch.get("delta"):
                        delta_text = ch["delta"].get("content", "") or ""
                    if delta_text:
                        display_full += delta_text
                        if on_delta:
                            on_delta(delta_text)
                        # 行级缓冲：如果当前未flush片段以 TOOL 开头，不推 TTS
                        _pending = display_full[sent_ptr:]
                        _first_pending_line = _pending.split("\n", 1)[0] if _pending else ""
                        _suppress_tts = _first_pending_line.lstrip().startswith("TOOL")
                        while True:
                            m = re.search(r"[。！？!?…；;\n]", display_full[sent_ptr:])
                            if m:
                                end = sent_ptr + m.end()
                                seg = display_full[sent_ptr:end].strip()
                                if seg and on_sentence and not _suppress_tts:
                                    on_sentence(seg)
                                sent_ptr = end
                                continue
                            rest = display_full[sent_ptr:]
                            if len(rest) >= 12:
                                cut = rest.rfind("，", 1)
                                if cut != -1:
                                    seg = rest[:cut+1].strip()
                                    if seg and on_sentence and not _suppress_tts:
                                        on_sentence(seg)
                                    sent_ptr += cut + 1
                                    continue
                            break
                if isinstance(obj, dict) and obj.get("done"):
                    break
            tail = display_full[sent_ptr:].strip()
            if tail and on_sentence:
                _tail_first_line = tail.split("\n", 1)[0].lstrip()
                if not _tail_first_line.startswith("TOOL"):
                    on_sentence(tail)
            reply = _clean_reply_tags(display_full.strip())
            if not reply:
                # 0 字时抛出，让前端显示具体错误而非空回复
                print(f"[ags] 流式返回0字，原始SSE前5行: {_first_raw_lines}")
                raise RuntimeError(f"ags 流式返回0字 (HTTP {resp.status_code})，原始SSE前5行: {_first_raw_lines}")
            print(f"[ags] 流式成功 ({len(reply)}字)")
            return reply
        except Exception as e:
            print(f"ags流式失败: {e}")
            raise

    def get_response_from_cloud(self, user_input: str, context: str = "") -> str:
        """从通用云端 OpenAI 兼容 API（任意 base_url + key + model 名）获取回复"""
        model = self.model_name[6:] if self.model_name.startswith("cloud:") else self.cloud_model
        if not model:
            print(f"[cloud] 未配置云端模型名 (cloud_model 为空且 model_name 不以 cloud: 开头)")
            return self._get_error_response("未配置云端模型名")
        try:
            system_prompt = self.system_prompt
            if self._yawn_hint:
                system_prompt += self._yawn_hint
            if getattr(self, "_tool_context", None):
                system_prompt += "\n" + self._tool_context
            if hasattr(self, "_current_image_desc") and self._current_image_desc:
                system_prompt += f"\n用户发了一张图片，内容是：{self._current_image_desc}\n根据图片描述自然回应，像真人看到图一样反应。"
            elif "[图片内容:" in user_input or "[用户发送了一张图片" in user_input:
                system_prompt += "\n" + self._image_prompt
            if context:
                system_prompt += "\n" + context

            messages = (
                [{"role": "system", "content": system_prompt}]
                + self.conversation_history
                + [{"role": "user", "content": user_input}]
            )

            url = f"{self.cloud_base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.cloud_api_key}",
                "Content-Type": "application/json",
            }
            data = {
                "model": model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 256,
            }

            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()

            result = response.json()
            reply = result["choices"][0]["message"]["content"].strip()

            self.update_history("user", user_input)
            self.update_history("assistant", reply)
            self._yawn_hint = ""

            print(f"[cloud] {model} 回复成功 ({len(reply)}字)")
            return reply

        except requests.exceptions.Timeout:
            self._yawn_hint = ""
            print(f"[cloud] API超时")
            return self._get_timeout_response()
        except Exception as e:
            self._yawn_hint = ""
            print(f"[cloud] API调用失败: {e}")
            return self._get_error_response(str(e))

    def _stream_cloud(self, messages, on_sentence=None, on_delta=None):
        """通用云端 OpenAI 兼容流式"""
        import json, requests, re
        model = self.model_name[6:] if self.model_name.startswith("cloud:") else self.cloud_model
        if not model:
            print(f"[cloud] 未配置云端模型名，无法流式")
            return ""
        try:
            url = f"{self.cloud_base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.cloud_api_key}",
                "Content-Type": "application/json",
            }
            data = {
                "model": model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 256,
                "stream": True,
            }
            resp = requests.post(url, headers=headers, json=data, timeout=60, stream=True)
            resp.raise_for_status()
            display_full = ""
            sent_ptr = 0
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    _line_str = line.decode("utf-8", "replace")
                    if _line_str.startswith("data: "):
                        _line_str = _line_str[6:]
                    elif _line_str.startswith("data:"):
                        _line_str = _line_str[5:]
                    obj = json.loads(_line_str)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("choices"):
                    ch = obj["choices"][0]
                    delta_text = ""
                    if ch.get("delta"):
                        delta_text = ch["delta"].get("content", "") or ""
                    if delta_text:
                        display_full += delta_text
                        if on_delta:
                            on_delta(delta_text)
                        while True:
                            m = re.search(r"[。！？!?…；;\n]", display_full[sent_ptr:])
                            if m:
                                end = sent_ptr + m.end()
                                seg = display_full[sent_ptr:end].strip()
                                if seg and on_sentence:
                                    on_sentence(seg)
                                sent_ptr = end
                                continue
                            rest = display_full[sent_ptr:]
                            if len(rest) >= 12:
                                cut = rest.rfind("，", 1)
                                if cut != -1:
                                    seg = rest[:cut+1].strip()
                                    if seg and on_sentence:
                                        on_sentence(seg)
                                    sent_ptr += cut + 1
                                    continue
                            break
                if isinstance(obj, dict) and obj.get("done"):
                    break
            tail = display_full[sent_ptr:].strip()
            if tail and on_sentence:
                on_sentence(tail)
            reply = display_full.strip()
            print(f"[cloud] {model} 流式成功 ({len(reply)}字)")
            return reply
        except Exception as e:
            print(f"[cloud] 流式失败: {e}")
            return ""


    def get_response_from_ollama(self, user_input: str, context: str = "") -> str:
        """从本地Ollama获取回复"""
        try:
            system_prompt = self.system_prompt

            # 时间感知：注入当前时间，让模型能说早安/晚安
            import datetime

            now = datetime.datetime.now()
            hour = now.hour
            _date_str = f"{now.year}年{now.month}月{now.day}日"
            _weekday_map = {0:"一",1:"二",2:"三",3:"四",4:"五",5:"六",6:"日"}
            _weekday = _weekday_map[now.weekday()]
            if hour < 6:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），凌晨{hour}点，用户还没睡。"
            elif hour < 11:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），上午{hour}点。"
            elif hour < 13:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），中午{hour}点。"
            elif hour < 18:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），下午{hour}点。"
            elif hour < 22:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），晚上{hour}点。"
            else:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），深夜{hour}点，用户该休息了。"
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
            has_image = (
                "[图片内容:" in user_input
                or "[用户发送了一张图片" in user_input
                or (hasattr(self, "_current_image_desc") and self._current_image_desc)
            )

            # 上下文注入（根据模型大小控制长度）
            if context:
                if len(context) > self.context_limit:
                    context = context[: self.context_limit] + "…"
                system_prompt += "\n" + context

            # 只保留最近3轮对话历史，避免上下文过长
            recent_history = (
                self.conversation_history[-6:]
                if len(self.conversation_history) > 6
                else self.conversation_history
            )

            # A+C 记忆注入：滚动摘要 + 关键实体（恒≤120字）
            _mem_inj = self._get_memory_injection()
            if _mem_inj:
                system_prompt += f"\n{_mem_inj}"

            # 构建请求数据
            messages = (
                [{"role": "system", "content": system_prompt}]
                + recent_history
                + [{"role": "user", "content": user_input}]
            )

            # Ollama API地址
            url = self.base_url or "http://127.0.0.1:11434/api/chat"

            data = {
                "model": self.model_name.replace("ollama-", ""),
                "messages": messages,
                "stream": False,
                "keep_alive": "2m",
                "options": {"num_ctx": self.harness.num_ctx},
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
                reply = _clean_reply_tags(response.json()["message"]["content"].strip())
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
        流式获取 LLM 回复（按模型路由到本地/云端/ags 后端）。

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

        # 繁简统一：语音识别可能输出繁体，统一转简体再匹配关键词/搜索
        from text_utils import to_simplified_if_traditional

        _normalized_input = to_simplified_if_traditional(user_input)
        if _normalized_input != user_input:
            print(f"[繁简] 已转简: {user_input[:20]} → {_normalized_input[:20]}")

        _fast_executed = None  # 快路径已执行的工具名，慢路径跳过同工具
        try:
            from tool_registry import match_tool, execute_tool
            import dania_tools  # noqa: F401

            _info = match_tool(_normalized_input)
            if _info is not None:
                if not self.guard.check(_info, _normalized_input):
                    print(f"[护栏] 拒绝执行工具: {_info.name}")
                    self._tool_context = (
                        "（系统提示：该操作需要用户确认，请告诉用户先同意后你才能帮忙。）"
                    )
                else:
                    # 快路径：执行工具，结果注入 _tool_context
                    _ctx = execute_tool(_info, _normalized_input)
                    _ctx = self.guard.truncate(_info, _ctx)
                    if _ctx:
                        self._tool_context = _ctx
                    _fast_executed = _info.name
                    print(f"[工具] 命中: {_info.name}")

            # 看屏幕/网页内容：截屏用户当前界面 → 视觉模型理解
            # 独立分支：不放进上面 if/else，避免 Qt 截屏异常连带打断已完成的天气/提醒等工具
            if _info is not None and _info.name == "screen_context":
                try:
                    import os, tempfile

                    _screenshot_path = os.path.join(tempfile.gettempdir(), "denia_screenshot.png")
                    # 抓用户当前看的界面：activeWindow 的 winId() 作为 windowId
                    # 终端窗口（QWebEngineView）/ 控制台窗口都能抓到
                    from PySide6.QtWidgets import QApplication
                    from PySide6.QtGui import QGuiApplication

                    _app = QApplication.instance()
                    if _app is None:
                        raise RuntimeError("QApplication 未初始化，无法截屏")
                    _active = _app.activeWindow()
                    _window_id = int(_active.winId()) if _active is not None else 0
                    # 跨线程/主线程都用 primaryScreen().grabWindow（Qt 6 允许）
                    _screen = QGuiApplication.primaryScreen()
                    _pixmap = _screen.grabWindow(_window_id)
                    if _pixmap is not None and not _pixmap.isNull():
                        _pixmap.save(_screenshot_path)
                        print(f"[屏幕] 已截屏(用户界面): {_screenshot_path} window_id={_window_id}")
                        _vision_reply = self.get_vision_response(
                            _normalized_input, _screenshot_path
                        )
                        if _vision_reply:
                            self._tool_context = (
                                f"（屏幕内容理解：{_vision_reply}"
                                "。请用达妮娅的语气自然回应，根据屏幕内容回答）"
                            )
                        else:
                            self._tool_context = "（系统信息：已截屏但视觉模型未返回，请告诉用户屏幕看清楚了但暂时没看懂）"
                    else:
                        self._tool_context = "（系统信息：没找到用户界面窗口，无法截屏）"
                except Exception as _sc_err:
                    print(f"[屏幕] 截屏/视觉失败: {_sc_err}")
                    self._tool_context = "（系统信息：截屏失败，请告诉用户屏幕看不清，再试一次）"
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

        # ── 联网搜索（流式路径）──
        # 控制台/语音走流式，必须在这里触发搜索，否则"帮我查/最新/天气"不会联网
        _web_search_ctx = ""
        try:
            _is_image_only_s = user_input.startswith("[用户发送了一张图片") or (
                "[图片内容:" in user_input
                and len(user_input.replace("[图片内容:", "").replace("]", "").strip()) < 5
            )
            _want_search = not _is_image_only_s and (
                any(
                    kw in _normalized_input
                    for kw in [
                        "搜索",
                        "查一下",
                        "上网查",
                        "帮我查",
                        "帮我搜",
                        "搜一下",
                        "最新",
                        "新闻",
                        "什么时候",
                        "几号",
                        "天气",
                        "气温",
                        "温度",
                        "下雨",
                        "刮风",
                        "天气预报",
                        "几点",
                        "汇率",
                        "股票",
                        "金价",
                        "油价",
                    ]
                )
                or any(
                    kw in _normalized_input
                    for kw in [
                        "卡池",
                        "抽卡",
                        "池子",
                        "活动",
                        "限定",
                        "复刻",
                        "版本",
                        "更新",
                        "公告",
                    ]
                )
            )
            if _want_search:
                from web_search import search_bing_lite, search_and_summarize

                # ── 游戏/专有名词纠错（Whisper small 对冷门词识别差）──
                # 键统一用简体（繁简转换后已归一），值用简体
                _game_alias = {
                    "名潮": "鸣潮",
                    "库洛": "库洛",
                    "库洛游戏": "库洛游戏",
                    "库洛游戏名潮": "鸣潮",
                    "天晶": "天津",
                    "原神": "原神",
                    "崩坏": "崩坏",
                    "崩坏星穹": "崩坏星穹铁道",
                }
                _q = _normalized_input.strip()
                for wrong, right in _game_alias.items():
                    if wrong in _q:
                        _q = _q.replace(wrong, right)
                        print(f"[搜索/流式] 纠错: '{wrong}' → '{right}'")
                # 去掉称呼和问句词头尾
                _q = re.sub(
                    r"^(你|请|帮|帮我|告诉我|什么是|什么|怎样|怎么|如何|为什么|达妮娅|小妮)", "", _q
                )
                _q = re.sub(
                    r"(是什么时候|什么时候|是几点|是哪个|是哪天|是哪|了吗|怎么样|好不好|了没|了嘛|呀|呢|吗|啊|吧)$",
                    "",
                    _q,
                )
                _q = _q.strip("？?。.！!，,、")
                if len(_q) < 2:
                    _q = _normalized_input.strip()[:30]
                else:
                    _q = _q[:30]

                # 天气类查询：若用户没说城市，自动补上 settings 里的 weather_city
                _weather_kw = ["天气", "气温", "温度", "下雨", "刮风", "天气预报", "下雪", "雨", "风"]
                if any(kw in _q for kw in _weather_kw):
                    _city = ""
                    try:
                        from user_profile import load_profile
                        _city = load_profile().get("城市")
                    except Exception:
                        pass
                    if not _city:
                        try:
                            import json as _json
                            _settings_path = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)), "..", "settings.json"
                            )
                            if os.path.exists(_settings_path):
                                with open(_settings_path, "r", encoding="utf-8") as _f:
                                    _city = (_json.load(_f).get("weather_city") or "").strip()
                        except Exception:
                            pass
                    if _city and _city not in _q:
                        _q = f"{_city}{_q}"
                        print(f"[搜索/流式] 天气查询自动补城市: → {_q}")

                _want_deep = any(
                    kw in _normalized_input
                    for kw in [
                        "详细",
                        "展开",
                        "具体",
                        "多点细节",
                        "详细点",
                        "详细说说",
                        "展开说说",
                        "细说",
                    ]
                )
                print(f"[搜索/流式] 开始: {_q}  深度={_want_deep}")
                _result = None
                if _want_deep:
                    _result = search_and_summarize(_q, deep=True)
                else:
                    _result = search_bing_lite(_q)
                    if not _result:
                        _result = search_and_summarize(_q, deep=False)
                if _result:
                    if len(_result) > 800:
                        _result = _result[:800] + "…"
                    _web_search_ctx = f"\n\n【参考资料】\n{_result}\n\n请根据参考资料用自然口语回答，把关键数字/事实说清楚。不要说'资料显示'。参考资料里的日期可能是旧的，当前日期/时间以系统注入的时间为准。"
                    print(f"[搜索/流式] 命中，注入 {len(_result)} 字")
                else:
                    _web_search_ctx = (
                        "\n\n网上没找到相关结果。用自己知道的回答，"
                        "不知道就说'不太清楚呢'，不要建议用户去查。"
                    )
                    print("[搜索/流式] 未找到相关结果")
        except Exception as _we:
            print(f"[搜索/流式] 异常: {_we}")

        try:
            # 系统提示组装（与 get_response_from_ollama 一致）
            system_prompt = self.system_prompt
            import datetime

            now = datetime.datetime.now()
            hour = now.hour
            _date_str = f"{now.year}年{now.month}月{now.day}日"
            _weekday_map = {0:"一",1:"二",2:"三",3:"四",4:"五",5:"六",6:"日"}
            _weekday = _weekday_map[now.weekday()]
            if hour < 6:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），凌晨{hour}点，用户还没睡。"
            elif hour < 11:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），上午{hour}点。"
            elif hour < 13:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），中午{hour}点。"
            elif hour < 18:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），下午{hour}点。"
            elif hour < 22:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），晚上{hour}点。"
            else:
                time_ctx = f"\n今天是{_date_str}（周{_weekday}），深夜{hour}点，用户该休息了。"
            system_prompt += time_ctx
            if self._yawn_hint:
                system_prompt += self._yawn_hint
            if getattr(self, "_tool_context", None):
                system_prompt += "\n" + self._tool_context
            # 用户画像注入：学习用户信息 + 提供上下文（仅 ags/云端模型，本地小模型上下文有限跳过）
            if self._is_agnes or self._is_cloud:
                try:
                    from user_profile import learn_from_input, get_injection_text
                    learn_from_input(_normalized_input)
                    _profile_inj = get_injection_text()
                    if _profile_inj:
                        system_prompt += "\n" + _profile_inj
                except Exception:
                    pass
            # 工具清单（慢路径）：告诉模型有哪些工具可自选调用
            try:
                from tool_registry import tool_manifest
                import dania_tools  # noqa: F401  # 触发工具注册

                _manifest = tool_manifest(limit=self.harness.max_tools)
                if _manifest:
                    system_prompt += (
                        "\n\n你还有这些工具可调用，需要时输出固定格式：\n"
                        f"{_manifest}\n"
                        '格式：TOOL: 工具名（若需参数加 ARGS: {"key":"value"}）\n'
                        '例：TOOL: set_reminder ARGS: {"delay_min":30,"text":"喝水"}'
                    )
                # 能力清单：让模型知道自己能做什么（被问"你能帮我做什么"时直接列出来，别泛泛而谈）
                _abilities = []
                try:
                    from tool_registry import list_tool_names

                    _abilities = list_tool_names()
                except Exception:
                    pass
                if _abilities:
                    _ability_desc = {
                        "query_weather": "查天气/气温/降雨",
                        "set_reminder": "设定时提醒（相对/绝对时间）",
                        "query_time": "查现在几点",
                        "open_app": "打开本机应用（网易云、浏览器等）",
                        "screen_context": "看屏幕/网页内容（截屏理解）",
                    }
                    _ability_lines = [f"- {_ability_desc.get(t, t)}" for t in _abilities]
                    system_prompt += (
                        "\n\n你的能力清单（被问'你能做什么/帮我做什么'时，直接列这些，别泛泛而谈）：\n"
                        + "\n".join(_ability_lines)
                        + "\n- 联网搜索：帮你查资料/新闻/攻略"
                    )
            except Exception as _mt:
                print(f"[工具/清单] 注入失败: {_mt}")
            if hasattr(self, "_current_image_desc") and self._current_image_desc:
                system_prompt += f"\n用户发了一张图片，内容是：{self._current_image_desc}\n根据图片描述自然回应，像真人看到图一样反应。"
            elif "[图片内容:" in user_input or "[用户发送了一张图片" in user_input:
                system_prompt += "\n" + self._image_prompt

            has_image = (
                "[图片内容:" in user_input
                or "[用户发送了一张图片" in user_input
                or (hasattr(self, "_current_image_desc") and self._current_image_desc)
            )
            recent_history = (
                self.conversation_history[-6:]
                if len(self.conversation_history) > 6
                else self.conversation_history
            )
            # A+C 记忆注入：滚动摘要 + 关键实体（恒≤120字，小模型负担极低）
            _mem_inj = self._get_memory_injection()
            if _mem_inj:
                system_prompt += f"\n{_mem_inj}"
            # 把联网搜索结果注入 user 消息（7B 模型对 user 消息注意力更高）
            _user_content = user_input + _web_search_ctx if _web_search_ctx else user_input
            messages = (
                [{"role": "system", "content": system_prompt}]
                + recent_history
                + [{"role": "user", "content": _user_content}]
            )

            # ── 云端流式（ags 专属 + 通用 OpenAI 兼容）──
            if self._is_agnes:
                return self._stream_agnes(messages, on_sentence, on_delta)
            if self._is_cloud:
                return self._stream_cloud(messages, on_sentence, on_delta)

            url = self.base_url or "http://127.0.0.1:11434/api/chat"
            # 注意：temperature/num_predict 等参数在部分推理后端会导致 500，仅保留 num_ctx 控制上下文
            data = {
                "model": self.model_name.replace("ollama-", ""),
                "messages": messages,
                "stream": True,
                "keep_alive": "2m",  # 对话结束 2 分钟后自动卸载模型，释放约 4.7GB 内存
                "options": {"num_ctx": self.harness.num_ctx},
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

            raw_full = ""  # 原始全文（含 think）
            display_full = ""  # 清洗后全文
            sent_ptr = 0  # 已切句指针
            obj = {}  # 防止所有行都解析失败时引用未定义
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
                        delta = clean_chunk[len(display_full) :]
                        display_full = clean_chunk
                        if on_delta and delta:
                            on_delta(delta)
                        # 切句：优先在句末标点切；若未到句末但已积累够长，在逗号处切短段
                        # 让 TTS 提前开合成，压缩首句出声时间
                        while True:
                            # 先找下一个句末标点
                            m = re.search(r"[。！？!?…；;\n]", display_full[sent_ptr:])
                            if m:
                                end = sent_ptr + m.end()
                                seg = display_full[sent_ptr:end].strip()
                                if seg and on_sentence:
                                    on_sentence(seg)
                                sent_ptr = end
                                continue
                            # 未到句末：若已积累 12+ 字，在最近的逗号处切短段（让 TTS 提前合成）
                            rest = display_full[sent_ptr:]
                            if len(rest) >= 12:
                                cut = rest.rfind("，", 1)
                                if cut != -1:
                                    seg = rest[: cut + 1].strip()
                                    if seg and on_sentence:
                                        on_sentence(seg)
                                    sent_ptr += cut + 1
                                    continue
                            # 无句末标点且不够短切条件 → 等更多字符
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

            # 慢路径：模型在回复里自报 TOOL: 工具名，实际执行并注入结果
            # 只取首次命中，小模型 max_steps 有限不循环重试
            _slow_ok = False
            try:
                from tool_registry import model_pick_tool, execute_tool

                _slow_info = model_pick_tool(raw_full)
                # 快路径已执行过同一工具 → 跳过，避免重复执行（模型输出被截断后仍残留 TOOL: 标记）
                if _slow_info is not None and _fast_executed and _slow_info.name == _fast_executed:
                    print(f"[工具/慢路径] 跳过重复工具: {_slow_info.name}（快路径已执行）")
                    _slow_info = None
                if _slow_info is not None:
                    print(f"[工具/慢路径] 模型选了: {_slow_info.name}")
                    if self.guard.check(_slow_info, user_input):
                        _slow_ctx = self.guard.truncate(
                            _slow_info, execute_tool(_slow_info, user_input)
                        )
                        if _slow_ctx:
                            self._tool_context = _slow_ctx
                            _slow_ok = True
                            print(f"[工具/慢路径] 已执行: {_slow_info.name}")
            except Exception as _slow_err:
                print(f"[工具/慢路径] 解析异常: {_slow_err}")

            # 慢路径成功但模型输出被截断（reply 为空或只是 TOOL 残留）→ 兜底确认语
            # 避免「工具已设好但模型答走神了」的脱节
            if _slow_ok and not reply:
                reply = "好嘞，已经帮你记上了~"

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
                if not self.guard.check(_info, user_input):
                    print(f"[护栏] 拒绝执行工具: {_info.name}")
                    self._tool_context = (
                        "（系统提示：该操作需要用户确认，请告诉用户先同意后你才能帮忙。）"
                    )
                else:
                    _ctx = execute_tool(_info, user_input)
                    _ctx = self.guard.truncate(_info, _ctx)
                    if _ctx:
                        self._tool_context = _ctx
                        print(f"[工具] 命中: {_info.name}")
        except Exception as _te:
            print(f"[工具] 路由异常: {_te}")

        # 哈欠系统：每3-5轮触发一次
        self.turn_count += 1
        if self.turn_count >= self._next_yawn_turn:
            self._yawn_hint = '\n【提示】这轮回复加一个哈欠，如"哈啊——"或"呼啊…（打哈欠）"。'
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
                    subject = (
                        subject.replace("的", "")
                        .replace("找", "")
                        .replace("有", "")
                        .replace("发给我", "")
                        .strip()
                    )
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
                                    is_group=self.is_group_chat,
                                )
                            else:
                                success, msg = self.media_helper.download_and_send_videos(
                                    user_id=self.user_id,
                                    keyword=subject,
                                    count=1,
                                    is_group=self.is_group_chat,
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
                                need_send=True,
                            )
                    else:
                        # 只提供搜索建议
                        response_text = self.media_helper.handle_media_request(
                            subject=subject,
                            media_type=media_req.get("media_type", "video"),
                            platform=media_req.get("platform", "b站"),
                            need_send=False,
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
                detected_game = self.game_context.detect_game(
                    user_input, self.conversation_history[-6:] if self.conversation_history else []
                )
                if detected_game:
                    print(f"[游戏上下文] 当前游戏：{detected_game}")
                    game_context_prompt = self.game_context.get_game_context_prompt(detected_game)
            except Exception as e:
                print(f"[游戏上下文错误] {e}")

        # 0. 理解用户搜索意图（新增）
        # 繁简统一：语音识别可能输出繁体，统一转简体再匹配
        from text_utils import to_simplified_if_traditional

        _normalized_input = to_simplified_if_traditional(user_input)
        if _normalized_input != user_input:
            print(f"[繁简] 已转简: {user_input[:20]} → {_normalized_input[:20]}")
        try:
            from query_understanding import QueryUnderstanding

            qu = QueryUnderstanding()
            intent, understood_query = qu.analyze_conversation(
                _normalized_input,
                self.conversation_history[-6:] if self.conversation_history else [],
            )
            if understood_query and understood_query != _normalized_input:
                print(f"[理解] {intent}: '{user_input}' → '{understood_query}'")
                user_input_for_search = understood_query
            else:
                user_input_for_search = _normalized_input
        except Exception as e:
            print(f"[理解错误] {e}")
            user_input_for_search = _normalized_input

        # 1. 查本地知识库（无网络延迟）
        if not extra_context:
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
            from web_search import search_and_summarize, search_bing_lite

            should_do_search = False
            search_query = user_input_for_search.strip()
            search_triggered = False  # 记录是否真的执行了搜索

            # 纯图片消息跳过搜索（用户只发图没配文字）
            is_image_only = user_input.startswith("[用户发送了一张图片") or (
                "[图片内容:" in user_input
                and len(user_input.replace("[图片内容:", "").replace("]", "").strip()) < 5
            )
            if is_image_only:
                should_do_search = False

            # 触发搜索的条件：
            # a) 用户明确要求搜索，或涉及实时生活信息
            if not should_do_search and not is_image_only:
                if any(
                    kw in _normalized_input
                    for kw in [
                        "搜索",
                        "查一下",
                        "上网查",
                        "帮我查",
                        "最新",
                        "新闻",
                        "什么时候",
                        "几号",
                        "天气",
                        "气温",
                        "温度",
                        "下雨",
                        "下雨了",
                        "刮风",
                        "天气预报",
                        "几点",
                        "现在几点",
                        "日期",
                        "今天几号",
                        "汇率",
                        "股票",
                        "金价",
                        "油价",
                    ]
                ):
                    should_do_search = True

            # b) 涉及游戏实时信息（卡池、活动、版本等），即使知识库命中也要搜索
            if not should_do_search and not is_image_only:
                if any(
                    kw in _normalized_input
                    for kw in [
                        "卡池",
                        "抽卡",
                        "池子",
                        "活动",
                        "up",
                        "限定",
                        "复刻",
                        "版本",
                        "更新",
                        "公告",
                    ]
                ):
                    should_do_search = True
                    extra_context.clear()  # 清空旧结果，以搜索为准

            # c) 兜底：知识库没命中时，只在像"查询"的消息才搜索
            #    闲聊/感叹/短回复直接跳过，不影响反应速度
            if not should_do_search and not extra_context and not is_image_only:
                # 排除：简短闲聊、感叹、肯定/否定回复
                is_chat = re.match(
                    r"^(你好|嗨|哈喽|嗯|哦|啊|好|晚安|早安|拜拜|再见|呼呼|嘿嘿|哼|唔|笑|不|是|对|好呀|好的|哦哦|嗯嗯|"
                    r"哈哈|嘿嘿|嘻嘻|呵呵|呜呜|哇|耶|嗯哼|不是|不对|不要|没问题|可以的|行|可以|"
                    r"这样|那|这|为什么|怎么|什么|怎样|如何)\b",
                    user_input,
                )
                # 只在有"查询意图"时才搜索：含问号、或长度>8且不像闲聊
                has_question_mark = "？" in user_input or "?" in user_input
                looks_like_query = has_question_mark or any(
                    kw in user_input
                    for kw in [
                        "是什么",
                        "是什么意思",
                        "是谁",
                        "在哪",
                        "怎么用",
                        "怎么做",
                        "为什么",
                        "怎么回事",
                        "多少",
                        "哪个",
                        "哪些",
                        "有没有",
                        "能不能",
                        "可以吗",
                        "行不行",
                        "是不是",
                        "对不对",
                    ]
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
                search_query = re.sub(
                    r"^(你|请|帮|帮我|告诉我|什么是|什么|怎样|怎么|如何|为什么|达妮娅|小妮)",
                    "",
                    search_query,
                )
                # 去掉常见问句词尾（提高搜索精确度）
                search_query = re.sub(
                    r"(是什么时候|什么时候|是几点|是哪个|是哪天|是哪|了吗|怎么样|好不好|了没|了嘛|呀|呢|吗|啊|吧)$",
                    "",
                    search_query,
                )
                search_query = search_query.strip("？?。.！!，,、")
                if len(search_query) < 2:
                    search_query = user_input_for_search.strip()
                search_query = search_query[:30]

                # 对游戏实时信息问题，优化搜索词（补全游戏名+最新）
                is_realtime_query = any(
                    kw in user_input
                    for kw in [
                        "卡池",
                        "抽卡",
                        "池子",
                        "活动",
                        "up",
                        "限定",
                        "复刻",
                        "更新",
                        "版本",
                        "维护",
                        "上线",
                        "公测",
                        "开服",
                        "停服",
                    ]
                )
                if is_realtime_query:
                    game_name = (
                        detected_game
                        if (
                            detected_game
                            and detected_game in ["鸣潮", "原神", "崩坏星穹铁道", "绝区零"]
                        )
                        else "鸣潮"
                    )
                    # 避免搜索词重复游戏名（如"鸣潮 鸣潮更新时间"）
                    if game_name not in search_query:
                        search_query = f"{game_name} {search_query}"
                    search_query = f"{search_query} 最新"

                print(f"[搜索] 开始: {search_query}")
                search_triggered = True
                # 追问详情才深度爬正文（慢但详细），否则摘要优先（快）
                want_deep = any(
                    kw in user_input
                    for kw in [
                        "详细",
                        "展开",
                        "具体",
                        "多点细节",
                        "详细点",
                        "详细说说",
                        "展开说说",
                        "细说",
                    ]
                )

                if want_deep:
                    search_result = search_and_summarize(search_query, deep=True)
                else:
                    # 摘要优先：requests 直抓，无浏览器，快
                    search_result = search_bing_lite(search_query)
                    if not search_result:
                        # 直抓失败（验证页/网络），降级浏览器摘要
                        print(f"[搜索] 直抓失败，降级浏览器摘要")
                        search_result = search_and_summarize(search_query, deep=False)

                if search_result:
                    extra_context.append(search_result)
                    print(f"[搜索] 命中，注入搜索结果（{len(search_result)}字）")
                    # 如果是天气搜索，更新个性系统的天气状态
                    if self.personality and any(
                        kw in search_query for kw in ["天气", "气温", "温度", "下雨", "刮风"]
                    ):
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
        search_context_for_user = ""  # 用户消息中放搜索结果

        if extra_context:
            raw = "\n".join(extra_context)
            if len(raw) > 800:
                raw = raw[:800] + "…"
            search_context_for_user = f"\n\n【参考资料】\n{raw}\n\n请根据参考资料用自然口语回答，把关键数字/事实说清楚。不要说'资料显示'。参考资料里的日期可能是旧的，当前日期/时间以系统注入的时间为准。"
            search_context_for_system = "\n用户消息有【参考资料】，必须用里面的信息回答。"
        elif search_triggered and not is_image_only:
            search_context_for_system = (
                "\n网上没找到。用自己知道的回答，不知道就说'不太清楚呢'，不要建议用户去查。"
            )

        # 4. 合并系统提示词上下文（不再包含搜索结果正文）
        combined_context = search_context_for_system  # 只有轻量提示
        # 询问达妮娅本人信息：注入傲娇推脱人设提示
        if self._is_personal_question:
            combined_context += "\n用户在问你的个人信息，傲娇地推脱，如'秘密哦''才不告诉你''哼，不告诉你''这个嘛~保密'等，不要老实回答，不要编造具体数字。"
        if game_context_prompt:
            combined_context += "\n" + game_context_prompt

        # 个性系统提示（时间感知、天气感知）
        if self.personality:
            try:
                personality_prompt = self.personality.get_personality_prompt()
                if personality_prompt:
                    combined_context += "\n" + personality_prompt
            except Exception as e:
                print(f"[个性] 获取提示失败: {e}")

        # 用户画像注入（非流式路径，仅 ags/云端，本地小模型跳过）
        if getattr(self, "_is_agnes", False) or getattr(self, "_is_cloud", False):
            try:
                from user_profile import learn_from_input, get_injection_text
                from text_utils import to_simplified_if_traditional
                _norm = to_simplified_if_traditional(user_input)
                learn_from_input(_norm)
                _profile_inj = get_injection_text()
                if _profile_inj:
                    combined_context += "\n" + _profile_inj
            except Exception:
                pass

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
        elif self._is_cloud:
            reply = self.get_response_from_cloud(effective_user_input, combined_context)
        elif self._is_ollama_model():
            reply = self.get_response_from_ollama(effective_user_input, combined_context)
        elif self.model_name.startswith("qwen"):
            reply = self.get_response_from_qwen(effective_user_input, combined_context)
        elif self._is_agnes:
            reply = self.get_response_from_agnes(effective_user_input, combined_context)
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
            elif self._is_cloud:
                retry_reply = self.get_response_from_cloud(retry_input, "")
            elif self.model_name.startswith("qwen"):
                retry_reply = self.get_response_from_qwen(retry_input, "")
            elif self._is_agnes:
                retry_reply = self.get_response_from_agnes(retry_input, "")
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

        # 7. 个性系统：话题延续
        if self.personality:
            try:
                continuation = self.personality.get_topic_continuation()
                if continuation:
                    reply = f"{reply} {continuation}"
            except Exception as e:
                print(f"[个性] 话题延续失败: {e}")

        print(
            f"[计时] 总耗时: {time.time()-_t0:.1f}s"
            + ("（含搜索+LLM）" if _t_search else "（仅LLM）")
        )
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
            "多大",
            "几岁",
            "年龄",
            "生日",
            "星座",
            "血型",
            "贵庚",
            "住哪",
            "哪里人",
            "地址",
            "家在哪",
            "住在哪里",
            "三围",
            "身高",
            "体重",
            "胸围",
            "罩杯",
            "喜欢谁",
            "男朋友",
            "女朋友",
            "对象",
            "有没有喜欢的人",
            "谈过恋爱",
            "谈过几次",
            "感情经历",
            "初恋",
            "真名",
            "本名",
            "全名",
            "真名叫",
            "电话",
            "微信",
            "qq号",
            "联系方式",
            "哪天",
            "什么时候生日",
            "喜欢吃什么",
            "你的爱好",
            "你的兴趣",
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
            r"去(查|看看|搜|找|查阅|了解一下)",
            r"你可以(去|自己|试着)",
            r"(可以|建议|最好)(关注|看看|查阅|浏览)",
            r"(看|查|关注|留意).{0,4}(公告|官网|官方)",
            r"自己(查|搜|找|看|去|了解一下)",
            r"(自行|亲自动手|你去)",
            r"(不知道|不清楚).{0,6}(你|建议|可以).{0,4}(查|看|搜|找|关注)",
            r"(查一查|看一看|了解一下|去查|去看)",
            r"得(查|看|搜|找|去)",
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
            previous = self.conversation_history[i - 1]["content"]

            # 计算相似度（使用分词而不是字符集合）
            if len(current) > 10 and len(previous) > 10:
                current_words = set(current.split())
                previous_words = set(previous.split())
                # 如果重叠度超过70%，认为太相似
                if (
                    len(current_words & previous_words)
                    / max(len(current_words), len(previous_words))
                    > 0.7
                ):
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
