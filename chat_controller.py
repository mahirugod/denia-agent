#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅终端 - 对话控制器
整合 LLM 流式对话、语音合成（分句队列播放）、语音输入（麦克风）
"""
import os
import sys
import re
import json
import time
import queue
import ctypes
import threading
import tempfile
from collections import deque

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication

# 复用 bot 模块（项目内 bot 目录）
BOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot")
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)


def _clean_display_text(text: str, for_tts: bool = False) -> str:
    """清洗显示文本：去掉表情标签（含流式输出时不完整的标签）

    for_tts=True 时额外丢弃纯标点片段（GPT-SoVITS 收到空内容会报 400）；
    显示路径必须保留标点，否则模型把句号单独作为一个 token 时会被吞掉，
    整段回复看起来就没有标点。
    """
    text = re.sub(r"\[情绪:\w+\]", "", text)
    text = re.sub(r"\[@[^\]]*\]", "", text)
    text = re.sub(r"\[[\u4e00-\u9fff][^\]]*\]", "", text)
    text = re.sub(r"\[[^\[\]]+\]", "", text)
    text = re.sub(r"【[^【】]+】", "", text)
    # 模型把工具调用当文本输出（流式可能被切碎成 OL:/TOOL:）：先整段去掉标记和 ARGS JSON，保留正文
    text = re.sub(
        r"(?is)^\s*[A-Z]{0,6}OL\s*[:：]\s*[\w-]*\s*ARGS\s*[:：]\s*\{.*?\}\s*",
        "", text,
    )
    # 标记被截断（没有完整 JSON）→ 丢掉从标记到第一个汉字之间的内容
    text = re.sub(r"(?is)^\s*[A-Z]{0,6}OL\s*[:：].*?(?=[\u4e00-\u9fff])", "", text)
    # 流式末尾可能有不完整的标签（如 [情绪:i），一并去掉
    text = re.sub(r"\[情绪:[^\]]*\Z", "", text)
    text = re.sub(r"\[@[^\]]*\Z", "", text)
    # 清理 TOOL: 工具标记（模型自报工具时不应显示给用户）
    text = re.sub(r"(?m)^TOOL[:：].*$\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\bTOOL[:：]\s*\w+(?:\s*ARGS[:：]\s*\{.*?\})?", "", text)
    text = re.sub(r"\bTOO?L?\b", "", text)
    # 纯标点/省略号/空白片段（TTS 跳过；显示时保留，标点常单独成一个 token）
    if for_tts:
        stripped = re.sub(r"[，。、！？；：…~～\s]", "", text)
        if not stripped:
            return ""
    return text.strip()


# 情绪标签 → 立绘状态（扩充词映射到现有立绘资产；Web 端另有 CSS 动画区分细腻情绪）
class ChatController(QObject):
    """对话控制器：管理 LLM、语音合成、麦克风输入"""

    # 对外信号
    stream_begin = Signal()
    stream_delta = Signal(str)
    stream_stall = Signal(str)  # 首字迟迟未到：先占位回应（不进打字机队列）
    stream_clear = Signal()  # 真实内容到达：清掉占位文字
    stream_finish = Signal(str)  # 纯完成通知；参数为错误提示（成功时为空串）
    stream_thinking = Signal(str)  # 思考链增量（reasoning_content，不进打字机，进折叠区）
    reply_ready = Signal(str)  # 完整回复文本（清洗后）
    state_changed = Signal(str)  # idle/thinking/speaking/sleeping
    log = Signal(str)
    reminder_fired = Signal(str)
    _reminder_popup = Signal(str)  # 内部：调度到 GUI 线程弹窗
    tool_event = Signal(str)  # 工具执行事件（JSON，协作流展示）
    user_message = Signal(str)  # 语音识别出的用户文本（用于回显为用户气泡）
    voice_partial = Signal(str)  # 流式识别的实时文本（空串表示这段说完/结束）
    voice_input_result = Signal(str)  # 识别结果/打断后写回输入框（空串表示被打断）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bot = None
        self._voice_synth = None
        self._voice_enabled = True

        # 主动话题调度：间隔 + 冷却 + 不打扰时段（settings.json 可覆盖）
        self._proactive_cfg = self._load_proactive_cfg()
        self._last_user_activity = time.time()
        self._last_proactive_ts = 0.0
        self._proactive_used = deque(maxlen=4)
        self._stream_active = False
        # 消息串行队列：连发多条时严格按顺序逐条处理，避免前一条回复被后一条顶掉
        self._msg_queue = queue.Queue()
        self._msg_worker_thread = None
        self._msg_worker_lock = threading.Lock()
        # 流式会话互斥锁：用户消息与主动话题不会同时播放
        self._chat_lock = threading.RLock()
        self._proactive_timer = QTimer(self)
        self._proactive_timer.setInterval(60_000)
        self._proactive_timer.timeout.connect(self._proactive_tick)
        self._proactive_timer.start()

        # 语音线程（合成/播放双线程流水线）
        self._voice_thread = None
        self._voice_synth_thread = None
        self._stream_ready_q = None
        self._voice_interrupt = threading.Event()
        self._typing_done_event = threading.Event()
        self._stream_seg_q = None
        self._stream_had_text = False

        # 气泡首字与首句语音同步：语音未出声前先缓冲文字增量
        self._delta_lock = threading.Lock()
        self._delta_buffer = []
        self._delta_released = True
        self._stream_session = 0
        self._first_audio = threading.Event()

        # VAD 打断监听（说话自动停播）——默认关闭，避免误触发
        self._vad_enabled = False
        self._vad_thread = None
        self._vad_abort = threading.Event()
        self._recent_replies = deque(maxlen=3)  # 回声防护用
        self._last_manual_send_ts = 0.0  # 手动空格录音最后发送时间戳（VAD 冷却用）

        # 麦克风
        self._mic = None
        self._mic_active = False
        self._mic_recognizing = False
        self._asr = None  # 语音识别器（懒加载）
        self._asr_cancel = threading.Event()

        # 系统级空格监听（绕过 WebEngine / Qt 事件过滤器吞键）
        self._kb_hook = None
        self._kb_pump = QTimer()
        self._kb_pump.setInterval(16)

    # ---------- 初始化 ----------
    def init_bot(
        self,
        llm_model="qwen2.5:7b",
        llm_api_key="ollama",
        llm_base_url="http://127.0.0.1:11434/api/chat",
        voice_api_url="http://127.0.0.1:9880",
        use_voice=True,
    ):
        """初始化机器人（可延迟，避免启动卡顿）"""
        self._voice_enabled = use_voice
        self._llm_base_url = llm_base_url or "http://127.0.0.1:11434/api/chat"
        self._llm_model = llm_model
        try:
            # ags / 云端模型时，若传入的 key/url 为空，回落 settings.json 里的历史 agnes_* 字段
            if llm_model.startswith("agnes") or llm_model.startswith("cloud:"):
                if not llm_api_key or not llm_base_url:
                    try:
                        _sp = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)), "settings.json"
                        )
                        if os.path.exists(_sp):
                            with open(_sp, "r", encoding="utf-8") as _f:
                                _scfg = json.load(_f)
                            if not llm_api_key and _scfg.get("cloud_api_key"):
                                llm_api_key = _scfg["cloud_api_key"]
                            if not llm_base_url and _scfg.get("cloud_base_url"):
                                llm_base_url = _scfg["cloud_base_url"]
                    except Exception:
                        pass
            self._llm_base_url = llm_base_url or self._llm_base_url
        except Exception:
            pass
        try:
            from denia_chatbot import DeniaChatbot

            self._bot = DeniaChatbot(
                use_llm=True,
                llm_model=llm_model,
                llm_api_key=llm_api_key,
                llm_base_url=llm_base_url,
                user_id=int(time.time()),
                use_voice=False,  # 终端自己控制语音播放
                voice_api_url=voice_api_url,
            )
            self.log.emit("🤖 达妮娅已就绪")
        except Exception as e:
            self.log.emit(f"❌ 机器人初始化失败: {e}")

        if use_voice:
            try:
                from voice_synthesizer import VoiceSynthesizer

                self._voice_synth = VoiceSynthesizer(api_url=voice_api_url)
                # 注意：不在初始化时判定可用性。GPT-SoVITS 服务可能在 main.py 启动后才拉起
                # （process_manager 异步启动），此时探测必失败。改为 send_message 时懒判定，
                # 保证服务上线后无需重启 main.py 就能出声。
                self.log.emit("🔊 语音合成服务已初始化（将在首次发送时探测可用性）")
            except Exception as e:
                self.log.emit(f"⚠️ 语音合成初始化失败: {e}")
                self._voice_synth = None

        # 注册提醒回调
        try:
            from dania_tools import set_reminder_callback

            set_reminder_callback(self._on_reminder)
        except Exception:
            pass
        # 提醒弹窗：后台线程 emit → Qt 自动排队到 GUI 主线程执行
        self._reminder_popup.connect(self._show_reminder_popup)

        # 注册工具执行事件回调（协作流可视化）
        try:
            from tool_registry import set_event_callback

            set_event_callback(self._on_tool_event)
        except Exception as e:
            self.log.emit(f"⚠️ 工具事件钩子注册失败: {e}")

        # 预热模型：仅当当前模型是 Ollama 本地模型时才执行（云端/ags 模型不需要预热本地推理后端）
        if self._bot is not None and getattr(self._bot, "_uses_ollama", False):
            threading.Thread(target=self._warmup_model, daemon=True).start()

        # 后台预加载语音识别模型（首次使用需 30s，提前加载避免卡顿）
        self._whisper_model = None
        threading.Thread(target=self._preload_asr, daemon=True).start()

    def _preload_asr(self):
        """后台预加载 faster-whisper 模型：镜像源失败时清掉缓存模块换官方源重试"""
        # 禁用 HuggingFace CAS 分片下载（走镜像直连，避免 xethub 401）
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        download_root = os.path.join(os.path.dirname(__file__), "models", "whisper")
        _last_err = None
        for endpoint in ("https://hf-mirror.com", "https://huggingface.co"):
            os.environ["HF_ENDPOINT"] = endpoint
            # huggingface_hub 在导入时就把 ENDPOINT 固化成常量，换源必须让模块重新加载
            for _m in [k for k in list(sys.modules)
                       if k.startswith(("huggingface_hub", "faster_whisper"))]:
                sys.modules.pop(_m, None)
            try:
                from faster_whisper import WhisperModel

                self._whisper_model = WhisperModel(
                    "small", device="cpu", compute_type="int8",
                    download_root=download_root,
                )
                self.log.emit(f"🎤 语音识别模型已就绪（{endpoint.split('//')[1]}）")
                return
            except Exception as e:
                _last_err = e
                self.log.emit(f"⚠️ 语音识别模型加载失败（{endpoint.split('//')[1]}）: {e}")
        self.log.emit(f"⚠️ 语音识别不可用：镜像与官方源均失败（{_last_err}）")

    def _warmup_model(self):
        """后台预热本地推理后端模型：发送一条最小请求让模型加载到显存/内存（仅 Ollama 后端需要）"""
        try:
            import requests

            self.log.emit("🔥 正在预热模型...")
            payload = {
                "model": self._llm_model or "qwen2.5:3b",
                "messages": [{"role": "user", "content": "你好"}],
                "stream": False,
                "keep_alive": -1,
                "options": {"num_ctx": 1024},
            }
            url = self._llm_base_url or "http://127.0.0.1:11434/api/chat"
            resp = requests.post(url, json=payload, timeout=120)
            # CUDA 失败时回退 CPU（仅 Ollama 支持 num_gpu 参数）
            if resp.status_code != 200 and "CUDA" in resp.text:
                self.log.emit("⚠️ GPU 不可用，改用 CPU 预热...")
                payload["options"] = {"num_gpu": 0}
                resp = requests.post(url, json=payload, timeout=300)
            if resp.status_code == 200:
                self.log.emit("✅ 模型预热完成")
            else:
                self.log.emit(f"⚠️ 模型预热返回 {resp.status_code}，首次对话可能较慢")
        except Exception as e:
            self.log.emit(f"⚠️ 模型预热失败（首次对话将自动加载）: {str(e)[:60]}")

    # ---------- 主动话题调度 ----------
    def _load_proactive_cfg(self) -> dict:
        cfg = {
            "enabled": True,
            "interval_min": 45.0,   # 距上一次主动搭话的最短间隔
            "cooldown_min": 20.0,   # 连续两次主动搭话的硬冷却
            "idle_min": 10.0,       # 用户至少安静这么久才搭话
            "dnd_start": "23:00",   # 不打扰时段（跨零点也支持）
            "dnd_end": "08:00",
        }
        try:
            _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
            with open(_p, encoding="utf-8") as f:
                s = json.load(f)
            for k, default in (
                ("proactive_enabled", cfg["enabled"]),
                ("proactive_interval_min", cfg["interval_min"]),
                ("proactive_cooldown_min", cfg["cooldown_min"]),
                ("proactive_idle_min", cfg["idle_min"]),
            ):
                if k in s:
                    cfg[k.replace("proactive_", "")] = s[k]
            cfg["dnd_start"] = s.get("proactive_dnd_start", cfg["dnd_start"])
            cfg["dnd_end"] = s.get("proactive_dnd_end", cfg["dnd_end"])
        except Exception:
            pass
        return cfg

    def _in_dnd(self) -> bool:
        """当前是否处于不打扰时段（支持 23:00-08:00 跨零点）"""
        try:
            def _m(t):
                h, m = str(t).split(":")[:2]
                return int(h) * 60 + int(m)
            s, e = _m(self._proactive_cfg["dnd_start"]), _m(self._proactive_cfg["dnd_end"])
            if s == e:
                return False
            now = time.localtime()
            cur = now.tm_hour * 60 + now.tm_min
            return (s <= cur < e) if s < e else (cur >= s or cur < e)
        except Exception:
            return False

    def _proactive_tick(self):
        """每分钟检查一次是否该主动搭话"""
        try:
            c = self._proactive_cfg
            if not c.get("enabled", True):
                return
            if self._stream_active:
                return
            now = time.time()
            if now - self._last_user_activity < float(c["idle_min"]) * 60:
                return
            if self._last_proactive_ts:
                gap = now - self._last_proactive_ts
                if gap < float(c["interval_min"]) * 60 or gap < float(c["cooldown_min"]) * 60:
                    return
            if self._in_dnd():
                return
            if self._voice_thread is not None and self._voice_thread.is_alive():
                return
            self._last_proactive_ts = now
            self._stream_active = True
            threading.Thread(target=self._proactive_locked, args=(None,), daemon=True).start()
        except Exception as e:
            self.log.emit(f"⚠️ 主动话题调度异常: {e}")

    def _proactive_locked(self, fallback: str):
        """主动话题与用户消息共用一把锁，保证不会同时播放两路流"""
        with self._chat_lock:
            text = ""
            try:
                llm = getattr(getattr(self, "_bot", None), "llm", None)
                if llm is not None and hasattr(llm, "generate_proactive"):
                    text = llm.generate_proactive(hint=self._proactive_hint()) or ""
            except Exception as e:
                self.log.emit(f"⚠️ 主动话题生成失败，用固定台词: {e}")
            self._proactive_worker(text or fallback or self._pick_proactive())

    def _proactive_hint(self) -> str:
        """给主动搭话的当下状态提示"""
        h = time.localtime().tm_hour
        if 5 <= h < 10:
            slot = "清晨"
        elif 10 <= h < 14:
            slot = "中午"
        elif 14 <= h < 18:
            slot = "下午"
        elif 18 <= h < 23:
            slot = "傍晚"
        else:
            slot = "深夜"
        hint = f"现在是{slot}。"
        try:
            mood = getattr(getattr(self, "_bot", None), "llm", None)
            if mood is not None and hasattr(mood, "_mood_label"):
                hint += f"你现在的心情：{mood._mood_label()}。"
        except Exception:
            pass
        return hint

    def _pick_proactive(self) -> str:
        """按时间段选一句主动话题（模型生成失败时的兜底），尽量不重复最近用过的"""
        h = time.localtime().tm_hour
        if 5 <= h < 10:
            pool = ["早呀……今天也一起慢慢来好不好？",
                    "唔……早上好，昨晚睡得好吗？",
                    "早安……我刚醒，你也刚起吗？",
                    "哈啊——早上了呢……再赖一会儿也没关系吧。",
                    "早上空气凉凉的……你那边开窗了吗？",
                    "嗯……早上想吃点甜的，蛋糕那种。",
                    "早呀……今天不用太用力，慢慢来就好。",
                    "唔……我刚把眼睛睁开，你就出现了呢。"]
        elif 10 <= h < 14:
            pool = ["到饭点了呢……今天想吃什么呀？",
                    "中午了，记得吃点东西哦。",
                    "唔……忙了一上午，歇一会儿吧？",
                    "这个点……要不要一起午睡一下？",
                    "嗯……我有点想吃草莓蛋糕了。",
                    "中午安……别边吃边看屏幕哦。",
                    "哈……上午过得怎么样，顺利吗？",
                    "唔……吃完了的话，靠一会儿吧。"]
        elif 14 <= h < 18:
            pool = ["下午的阳光有点懒呢……你在做什么呀？",
                    "这个点最容易犯困了……要不要休息一下？",
                    "下午好……我刚打了个盹，你呢？",
                    "嗯……窗外的动静有点大，风吗。",
                    "哈……下午好长，慢慢熬吧。",
                    "唔……要不要吃点甜的提提神？",
                    "下午呢……你在忙的话，我就安静待着。",
                    "呼……刚醒，脑子还有点软软的。"]
        elif 18 <= h < 23:
            pool = ["天黑了……今天过得怎么样呀？",
                    "晚上好……今天有什么想聊的吗？",
                    "晚饭吃了吗？别又随便对付哦。",
                    "嗯……灯亮起来的时候，一天就快过完了呢。",
                    "哈……晚上好，你今天辛苦了。",
                    "唔……夜里凉，记得添件衣服。",
                    "晚上呢……要不要聊点轻松的？",
                    "呼……我有点困了，不过还想再陪你一会儿。"]
        else:
            pool = ["已经很晚了……还不睡吗？",
                    "唔……我有点困了，你也早点休息吧？",
                    "夜里安静得有点过头了呢……",
                    "哈……再熬下去，明天会很难受的哦。",
                    "嗯……入睡像练习死亡一样，其实挺舒服的。",
                    "这么晚了……你在想什么呀？",
                    "呼……我把灯关掉了一半，陪你说两句。",
                    "夜深了呢……说完这句，就去睡吧。"]
        extra = "见到我很意外吗？哈哈，别紧张嘛，我说过我们会再见面的。"
        if extra not in self._proactive_used:
            pool = [extra] + pool
        for line in pool:
            if line not in self._proactive_used:
                self._proactive_used.append(line)
                return line
        line = pool[0]
        self._proactive_used.append(line)
        return line

    def _proactive_worker(self, text: str):
        """像普通回复一样显示气泡并播报，但不写入对话历史"""
        try:
            self._stream_had_text = False
            self._typing_done_event = threading.Event()
            with self._delta_lock:
                self._stream_session += 1
                _sid = self._stream_session
                self._delta_buffer = []
                self._delta_released = True
            self._prefix_buf = ""
            self._prefix_ready = False
            self._first_audio = threading.Event()
            _first_audio = self._first_audio

            seg_q = queue.Queue()
            self._stream_seg_q = seg_q
            self.stream_begin.emit()
            self.state_changed.emit("thinking")

            if self._voice_enabled and self._voice_synth is not None:
                try:
                    if self._voice_synth.is_available():
                        self._start_stream_voice()
                        with self._delta_lock:
                            if _sid == self._stream_session:
                                self._delta_released = False
                        threading.Thread(
                            target=lambda: (self._first_audio.wait(15.0), self._release_deltas(_sid)),
                            daemon=True,
                        ).start()
                except Exception:
                    pass

            self._emit_delta(text)
            for seg in re.findall(r"[^。！？!?\n]+[。！？!?]?", text):
                if seg.strip():
                    seg_q.put(seg.strip())
            seg_q.put(None)

            # 还在等首句出声时交给播放线程放行，避免文字先于语音
            with self._delta_lock:
                _waiting_audio = (_sid == self._stream_session and not self._delta_released)
            if _waiting_audio:
                # 同 _stream_worker：文字没放行前不能先发 finish，否则前端会丢增量
                def _finish_after_audio(_s=_sid, _evt=_first_audio):
                    _evt.wait(16.0)
                    self._release_deltas(_s)
                    if _s == self._stream_session:
                        self.stream_finish.emit("")

                threading.Thread(target=_finish_after_audio, daemon=True).start()
            else:
                self._release_deltas(_sid)
                self.stream_finish.emit("")
            self.reply_ready.emit(text)
            self._recent_replies.append(text)
            self.state_changed.emit("idle")
            self.log.emit(f"💭 达妮娅（主动）: {text[:60]}")
        except Exception as e:
            self.log.emit(f"⚠️ 主动话题失败: {e}")
        finally:
            self._stream_active = False

    # ---------- 发送消息 ----------
    def send_message(self, text: str):
        """发送用户消息（新开线程流式处理）"""
        # 用户一开口就重置主动话题的空闲计时
        self._last_user_activity = time.time()
        if self._bot is None:
            self.log.emit("⚠️ 机器人尚未初始化")
            return

        text = text.strip()
        if not text:
            return

        # 入队后由单一线程按顺序消费，避免并发流互相顶掉
        self._msg_queue.put(text)
        self._ensure_msg_worker()

    def _ensure_msg_worker(self):
        """确保消息消费线程存活（空闲退出，下次发消息再拉起）"""
        with self._msg_worker_lock:
            t = self._msg_worker_thread
            if t is not None and t.is_alive():
                return
            t = threading.Thread(target=self._msg_worker_loop, name="chat-queue", daemon=True)
            self._msg_worker_thread = t
            t.start()

    def _msg_worker_loop(self):
        while True:
            try:
                text = self._msg_queue.get(timeout=300)
            except queue.Empty:
                # 空闲 5 分钟退出。判空和清引用必须在同一把锁里：
                # 否则 send_message 可能刚入队就发现「线程还活着」而不拉起新线程，
                # 那条消息会一直躺在队列里等用户再发一条
                with self._msg_worker_lock:
                    if self._msg_queue.empty():
                        self._msg_worker_thread = None
                        return
                continue
            try:
                self._stream_active = True
                with self._chat_lock:
                    self._stream_worker(text)
            except Exception as e:
                self.log.emit(f"⚠️ 消息处理失败: {e}")
            finally:
                self._stream_active = False

    def _stream_worker(self, text: str):
        try:
            seg_q = queue.Queue()
            self._stream_seg_q = seg_q
            self._stream_had_text = False
            self._typing_done_event = threading.Event()

            # 新会话：重置文字缓冲与首句出声事件（默认不等待语音）
            with self._delta_lock:
                self._stream_session += 1
                _sid = self._stream_session
                self._delta_buffer = []
                self._delta_released = True
            self._first_audio = threading.Event()
            _first_audio = self._first_audio

            self.stream_begin.emit()

            # 懒判定语音可用性：服务可能晚于 main.py 起来，每次发送时再探一次
            if self._voice_enabled and self._voice_synth is not None:
                if not self._voice_synth.is_available():
                    # 服务不可用 → 仅记录一次，不永久关闭（避免服务晚起后必须重启 main.py）
                    if not getattr(self, "_voice_unavail_logged", False):
                        self.log.emit("⚠️ 语音服务暂不可用，本次不发声")
                        self._voice_unavail_logged = True
                else:
                    self._voice_unavail_logged = False
                    self._start_stream_voice()
                    # 语音可用：先扣住文字增量，等首句真正出声再一次性放行
                    with self._delta_lock:
                        if _sid == self._stream_session:
                            self._delta_released = False

                    def _watch_sync(_s=_sid, _evt=_first_audio):
                        # 语音迟迟没出声（合成失败/服务卡住）→ 超时后照常显示文字
                        # 15s：GPT-SoVITS 首句合成实测可达 8s，太短会让文字抢跑
                        _evt.wait(15.0)
                        self._release_deltas(_s)

                    threading.Thread(target=_watch_sync, daemon=True).start()

            self.log.emit(f"⏳ 思考中 ({text[:20]}...)")

            reply = ""
            stream_error = ""
            # 首字迟迟未到 → 先在气泡里回一句，避免用户以为卡死
            self._stall_spoke = False
            self._stall_cleared = False
            self._prefix_buf = ""
            self._prefix_ready = False
            _stop_stall = threading.Event()

            def _watch_stall():
                if _stop_stall.wait(1.5):
                    return
                if self._stream_had_text:
                    return
                self._stall_spoke = True
                self.stream_stall.emit("嗯…让我想想")

            threading.Thread(target=_watch_stall, daemon=True).start()
            try:
                for attempt in range(2):  # 最多重试 1 次（应对模型未加载的 500 错误）
                    try:
                        reply = self._bot.get_response_stream(
                            text,
                            on_sentence=lambda seg: seg_q.put(seg),
                            on_delta=self._emit_delta,
                        )
                        break  # 成功则跳出重试
                    except Exception as e:
                        err_msg = str(e)
                        self.log.emit(f"❌ 流式失败 (尝试 {attempt+1}/2): {err_msg[:80]}")
                        # 500 通常是模型加载中，网络抖动（连接中断/远端断连）也可重试，等 3 秒再试
                        if attempt == 0 and (
                            "500" in err_msg
                            or "Internal Server Error" in err_msg
                            or "Connection aborted" in err_msg
                            or "RemoteDisconnected" in err_msg
                            or "Connection reset" in err_msg
                        ):
                            self.log.emit("⏳ 连接异常，3 秒后重试...")
                            time.sleep(3)
                            # 重试前重发 stream_begin，让前端清零旧气泡/打字机队列，避免残字重复
                            self.stream_begin.emit()
                            # 上一轮失败尝试缓冲的文字一并丢弃，避免重试后重复
                            with self._delta_lock:
                                if _sid == self._stream_session:
                                    self._delta_buffer = []
                            continue
                        # 判断是否连接失败（服务未启动）：用 base_url 提取端口做通用判断
                        host_port = ""
                        try:
                            from urllib.parse import urlparse as _urlparse
                            _u = _urlparse(self._llm_base_url or "http://127.0.0.1:11434")
                            host_port = f"{_u.hostname or ''}:{_u.port or ''}"
                        except Exception:
                            pass
                        if (
                            host_port and host_port in err_msg
                        ) or "refused" in err_msg or "Max retries" in err_msg:
                            stream_error = "⚠️ 云端服务没连上，检查下网络和设置页的 API Key"
                        else:
                            stream_error = f"⚠️ 回复失败: {err_msg[:40]}"
                        reply = ""
                        break
            finally:
                _stop_stall.set()
                # 流结束不等于语音已出声：还在等首句时继续扣住文字，
                # 由播放线程出声时放行（否则整段文字会先于语音弹出）。
                with self._delta_lock:
                    _waiting_audio = (_sid == self._stream_session and not self._delta_released)
                if _waiting_audio:
                    # 文字还扣在缓冲里就不能先发 stream_finish：
                    # 前端收到 finish 会把空气泡定格并摘掉流式标记，
                    # 之后放行的增量找不到气泡，整段文字会被丢掉（只剩语音）。
                    def _finish_after_audio(_s=_sid, _err=stream_error, _evt=_first_audio):
                        _evt.wait(16.0)
                        self._release_deltas(_s)
                        if _s == self._stream_session:
                            self.stream_finish.emit(_err)

                    threading.Thread(target=_finish_after_audio, daemon=True).start()
                else:
                    self._release_deltas(_sid)
                    self.stream_finish.emit(stream_error)
                seg_q.put(None)  # 语音队列哨兵
                # 思考链：把本轮累积的 reasoning 推到前端折叠区
                _thinking = ""
                if self._bot and hasattr(self._bot, "llm") and self._bot.llm:
                    _thinking = getattr(self._bot.llm, "last_thinking", "")
                if _thinking:
                    self.stream_thinking.emit(_thinking)

            if reply:
                clean = _clean_display_text(reply)
                self.reply_ready.emit(clean)
                self._recent_replies.append(clean)  # 回声防护样本
                self.state_changed.emit("idle")
                self.log.emit(f"✨ 达妮娅: {clean[:60]}")
            else:
                _hint = "⚠️ 回复为空"
                if self._llm_model.startswith("agnes") or self._llm_model.startswith("cloud:"):
                    _hint += "（ags/云端流式返回0字，请查看上方[ags]日志中的原始SSE行）"
                self.log.emit(_hint)
                self.state_changed.emit("idle")
        except Exception as e:
            self.log.emit(f"❌ 出错: {e}")
        finally:
            self._stream_active = False

    def _emit_delta(self, d: str):
        # 开头可能是被流式切碎的 TOOL:...ARGS:{...} 标记：先缓存原始增量，
        # 出现首个汉字/换行后再整体清洗放行（避免半截标记被逐段清洗后留下 JSON 残渣）
        if not getattr(self, "_prefix_ready", True):
            self._prefix_buf += d
            lead = self._prefix_buf.lstrip()
            if not lead:
                return
            if ord(lead[0]) > 127 or "\n" in lead or len(self._prefix_buf) >= 160:
                raw, self._prefix_buf = self._prefix_buf, ""
                self._prefix_ready = True
                clean_d = _clean_display_text(raw)
                if not clean_d:
                    return
            else:
                return
        else:
            # 打字机增量也要清洗，避免 [情绪:xxx] 在打字过程中闪现
            clean_d = _clean_display_text(d)
            if not clean_d:
                return
        with self._delta_lock:
            if not self._delta_released:
                # 首句语音还没出声：先存着，出声那一刻再放出来
                self._delta_buffer.append(clean_d)
                return
            self._emit_delta_now(clean_d)

    def _emit_delta_now(self, clean_d: str):
        self._stream_had_text = True
        # 真实内容到达：先清掉"嗯…让我想想"占位，再正常逐字追加
        if getattr(self, "_stall_spoke", False) and not getattr(self, "_stall_cleared", False):
            self._stall_cleared = True
            self.stream_clear.emit()
        self.stream_delta.emit(clean_d)

    def _release_deltas(self, sid: int):
        """放行缓冲的文字增量；sid 校验防止上一轮播放线程误放新会话的内容。"""
        with self._delta_lock:
            if sid != self._stream_session or self._delta_released:
                return
            self._delta_released = True
            buf, self._delta_buffer = self._delta_buffer, []
            for clean_d in buf:
                self._emit_delta_now(clean_d)

    # ---------- 语音合成（流式分句，合成/播放流水线） ----------
    def _start_stream_voice(self):
        # 每次会话独立线程组：队列/事件按会话闭包传递，旧线程卡死也不影响新一轮
        interrupt = threading.Event()
        self._voice_interrupt = interrupt
        self._typing_done_event = threading.Event()
        session_done = threading.Event()
        ready_q = queue.Queue(maxsize=3)
        self._stream_ready_q = ready_q
        _sid = self._stream_session
        self._voice_synth_thread = threading.Thread(
            target=self._voice_synth_worker, args=(ready_q, interrupt), daemon=True
        )
        self._voice_thread = threading.Thread(
            target=self._voice_stream_player,
            args=(ready_q, interrupt, self._typing_done_event, session_done, _sid),
            daemon=True,
        )
        self._voice_synth_thread.start()
        self._voice_thread.start()
        # VAD 打断监听：说话自动停播
        self._start_vad_monitor(interrupt, session_done)

    def _start_vad_monitor(self, interrupt, session_done):
        if not getattr(self, "_vad_enabled", True) or self._mic_active:
            return
        t = getattr(self, "_vad_thread", None)
        if t is not None and t.is_alive():
            return
        self._vad_thread = threading.Thread(
            target=self._vad_worker, args=(interrupt, session_done), daemon=True
        )
        self._vad_thread.start()

    def _vad_worker(self, interrupt, session_done):
        """VAD 监视线程：用户开口自动停播，捕获语音识别后作为新输入提交"""
        asr = None
        try:
            from vad_monitor import VadMonitor

            # 优先流式识别：麦克风帧边收边解码，句尾 finalize 立即出结果
            try:
                from streaming_asr import StreamingASR, available as _stream_asr_ok

                if _stream_asr_ok():
                    asr = StreamingASR(on_partial=lambda t: self.voice_partial.emit(t))
            except Exception as e:
                self.log.emit(f"⚠️ 流式识别不可用，回落整句识别: {str(e)[:50]}")

            mon = VadMonitor(
                should_stop=lambda: interrupt.is_set() or session_done.is_set(),
                hard_stop=lambda: self._vad_abort.is_set(),
                on_frame=(asr.accept if asr is not None else None),
            )
            data = mon.run()
        except Exception as e:
            self.voice_partial.emit("")
            self.log.emit(f"⚠️ VAD 监听不可用: {str(e)[:60]}")
            return
        if not data or session_done.is_set():
            self.voice_partial.emit("")
            return
        try:
            self._stop_stream_voice()  # 打断播放（player 退出后会话标记 done）
            text = asr.finalize() if asr is not None else self._asr_from_pcm(data)
            self.voice_partial.emit("")
            if (not text or len(text) < 2) and asr is not None:
                # 流式漏识别（噪声/口音）→ 回落到整句 Whisper 再试一次
                text = self._asr_from_pcm(data)
            if not text or len(text) < 2:
                self.log.emit("🗣️ 听到说话但没识别出内容")
                self.state_changed.emit("idle")
                return
            # 手动空格录音发送后 5 秒内，VAD 不应重复处理同一段话
            if time.time() - self._last_manual_send_ts < 5.0:
                self.log.emit("🔇 VAD 冷却期内，忽略（手动录音刚发送）")
                self.state_changed.emit("idle")
                return
            if self._looks_like_echo(text):
                self.log.emit("🔇 疑似扬声器回声，已忽略")
                self.state_changed.emit("idle")
                return
            self.log.emit(f"🗣️ 打断捕获: {text}")
            self._last_manual_send_ts = time.time()  # VAD 发送也标记，防后续重复
            self.user_message.emit(text)
            self.send_message(text)
        except Exception as e:
            self.log.emit(f"⚠️ VAD 处理失败: {e}")
            self.state_changed.emit("idle")

    def _asr_from_pcm(self, pcm: bytes) -> str:
        """VAD 捕获的 PCM → wav → whisper 识别"""
        try:
            import wave

            wav_path = os.path.join(tempfile.gettempdir(), "dania_vad_capture.wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
            return self._run_asr(wav_path)
        except Exception as e:
            self.log.emit(f"⚠️ 捕获音频处理失败: {e}")
            return ""

    def _looks_like_echo(self, text: str) -> bool:
        """识别文本与最近 assistant 回复高度相似 → 判定为扬声器回声"""
        import difflib

        t = re.sub(r"[，。！？、,.!?\s]", "", text or "")
        if not t:
            return False
        for prev in self._recent_replies:
            p = re.sub(r"[，。！？、,.!?\s]", "", prev or "")
            if p and difflib.SequenceMatcher(None, t, p).ratio() > 0.72:
                return True
        return False

    def _stop_stream_voice(self):
        evt = getattr(self, "_voice_interrupt", None)
        if evt is not None:
            evt.set()
        old_q = getattr(self, "_stream_seg_q", None)
        if old_q is not None:
            try:
                old_q.put(None)
            except Exception:
                pass
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        for attr in ("_voice_synth_thread", "_voice_thread"):
            t = getattr(self, attr, None)
            if t is not None and t.is_alive():
                t.join(timeout=2)

    def _voice_synth_worker(self, ready_q, interrupt):
        """合成线程：取分句 → 后台预合成 → 就绪队列（与播放并行，消除句间空窗）"""
        synth = self._voice_synth
        seg_q = self._stream_seg_q
        if synth is None or seg_q is None:
            return
        idx = 0
        try:
            while True:
                if interrupt.is_set():
                    break
                try:
                    seg = seg_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if seg is None:
                    break
                clean_seg = _clean_display_text(seg, for_tts=True)
                if not clean_seg.strip():
                    continue
                wav_path = os.path.join(
                    tempfile.gettempdir(),
                    f"dania_voice_{int(time.time() * 1000)}_{idx}.wav",
                )
                idx += 1
                ok = synth.synthesize(clean_seg, wav_path)
                if interrupt.is_set():
                    # 已打断：丢弃刚合成的音频
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                    break
                if not ok:
                    wav_path = None  # 合成失败标记，播放端跳过
                # 带打断检查的阻塞入队（队列满 = 播放端落后，自然背压）
                while not interrupt.is_set():
                    try:
                        ready_q.put((clean_seg, wav_path), timeout=0.3)
                        break
                    except queue.Full:
                        continue
        finally:
            try:
                ready_q.put(None, timeout=2)
            except Exception:
                pass

    def _voice_stream_player(self, ready_q, interrupt, typing_done, session_done=None, sid=None):
        """播放线程：只播就绪音频，被打断时立即退出"""
        import winsound

        heard_first = False
        interrupted = False
        t0 = time.time()
        try:
            if ready_q is None:
                return
            while True:
                try:
                    item = ready_q.get(timeout=0.5)
                except queue.Empty:
                    if interrupt.is_set():
                        interrupted = True
                        break
                    continue
                if item is None:
                    break
                if interrupt.is_set():
                    self._discard_wav(item)
                    interrupted = True
                    break
                seg, wav_path = item
                if wav_path and os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                    if not heard_first:
                        heard_first = True
                        self.log.emit(f"🔊 首句出声 {time.time() - t0:.1f}s")
                        # 气泡首字与首句语音同步：出声瞬间再放行文字增量
                        if sid is None or sid == self._stream_session:
                            self._first_audio.set()
                        self._release_deltas(sid)
                        self.state_changed.emit("speaking")
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                    if interrupt.is_set():
                        interrupted = True
                        break
                else:
                    self.log.emit("⚠️ 一句合成失败，跳过")
        except Exception as e:
            self.log.emit(f"❌ 语音出错: {e}")
        finally:
            # 无论正常结束还是合成失败，都要放行被扣住的文字，避免气泡一直空着
            if sid is None or sid == self._stream_session:
                self._first_audio.set()
            self._release_deltas(sid)
            # 清理未播放的预合成音频
            while ready_q is not None:
                try:
                    item = ready_q.get_nowait()
                except queue.Empty:
                    break
                self._discard_wav(item)
            # 等打字机完成
            evt = typing_done
            if evt is not None and not evt.is_set() and not interrupted:
                evt.wait(timeout=30)
            if not interrupted:
                self.state_changed.emit("idle")
            # 会话结束标记：让 VAD 监视线程退出
            if session_done is not None:
                session_done.set()

    @staticmethod
    def _discard_wav(item):
        """丢弃队列中未播放的音频文件"""
        if isinstance(item, tuple) and len(item) == 2 and item[1]:
            try:
                if os.path.exists(item[1]):
                    os.remove(item[1])
            except OSError:
                pass

    def interrupt_voice(self):
        """打断语音播放"""
        self._stop_stream_voice()

    # ---------- 打字机完成通知 ----------
    def on_typing_finished(self):
        """UI 打字机完成 → 唤醒语音线程"""
        evt = getattr(self, "_typing_done_event", None)
        if evt is not None:
            evt.set()

    # ---------- 麦克风语音输入 ----------
    def install_system_space_listener(self):
        """
        安装系统级空格监听：用 GetAsyncKeyState 轮询空格键状态，
        彻底绕过 Qt 事件过滤器被 Web 引擎拦截的问题，且无需消息泵线程。
        """
        if self._kb_hook is not None:
            return
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32

            def _tick():
                try:
                    key_state = user32.GetAsyncKeyState(0x20)  # VK_SPACE = 0x20
                    is_down = bool(key_state & 0x8000)
                    if is_down and not self._kb_space_was_down:
                        self._kb_space_was_down = True
                        self._on_system_space(is_up=False)
                    elif not is_down and self._kb_space_was_down:
                        self._kb_space_was_down = False
                        self._on_system_space(is_up=True)
                except Exception:
                    pass

            self._kb_tick = _tick
            self._kb_pump.timeout.connect(self._kb_tick)
            self._kb_pump.start()
            self._kb_space_was_down = False
            self._kb_hook = True
            self.log.emit("🎤 系统级空格监听已启用（轮询模式）")
        except Exception as e:
            self.log.emit(f"⚠️ 系统级空格监听安装异常: {e}")

    def uninstall_system_space_listener(self):
        """卸载系统级空格监听"""
        if not self._kb_hook:
            return
        self._kb_pump.stop()
        self._kb_pump.timeout.disconnect()
        self._kb_hook = None
        self._kb_tick = None
        self._kb_space_was_down = False

    def _poll_system_key_events(self):
        """保留接口，轮询模式下由 _kb_pump 直接驱动，无需队列"""
        pass

    def _on_system_space(self, is_up: bool):
        """空格按下 / 松开 → 驱动录音状态机（GUI 线程）"""
        if is_up:
            self.stop_mic()
        else:
            if getattr(self, "_mic_recognizing", False):
                self.cancel_mic()
            else:
                self.start_mic()

    def _focus_in_web_console(self) -> bool:
        """
        判断当前键盘焦点是否真正在 Web 输入框内。
        QWebEngineView 内部的 DOM 焦点无法同步查询，
        所以退而求其次：若焦点 widget 是 QWebEngineView 本身且窗口有 bridge（即 Web 控制台），
        则返回 True（说明焦点落在 Web 区，空格可能被 Web 引擎用来输入）。
        非 Web 区（原生窗口、桌面、其他应用）不拦截，空格可正常触发录音。
        """
        fw = QApplication.focusWidget()
        if fw is None:
            return False
        # 焦点在 QWebEngineView 实例上 → 用户在 Web 区，拦截空格
        from PySide6.QtWebEngineWidgets import QWebEngineView

        if isinstance(fw, QWebEngineView):
            # 确认是控制台窗口（有 bridge），而不是其他 WebEngineView
            top = fw
            while top:
                if hasattr(top, "bridge"):
                    return True
                top = top.parentWidget() if top.parentWidget() else None
            return True
        return False

    def _set_mic_recognizing(self, flag: bool):
        self._mic_recognizing = flag

    def toggle_mic(self):
        """切换麦克风录音"""
        if self._mic_active:
            self._stop_mic()
        else:
            self._start_mic()

    def start_mic(self):
        """空格按下：开始录音"""
        self._asr_cancel.clear()
        self._mic_recognizing = False
        if not self._mic_active:
            self._start_mic()

    def stop_mic(self):
        """空格松开：停止录音并进入识别"""
        if self._mic_active:
            self._mic_recognizing = True
            self._stop_mic()

    def cancel_mic(self):
        """识别中按空格：打断录音/识别，丢弃结果"""
        self._mic_active = False
        self._mic_recognizing = False
        self._asr_cancel.set()
        if getattr(self, "_mic", None):
            try:
                self._mic.stop()
            except Exception:
                pass
        self._mic = None
        self._audio_buffer = bytearray()
        self.voice_input_result.emit("")
        self.log.emit("⏹ 语音输入已取消")

    def _start_mic(self):
        try:
            # mic_stream 已随项目整合到根目录
            from mic_stream import MicStream

            self._mic = MicStream(sample_rate=16000, block_ms=100, on_block=self._on_audio_block)
            self._audio_buffer = bytearray()
            self._mic.start()
            self._mic_active = True
            self.log.emit("🎤 麦克风已开启，说话中…")
        except Exception as e:
            self.log.emit(f"❌ 麦克风启动失败: {e}")

    def _stop_mic(self):
        if self._mic:
            try:
                self._mic.stop()
            except Exception:
                pass
            self._mic = None
        self._mic_active = False
        self.log.emit("🎤 麦克风已关闭，识别中…")
        # 识别缓冲音频
        self._recognize_audio()

    def _on_audio_block(self, data: bytes):
        if hasattr(self, "_audio_buffer"):
            self._audio_buffer.extend(data)

    def _is_valid_voice_text(self, text: str) -> bool:
        """过滤太短、空白或明显无效的用户语音文本，避免误触发对话"""
        t = re.sub(r"\s+", "", text or "")
        if len(t) < 4:
            return False
        return bool(t)

    def _recognize_audio(self):
        """将录制的 PCM 保存为 WAV 并识别（结果写回输入框，不直接发送）"""
        buf = getattr(self, "_audio_buffer", None)
        if not buf:
            self._mic_recognizing = False
            self.voice_input_result.emit("")
            return
        try:
            import wave

            wav_path = os.path.join(tempfile.gettempdir(), "dania_mic_input.wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(bytes(buf))

            # 识别放到后台线程，避免阻塞 GUI；完成/异常后通过信号写回输入框
            threading.Thread(target=self._run_asr_thread, args=(wav_path,), daemon=True).start()
        except Exception as e:
            self.log.emit(f"❌ 语音识别失败: {e}")
            self._mic_recognizing = False
            self.voice_input_result.emit("")

    def _is_valid_voice_text(self, text: str) -> bool:
        """过滤太短、纯符号或无效识别结果，避免误触发对话"""
        t = re.sub(r"[\s，。！？,.!?、~～…—-]", "", text or "")
        # 至少 3 个有效汉字/字母
        return len(t) >= 3

    def _run_asr_thread(self, wav_path: str):
        """后台执行 ASR，结果通过 voice_input_result 信号写回 UI"""
        # 在启动前检查取消标志，减少无效识别开销
        if self._asr_cancel.is_set():
            self._mic_recognizing = False
            return
        try:
            text = self._run_asr(wav_path)
        finally:
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except OSError:
                pass

        # 识别完成后再次检查取消标志，若已取消则丢弃结果
        if self._asr_cancel.is_set():
            self._mic_recognizing = False
            return
        self._mic_recognizing = False
        if text:
            if not self._is_valid_voice_text(text):
                self.log.emit("⚠️ 识别内容过短或无效，已忽略")
                self.voice_input_result.emit("")
                return
            # 简单的回声/重复防护：如果和最近一次回复高度相似，视为回声，不发送
            if self._looks_like_echo(text):
                self.log.emit("⚠️ 疑似回声，已忽略")
                self.voice_input_result.emit("")
                return
            self.log.emit(f"🎤 识别: {text}")
            self._last_manual_send_ts = time.time()  # 标记手动发送，VAD 冷却
            self.user_message.emit(text)  # 控制台气泡显示
            self.voice_input_result.emit(text)
            self.send_message(text)  # 直接发送给 LLM
        else:
            self.log.emit("⚠️ 未识别到语音内容")
            self.voice_input_result.emit("")

    def _run_asr(self, wav_path: str) -> str:
        """语音识别：使用 faster-whisper（CPU int8）"""
        try:
            os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from faster_whisper import WhisperModel

            if not hasattr(self, "_whisper_model") or self._whisper_model is None:
                self._whisper_model = WhisperModel(
                    "small", device="cpu", compute_type="int8",
                    download_root=os.path.join(os.path.dirname(__file__), "models", "whisper"),
                )
            segments, _ = self._whisper_model.transcribe(
                wav_path,
                language="zh",
                beam_size=1,
                vad_filter=True,
            )
            text = "".join(s.text for s in segments).strip()
            # 繁简统一：whisper 中文输出可能偏繁体，统一转简体再返回
            from text_utils import to_simplified_if_traditional
            _norm = to_simplified_if_traditional(text)
            if _norm and _norm != text:
                self.log.emit(f"[繁简] 识别已转简: {text[:20]} → {_norm[:20]}")
            return _norm
        except Exception as e:
            self.log.emit(f"⚠️ 语音识别异常: {e}")
            return ""

    # ---------- 提醒 ----------
    def _on_reminder(self, content: str):
        """提醒到点回调（后台线程）→ 转发信号，弹窗走 GUI 线程"""
        self.reminder_fired.emit(content)
        self._reminder_popup.emit(content)

    def _show_reminder_popup(self, content: str):
        """在 GUI 主线程弹出提醒窗口（Qt 信号槽自动跨线程排队）"""
        try:
            from reminder_popup import ReminderPopup

            popup = ReminderPopup(content)
            popup.show_centered()
            popup._ref = popup  # 防止被 GC 回收
        except Exception as e:
            print(f"[提醒] 弹窗异常: {e}")

    def get_active_reminders(self) -> list:
        """待触发的提醒（供 UI 首页卡片/任务页展示），按触发时间排序"""
        try:
            from bot.dania_tools import get_active_reminders

            return get_active_reminders()
        except Exception:
            return []

    # ---------- 工具协作事件 ----------
    def _on_tool_event(self, event: dict):
        """工具执行事件（后台线程）→ 信号转发给 UI（Qt 自动跨线程排队）"""
        try:
            self.tool_event.emit(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass

    def replay_voice(self, text: str):
        """重播指定文本的语音（对话气泡喇叭按钮）"""
        text = (text or "").strip()
        if not text:
            return
        if self._voice_synth is None:
            self.log.emit("⚠️ 语音服务不可用")
            return

        def _run():
            wav_path = os.path.join(tempfile.gettempdir(), "dania_replay_voice.wav")
            try:
                import winsound

                self._stop_stream_voice()
                clean = _clean_display_text(text, for_tts=True)
                if self._voice_synth.synthesize(clean, wav_path):
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                else:
                    self.log.emit("⚠️ 语音合成失败")
            except Exception as e:
                self.log.emit(f"❌ 语音重播失败: {e}")
            finally:
                try:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except OSError:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    # ---------- 清理 ----------
    def cleanup(self):
        self._vad_abort.set()
        self._stop_stream_voice()
        self._stop_mic()

    def reinit_bot(self, init_fn):
        """热切换模型：重置 bot 后调用 init_fn 重新初始化"""
        try:
            if self._bot:
                self._bot = None
            init_fn()
            self.log.emit("✅ 模型切换完成")
        except Exception as e:
            self.log.emit(f"❌ 模型切换失败: {e}")
