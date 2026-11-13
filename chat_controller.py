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
    text = re.sub(r"\[情绪[：:][^\]]*\Z", "", text)
    text = re.sub(r"\[@[^\]]*\Z", "", text)
    # 清理 TOOL: 工具标记（模型自报工具时不应显示给用户）
    text = re.sub(r"(?m)^TOOL[:：].*$\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"(?<![A-Za-z])TOOL[:：]\s*\w*(?:\s*ARGS[:：]\s*\{.*?\})?", "", text)
    text = re.sub(r"\bTOO?L?\b", "", text)
    # 工具参数残片（TOOL:/ARGS: 标记被流式截断后行首只剩 query":"梁文峰是谁"}）
    _arg_key = (
        r"query|keyword|keywords|url|path|name|slug|count|city|text|q|"
        r"page|limit|group_id|user_id|code|command|args|arguments")
    _arg_pair = (r'["\']?(?:%s)["\']?\s*[:：]\s*(?:"[^"\n]*"|[^,，\s}\n]{0,60})'
                 % _arg_key)
    text = re.sub(
        r'(?is)^\s*[{\[]?\s*' + _arg_pair +
        r'(?:\s*[,，]\s*' + _arg_pair + r')*\s*[}\]]?[，,。;；、]?\s*',
        "", text,
    )
    # 纯标点/省略号/空白片段（TTS 跳过；显示时保留，标点常单独成一个 token）
    if for_tts:
        stripped = re.sub(r"[，。、！？；：…~～\s]", "", text)
        if not stripped:
            return ""
    return text.strip()


# 内心独白：模型用（…）写没说出口的话，只显示、不朗读
_INNER_OS_RE = re.compile(r"[（(][^（）()\n]{1,30}[）)]")


def _strip_inner_os(text: str) -> str:
    """语音合成前去掉括号里的内心独白；显示路径保留原文。"""
    return _INNER_OS_RE.sub("", text or "").strip()


# 环境感知：只取前台进程名做粗分类，不读窗口标题，避免把隐私内容带进对话
_AMBIENT_APPS = (
    (("chrome", "msedge", "firefox", "brave", "opera", "360se", "qqbrowser"), "逛网页"),
    (("code", "pycharm", "idea", "devenv", "sublime", "webstorm", "goland", "trae"), "写代码"),
    (("wechat", "weixin", "qq", "discord", "telegram", "lark", "feishu", "dingtalk"), "聊天"),
    (("steam", "epicgames", "wegame", "genshin", "yuanshen", "starrail", "wuthering"), "玩游戏"),
    (("potplayer", "vlc", "mpc-hc", "bilibili", "iqiyi", "youku"), "看视频"),
    (("windowsterminal", "powershell", "cmd", "wt"), "用终端"),
    (("excel", "winword", "powerpnt", "wps"), "写文档"),
    (("explorer",), "翻文件"),
)


def _foreground_app_name() -> str:
    """前台进程名（不含 .exe，小写）；取不到返回空串。"""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(user32.GetForegroundWindow())
        if not hwnd:
            return ""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.c_ulong(512)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
        finally:
            kernel32.CloseHandle(handle)
        return os.path.splitext(os.path.basename(buf.value))[0].lower()
    except Exception:
        return ""


def _ambient_category(name: str) -> str:
    """进程名 → 粗类别；认不出来就不报（宁可不说话，也不猜）。"""
    name = (name or "").lower()
    if not name or name in ("python", "pythonw"):
        return ""
    for keys, label in _AMBIENT_APPS:
        if any(k == name or k in name for k in keys):
            return label
    return ""


# 情绪标签 → 立绘状态（扩充词映射到现有立绘资产；Web 端另有 CSS 动画区分细腻情绪）
class ChatController(QObject):
    """对话控制器：管理 LLM、语音合成、麦克风输入"""

    # 对外信号
    stream_begin = Signal()
    stream_delta = Signal(str)
    stream_stall = Signal(str)  # 首字迟迟未到：先占位回应（不进打字机队列）
    stream_clear = Signal()  # 真实内容到达：清掉占位文字
    voice_rhythm = Signal(float)  # 每字毫秒（按当前句音频时长/字数算出），前端打字机跟着语速走
    voice_segment = Signal(str, float)  # 本段显示文本 + 音频时长ms：前端按段内时钟揭示文字
    voice_stream_end = Signal()         # 本轮语音全部播完（含被打断/合成失败）
    voice_replay_state = Signal(bool)  # 气泡语音重播：True=开始播放，False=结束/被打断
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
        self._proactive_miss = 0      # 连续几次主动搭话没被回应
        self._proactive_day = ""      # 每日预算按自然日重置
        self._proactive_today = 0
        self._proactive_mark = 0.0    # 上次重置时的用户活动时间
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
        # 流式（增量）播放：按上一段实测语速估算本段时长，驱动前端逐字揭示
        self._tts_ms_per_char = 220.0
        self._active_stream = None   # sounddevice 输出流（打断时立即 abort）
        self._stream_voice_flag = None  # 是否支持流式合成/增量播放（首次会话探测后缓存）

        # 气泡首字与首句语音同步：语音未出声前先缓冲文字增量
        self._delta_lock = threading.Lock()
        self._delta_buffer = []
        self._delta_released = True
        self._stream_session = 0
        self._first_audio = threading.Event()

        # 播出文本语义：本轮真正写进声卡的文字（被打断时按它截断历史）
        self._voice_spoken_sid = 0
        self._voice_spoken_text = ""
        self._voice_incomplete = False  # 播放线程是否提前退出（打断/设备故障）
        self._turn_reply = ""           # 本轮 LLM 完整回复（截断时用于定位历史条目）
        self._turn_reply_sid = 0
        self._turn_emotion = ""         # 本轮 [情绪:xxx]，用于 TTS 语速微调

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
            "daily_max": 5,         # 每天主动开口的上限（含没人理的）
            "rhythm": True,         # 用观测到的作息兜住固定 DND 窗口
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
            # 单独的开合开关（不在上面按前缀批量映射，免得键名对不上）
            cfg["backoff"] = bool(s.get("proactive_backoff_enabled", True))
            cfg["hooks"] = bool(s.get("proactive_hooks_enabled", True))
            try:
                cfg["daily_max"] = max(1, int(s.get("proactive_daily_max", cfg["daily_max"])))
            except (TypeError, ValueError):
                pass
            cfg["rhythm"] = bool(s.get("proactive_rhythm_enabled", True))
        except Exception:
            pass
        return cfg

    def _relationship_scale(self) -> float:
        """关系越熟，主动搭话间隔越短（1.0 → 0.6）；读不到就按 1.0。"""
        try:
            import relationship
            return float(relationship.interval_scale())
        except Exception:
            return 1.0

    def _energy_scale(self) -> float:
        """她越累，主动搭话的间隔越长（1.0 → 1.5）。"""
        try:
            return float(self._bot.llm.energy_scale())
        except Exception:
            return 1.0

    def _ambient_enabled(self) -> bool:
        """环境感知开关（settings.json 的 ambient_awareness，默认关闭）。"""
        try:
            sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
            with open(sp, encoding="utf-8") as f:
                return bool(json.load(f).get("ambient_awareness", False))
        except Exception:
            return False

    def _ambient_sample(self) -> str:
        """采样前台应用类别，返回"连续用了多久"的提示；不满足条件返回空串。

        只记录类别与起始时间，不记录窗口标题/文件名，也不落盘。
        """
        if not self._ambient_enabled():
            self._ambient = {}
            return ""
        cat = _ambient_category(_foreground_app_name())
        now = time.time()
        a = getattr(self, "_ambient", None) or {}
        if not cat:
            self._ambient = {}
            return ""
        if a.get("cat") != cat:
            a = {"cat": cat, "since": now}
            self._ambient = a
        mins = int((now - float(a.get("since") or now)) / 60)
        if mins < 25:
            return ""
        return (f"用户已经在前台{cat}连续约 {mins} 分钟了。"
                f"可以自然提一句关心（眼睛/休息之类），别像监控，也别报具体分钟数。")

    def _due_loop(self):
        """到期且还没追问过的未闭环话题（"明天面试"这类）；没有就返回 None。

        只有关系熟到"熟悉"阶段才追问：刚认识就问面试/生日会显得越界。
        不达标时话题保留在库里，等关系涨上来再问。
        """
        try:
            import relationship
            if not relationship.followup_ok():
                return None
        except Exception:
            pass
        try:
            import memory_store
            loops = memory_store.due_open_loops()
            return loops[0] if loops else None
        except Exception:
            return None

    def _in_dnd(self) -> bool:
        """当前是否处于不打扰时段：固定窗口 + 观测到的作息窗口。

        固定 23:00-08:00 是拍脑袋的，作息不同的人会一直被吵醒或被错过。
        这里再用"用户平时几点有动静"兜一层，样本不够时不生效（返回 False，
        和以前行为一致）。
        """
        try:
            def _m(t):
                h, m = str(t).split(":")[:2]
                return int(h) * 60 + int(m)
            s, e = _m(self._proactive_cfg["dnd_start"]), _m(self._proactive_cfg["dnd_end"])
            now = time.localtime()
            cur = now.tm_hour * 60 + now.tm_min
            if s != e and ((s <= cur < e) if s < e else (cur >= s or cur < e)):
                return True
            if self._proactive_cfg.get("rhythm", True):
                import memory_store
                wake, bed = memory_store.rhythm_window()
                if wake >= 0:
                    h = now.tm_hour
                    awake = (wake <= h < bed) if wake <= bed else (h >= wake or h < bed)
                    if not awake:
                        return True
        except Exception:
            return False
        return False

    def _proactive_tick(self):
        """每分钟检查一次是否该主动搭话"""
        try:
            c = self._proactive_cfg
            if not c.get("enabled", True):
                return
            # 每日预算：按自然日重置。没人理的也算一次，免得冷场时越试越勤
            _today = time.strftime("%Y-%m-%d")
            if self._proactive_day != _today:
                self._proactive_day = _today
                self._proactive_today = 0
            if self._proactive_today >= int(c.get("daily_max", 5) or 5):
                return
            if self._stream_active:
                return
            now = time.time()
            self._ambient_sample()  # 维护"连续使用同一类应用"的起点
            if now - self._last_user_activity < float(c["idle_min"]) * 60:
                return
            if self._last_user_activity > float(getattr(self, "_proactive_mark", 0.0)):
                self._proactive_mark = self._last_user_activity
                self._proactive_miss = 0
            _miss = int(getattr(self, "_proactive_miss", 0))
            # 到点的未闭环话题优先，且不受常规间隔限制（仍尊重硬冷却）
            loop = self._due_loop()
            if self._last_proactive_ts:
                gap = now - self._last_proactive_ts
                if gap < float(c["cooldown_min"]) * 60:
                    return
                if not loop:
                    # 连续几次没人理，就逐步拉长间隔，别越冷场越起劲（可用开关关掉）
                    _factor = (2 ** min(_miss, 3)) if c.get("backoff", True) else 1
                    # 关系越熟间隔越短：熟悉之后更愿意主动找你说话
                    need = (float(c["interval_min"]) * 60 * _factor
                            * self._relationship_scale() * self._energy_scale())
                    if gap < need:
                        return
            if self._in_dnd():
                return
            if self._voice_thread is not None and self._voice_thread.is_alive():
                return
            # 心情很差时不主动搭话。必须在消耗自思考话头/调 API 之前拦住，
            # 否则想法被吃掉、还白花一次生成（软下限，用户问的事照样会答）
            try:
                _m = float(getattr(
                    getattr(getattr(self, "_bot", None), "llm", None), "_mood", 0.0) or 0.0)
                if _m <= -6:
                    return
            except Exception:
                pass
            loop_hint = ""
            if loop:
                if loop.get("kind") == "confirm":
                    # 事实变更待确认：不是催进度，只是确认现在以哪个为准
                    loop_hint = (f"用户之前说「{loop.get('old_value', '')}」，"
                                 f"后来又说「{loop.get('new_value', '')}」。"
                                 f"找个自然的时机确认一下现在以哪个为准，一句话，别像查岗。")
                else:
                    loop_hint = (f"用户之前提过「{loop.get('text', '')}」，今天到日子了。"
                                 f"先自然地问一句进展，别像查岗。")
                try:
                    import memory_store
                    memory_store.mark_loop_asked(loop.get("id", ""))
                except Exception:
                    pass
            # 上一次主动搭话没人理：她会失落一点、精力也掉（只影响语气，不阻止这次开口）
            if _miss >= 1:
                try:
                    _llm = getattr(getattr(self, "_bot", None), "llm", None)
                    if _llm is not None and hasattr(_llm, "note_unanswered"):
                        _llm.note_unanswered(_miss)
                except Exception:
                    pass
            self._last_proactive_ts = now
            self._proactive_today += 1
            self._proactive_miss = _miss + 1
            self._stream_active = True
            threading.Thread(target=self._proactive_locked,
                             args=(None, loop_hint), daemon=True).start()
        except Exception as e:
            self.log.emit(f"⚠️ 主动话题调度异常: {e}")

    def _proactive_mode(self) -> str:
        """主动开口的档位：'brief' 只应一声 / 'full' 正常开个话题。

        用户在写代码写文档，或者她精力见底时，完整开新话题很打扰；
        给一个短反应更接近"在场"，而不是"抢话"。
        """
        try:
            llm = getattr(getattr(self, "_bot", None), "llm", None)
            if llm is not None and float(getattr(llm, "_energy", 70.0)) < 25.0:
                return "brief"
        except Exception:
            pass
        a = getattr(self, "_ambient", None) or {}
        if a.get("cat") in ("写代码", "写文档"):
            if time.time() - float(a.get("since") or 0) >= 25 * 60:
                return "brief"
        return "full"

    @staticmethod
    def _trim_brief(text: str) -> str:
        """短反应档：只留一句、15 字以内；空内容返回空串交给调用方兜底。"""
        t = (text or "").strip()
        if not t:
            return ""
        t = re.split(r"[。！？!?…\n]", t)[0].strip()
        if len(t) > 15:
            # 优先在逗号处断开，保留一个完整的小句，而不是硬切半句
            head = re.split(r"[，,、；;]", t)[0].strip()
            t = head if 0 < len(head) <= 15 else t[:15].rstrip("，、, ")
        if t and t[-1] not in "。！？!?…~～":
            t += "。"
        return t

    def _proactive_locked(self, fallback: str, extra_hint: str = ""):
        """主动话题与用户消息共用一把锁，保证不会同时播放两路流"""
        with self._chat_lock:
            mode = self._proactive_mode()
            text = ""
            try:
                llm = getattr(getattr(self, "_bot", None), "llm", None)
                if llm is not None and hasattr(llm, "generate_proactive"):
                    hist = list(getattr(llm, "conversation_history", []) or [])[-6:]
                    hint = self._proactive_hint(brief=(mode == "brief"))
                    if extra_hint:
                        hint += "\n" + extra_hint
                    if mode == "brief":
                        hint += "\n这次只应一声：一句话，15 字以内，别开新话题、别提问。"
                    text = llm.generate_proactive(hint=hint, history=hist) or ""
            except Exception as e:
                self.log.emit(f"⚠️ 主动话题生成失败，用固定台词: {e}")
            self._proactive_worker(text or fallback or self._pick_proactive(),
                                   brief=(mode == "brief"))

    def _proactive_hint(self, brief: bool = False) -> str:
        """给主动搭话的当下状态提示；brief 档不谈想法/话头，免得又开一个话题"""
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
        llm = getattr(getattr(self, "_bot", None), "llm", None)
        try:
            if llm is not None and hasattr(llm, "_mood_label"):
                hint += f"你现在的心情：{llm._mood_label()}。"
            # 空闲时自己冒出来的想法优先：从这里开口，比固定台词更像"她自己在想事"。
            # brief 档不要想法/话头：那会把"应一声"又变成开新话题。
            if not brief:
                thought = ""
                if llm is not None and hasattr(llm, "take_proactive_thought"):
                    thought = llm.take_proactive_thought()
                if thought:
                    hint += f"\n你刚才闲着的时候一直在想：{thought}。就用这个念头自然开个口，别把它念出来。"
                if (self._proactive_cfg.get("hooks", True)
                        and llm is not None and hasattr(llm, "take_proactive_hook")):
                    hook = llm.take_proactive_hook()
                    if hook:
                        hint += f"\n可接的话头（用户之前说过这句）：{hook}"
            _amb = self._ambient_sample()
            if _amb:
                hint += "\n" + _amb
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

    def _proactive_worker(self, text: str, brief: bool = False):
        """像普通回复一样显示气泡并播报；这句话要记进历史，用户接下来的短答才有参照"""
        # 硬闸门：用户说过不想聊的话题，主动开口直接作废（不重生成，免得绕一圈又踩雷）
        try:
            import memory_store as _ms
            _banned = _ms.match_directive(text)
            if _banned:
                self.log.emit(f"🚫 主动话题命中禁提「{_banned}」，本轮不开口")
                return
        except Exception:
            pass
        if brief:
            text = self._trim_brief(text) or "嗯。"
        try:
            _m = float(getattr(getattr(getattr(self, "_bot", None), "llm", None), "_mood", 0.0) or 0.0)
            if _m <= -6:
                return  # 心情很差时不主动搭话；用户问的事照样会答（软下限）
        except Exception:
            pass
        try:
            _llm = getattr(getattr(self, "_bot", None), "llm", None)
            if _llm is not None and hasattr(_llm, "update_history") and text:
                _llm.update_history("assistant", text)
        except Exception:
            pass
        try:
            self._stream_had_text = False
            self._typing_done_event = threading.Event()
            with self._delta_lock:
                self._stream_session += 1
                _sid = self._stream_session
                self._delta_buffer = []
                self._delta_released = True
            # 主动话题也按播出文本记历史：被打断时同样截断到真正播出的部分
            self._turn_reply = text
            self._turn_reply_sid = _sid
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
        # 顺带记一次"这个小时醒着"，用于估计作息（只记小时，不记内容）
        try:
            import memory_store
            memory_store.note_active_hour()
        except Exception:
            pass
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
                            # 被打断时按"真正播出"的部分写历史，而不是整段回复
                            spoken_provider=lambda _s=_sid: self._spoken_snapshot(_s),
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

            # 记录本轮完整回复 + 所属会话：播放线程打断时据此定位历史条目
            self._turn_reply = reply or ""
            self._turn_reply_sid = _sid

            if reply:
                _emo = re.search(r"\[情绪[:：]([\w\u4e00-\u9fff]+)\]", reply)
                if _emo:
                    self._turn_emotion = _emo.group(1)
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
        # 情绪标签只在原始增量里：记下本轮情绪，合成时用来微调语速（显示层照旧剥掉）
        _emo = re.search(r"\[情绪[:：]([\w\u4e00-\u9fff]+)\]", d)
        if _emo:
            self._turn_emotion = _emo.group(1)
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
        if self._stream_voice_capable():
            # 增量播放：队列里是 PCM 分片（约 5 秒缓冲），合成线程只负责收流，
            # 播放线程按段写声卡；两线程解耦后下句请求能在上句播放时就开始
            ready_q = queue.Queue(maxsize=80)
            synth_target = self._voice_synth_worker_stream
            player_target = self._voice_stream_player_stream
        else:
            ready_q = queue.Queue(maxsize=3)
            synth_target = self._voice_synth_worker
            player_target = self._voice_stream_player
        self._stream_ready_q = ready_q
        _sid = self._stream_session
        # 新一轮语音：清空上一轮的播出文本快照
        self._voice_spoken_sid = _sid
        self._voice_spoken_text = ""
        self._voice_incomplete = False
        self._turn_emotion = ""
        self._voice_synth_thread = threading.Thread(
            target=synth_target, args=(ready_q, interrupt), daemon=True
        )
        self._voice_thread = threading.Thread(
            target=player_target,
            args=(ready_q, interrupt, self._typing_done_event, session_done, _sid),
            daemon=True,
        )
        self._voice_synth_thread.start()
        self._voice_thread.start()
        # VAD 打断监听：说话自动停播
        self._start_vad_monitor(interrupt, session_done)

    def _stream_voice_capable(self) -> bool:
        """探测增量播放能力：sounddevice/numpy 可用且合成器支持流式。

        首次探测后缓存；不可用时整段合成 + winsound 播放（旧链路）兜底。
        """
        if self._stream_voice_flag is not None:
            return self._stream_voice_flag
        ok = False
        try:
            import sounddevice  # noqa: F401
            import numpy  # noqa: F401

            ok = self._voice_synth is not None and hasattr(
                self._voice_synth, "synthesize_stream")
        except Exception as e:
            self.log.emit(f"⚠️ 增量播放不可用，回落整段播放: {str(e)[:60]}")
        self._stream_voice_flag = bool(ok)
        if self._stream_voice_flag:
            self.log.emit("🔊 语音增量播放已启用")
        return self._stream_voice_flag

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
        _partial = {"text": ""}
        try:
            from vad_monitor import VadMonitor

            # 优先流式识别：麦克风帧边收边解码，句尾 finalize 立即出结果
            try:
                from streaming_asr import StreamingASR, available as _stream_asr_ok

                if _stream_asr_ok():
                    def _on_partial(t):
                        _partial["text"] = t or ""
                        self.voice_partial.emit(t)

                    asr = StreamingASR(on_partial=_on_partial)
            except Exception as e:
                self.log.emit(f"⚠️ 流式识别不可用，回落整句识别: {str(e)[:50]}")

            mon = VadMonitor(
                should_stop=lambda: interrupt.is_set() or session_done.is_set(),
                hard_stop=lambda: self._vad_abort.is_set(),
                on_frame=(asr.accept if asr is not None else None),
                # 静音到点后再判断一次说没说完，避免把"因为…"截成半句
                should_continue=lambda: self._utterance_unfinished(_partial["text"]),
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
            if self._is_filler(text):
                self.log.emit(f"🔇 只是应声「{text}」，不当作打断")
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

    # 句尾是这些词，说明话还没说完——再多等一个静音窗口（相当于轻量轮次判断，
    # 不需要额外模型：流式识别的高频局部结果已经够了）
    _UNFINISHED_TAIL = ("然后", "还有", "而且", "因为", "所以", "但是", "就是", "那个",
                        "这个", "的话", "跟", "和", "、", "，", ",")
    # 纯应声，不当作"打断"
    _FILLER_ONLY = ("嗯", "嗯嗯", "啊", "哦", "哦哦", "呃", "唔", "诶", "哎", "唉",
                    "那个", "这个", "好的", "对", "是")

    def _utterance_unfinished(self, text: str) -> bool:
        """流式识别的局部文本是否明显没说完（VAD 句尾二次确认用）。"""
        t = (text or "").strip()
        if not t:
            return False
        if t[-1] in "。！？!?…":
            return False
        return any(t.endswith(x) for x in self._UNFINISHED_TAIL)

    def _is_filler(self, text: str) -> bool:
        """只哼了一声/应了一句，不该打断正在播放的语音。"""
        t = re.sub(r"[\s，。！？、,.!?~～…]", "", text or "")
        return bool(t) and len(t) <= 3 and t in self._FILLER_ONLY

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
        # 增量播放：立即中止声卡输出流，避免打断后还在吐缓冲帧
        st = getattr(self, "_active_stream", None)
        if st is not None:
            try:
                st.abort()
            except Exception:
                pass
        for attr in ("_voice_synth_thread", "_voice_thread"):
            t = getattr(self, attr, None)
            if t is not None and t.is_alive():
                t.join(timeout=2)

    @staticmethod
    def _prefix_by_chars(text: str, n: int) -> str:
        """取前 n 个有效字（不含标点/空白）的原始前缀，用于截断未播完的句子。"""
        if n <= 0 or not text:
            return ""
        count = 0
        for i, ch in enumerate(text):
            if re.match(r"[\s，。、！？；：…~～,.\-!?;:()（）“”\"']", ch):
                continue
            count += 1
            if count >= n:
                return text[:i + 1]
        return text

    def _spoken_snapshot(self, sid):
        """本轮已播出文字：None=未被打断（按全文记历史）；空串=一句都没播出。"""
        if getattr(self, "_voice_spoken_sid", 0) != sid:
            return None
        if not getattr(self, "_voice_incomplete", False):
            return None
        return getattr(self, "_voice_spoken_text", "")

    def _on_voice_turn_finished(self, sid, spoken, incomplete):
        """语音轮次结束：被打断时把历史里那条完整回复截断成真正播出的部分。"""
        if not incomplete:
            return
        if getattr(self, "_turn_reply_sid", 0) != sid:
            return  # LLM 还没写完这轮（历史由 spoken_provider 在写盘时截断）
        reply = getattr(self, "_turn_reply", "") or ""
        if len(reply.strip()) < 2:
            return
        llm = getattr(getattr(self, "_bot", None), "llm", None)
        if llm is None or not hasattr(llm, "truncate_last_assistant_reply"):
            return
        llm.truncate_last_assistant_reply(reply, spoken)

    @staticmethod
    def _split_first_tts_segment(text: str, min_head: int = 6):
        """首句在第一个逗号/顿号处切一刀，先合成前半句尽快出声。

        - 只对首句调用；head+tail 拼起来仍是原文，前端按段累计不会丢字
        - 逗号前有效字不足 min_head（如"嗯，"）就换下一个逗号，避免只合成语气词
        """
        pos = 0
        while True:
            m = re.search(r"[，、]", text[pos:])
            if m is None:
                return None
            end = pos + m.end()
            head, tail = text[:end], text[end:]
            if (len(re.sub(r"[\s，、。！？；：…]", "", head)) >= min_head
                    and len(re.sub(r"\s", "", tail)) >= 2):
                return head, tail
            pos = end

    def _voice_synth_worker(self, ready_q, interrupt):
        """合成线程：取分句 → 后台预合成 → 就绪队列（与播放并行，消除句间空窗）"""
        synth = self._voice_synth
        seg_q = self._stream_seg_q
        if synth is None or seg_q is None:
            return
        idx = 0
        _pending_pieces = []  # 首句切分后的剩余片段：前半句先出声，后半句边播边合成
        _first_seg = True
        try:
            while True:
                if interrupt.is_set():
                    break
                if _pending_pieces:
                    clean_seg = _pending_pieces.pop(0)
                else:
                    try:
                        seg = seg_q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if seg is None:
                        break
                    clean_seg = _clean_display_text(seg, for_tts=True)
                    if not clean_seg.strip():
                        continue
                    if _first_seg:
                        _first_seg = False
                        _parts = self._split_first_tts_segment(clean_seg)
                        if _parts:
                            _pending_pieces = list(_parts)
                            clean_seg = _pending_pieces.pop(0)
                # 内心独白只显示不朗读：整段都是独白时没有音频，仍把文字推给前端
                _speech = _strip_inner_os(clean_seg)
                if not _speech:
                    while not interrupt.is_set():
                        try:
                            ready_q.put((clean_seg, None), timeout=0.3)
                            break
                        except queue.Full:
                            continue
                    continue
                wav_path = os.path.join(
                    tempfile.gettempdir(),
                    f"dania_voice_{int(time.time() * 1000)}_{idx}.wav",
                )
                idx += 1
                ok = synth.synthesize(_speech, wav_path,
                                      emotion=getattr(self, "_turn_emotion", ""))
                # 质量兜底：时长/字数明显偏离正常语速（吞字或拖沓）就换随机种子重合成一次
                if ok:
                    try:
                        import wave as _wq
                        with _wq.open(wav_path, "rb") as _f:
                            _dur = _f.getnframes() / float(_f.getframerate() or 1)
                        _n = max(1, len(re.sub(r"[\s，。！？、…—\"“”]", "", _speech)))
                        _per = _dur / _n
                        if _per < 0.08 or _per > 0.45:
                            self.log.emit(f"⚠️ 合成异常({_per:.2f}s/字)，重试一次")
                            ok = synth.synthesize(
                                _speech, wav_path,
                                emotion=getattr(self, "_turn_emotion", ""))
                    except Exception:
                        pass
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

    def _voice_synth_worker_stream(self, ready_q, interrupt):
        """流式合成线程：逐句收流，把 PCM 分片入队（不在这里播放）。

        收到头块就入队 "seg"，之后每收一块入队 "pcm"；
        播放线程消费的阻塞会形成背压，同时下句请求可以在上句播放期间发起。
        """
        synth = self._voice_synth
        seg_q = self._stream_seg_q
        if synth is None or seg_q is None:
            return
        idx = 0
        _pending_pieces = []  # 首句切分后的剩余片段
        _first_seg = True

        def _put(item):
            while not interrupt.is_set():
                try:
                    ready_q.put(item, timeout=0.3)
                    return True
                except queue.Full:
                    continue
            return False

        try:
            while True:
                if interrupt.is_set():
                    break
                if _pending_pieces:
                    clean_seg = _pending_pieces.pop(0)
                else:
                    try:
                        seg = seg_q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if seg is None:
                        break
                    clean_seg = _clean_display_text(seg, for_tts=True)
                    if not clean_seg.strip():
                        continue
                    if _first_seg:
                        _first_seg = False
                        _parts = self._split_first_tts_segment(clean_seg)
                        if _parts:
                            _pending_pieces = list(_parts)
                            clean_seg = _pending_pieces.pop(0)
                # 内心独白只显示不朗读：整段都是独白就直接推纯文本给播放端
                _speech = _strip_inner_os(clean_seg)
                if not _speech:
                    _put(("os", clean_seg))
                    continue

                wav_path = os.path.join(
                    tempfile.gettempdir(),
                    f"dania_voice_{int(time.time() * 1000)}_{idx}.wav",
                )
                _is_first = (idx == 0)  # 本轮第一段：允许引擎用 cut5 抢先出声
                idx += 1

                def _on_start(sr, ch, width, _seg=clean_seg, _path=wav_path):
                    _put(("seg", _seg, _path, sr, ch, width))

                def _on_frames(pcm):
                    _put(("pcm", pcm))

                # 不做整段质量重试：流式已经播出，重合成会造成重复/割裂
                info = synth.synthesize_stream(
                    _speech, wav_path,
                    on_start=_on_start,
                    on_frames=_on_frames,
                    should_stop=interrupt.is_set,
                    first=_is_first,
                    emotion=getattr(self, "_turn_emotion", ""),
                )
                if not info:
                    self.log.emit("⚠️ 一句合成失败，跳过")
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                frames = info[3] if info else 0
                _put(("eos", frames, clean_seg))
                if interrupt.is_set():
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                    break
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
        spoken_parts = []  # 真正播完的段文本（被打断时据此截断历史）
        saw_end = False    # 是否消费到队列哨兵（正常播完）
        cur_seg = ""       # 当前正在播的段文本
        cur_dur = 0.0      # 当前段的音频时长（秒）
        cur_t0 = 0.0       # 当前段开始播放的时刻
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
                    saw_end = True
                    break
                if interrupt.is_set():
                    self._discard_wav(item)
                    interrupted = True
                    break
                seg, wav_path = item
                if wav_path and os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                    # 打字机与语速同步：这句音频时长 ÷ 字数 = 每字毫秒，推给前端
                    _seg_ms = 0.0  # 读时长失败时按 0 处理：前端整段直接显示，不用错值
                    try:
                        import wave as _wave
                        with _wave.open(wav_path, "rb") as _w:
                            _dur = _w.getnframes() / float(_w.getframerate() or 1)
                        _seg_ms = max(0.0, _dur * 1000.0)
                        _chars = len(re.sub(r"[\s，。！？、,.!?…；;:：\"“”]", "", seg or ""))
                        if _dur > 0 and _chars >= 2:
                            self.voice_rhythm.emit(
                                max(40.0, min(400.0, _dur * 1000.0 / _chars)))
                    except Exception:
                        pass
                    cur_seg = seg or ""
                    cur_dur = _seg_ms / 1000.0
                    # 段内时钟：在音频真正开播前把「本段文本 + 时长」推给前端，
                    # 前端按这段的时长逐字揭示，段边界强制对齐，避免跨句累积漂移
                    if sid is None or sid == self._stream_session:
                        try:
                            self.voice_segment.emit(seg, _seg_ms)
                        except Exception:
                            pass
                    if not heard_first:
                        heard_first = True
                        self.log.emit(f"🔊 首句出声 {time.time() - t0:.1f}s")
                        # 气泡首字与首句语音同步：出声瞬间再放行文字增量
                        if sid is None or sid == self._stream_session:
                            self._first_audio.set()
                        self._release_deltas(sid)
                        self.state_changed.emit("speaking")
                    cur_t0 = time.time()
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                    if interrupt.is_set():
                        # winsound 被 SND_PURGE 中止：按已播时长占比估算播出字数
                        _elapsed = max(0.0, time.time() - cur_t0)
                        if cur_dur > 0 and cur_seg:
                            _n = len(re.sub(r"[\s，。！？、,.!?…；;:：\"“”]", "", cur_seg))
                            _frac = min(1.0, _elapsed / cur_dur)
                            _part = self._prefix_by_chars(cur_seg, int(_n * _frac))
                            if _part:
                                spoken_parts.append(_part)
                        interrupted = True
                        break
                    if cur_seg:
                        spoken_parts.append(cur_seg)
                    cur_seg = ""
                else:
                    # 没有音频（纯内心独白）：文字照常推给前端，0ms 立即显示
                    if seg and (sid is None or sid == self._stream_session):
                        try:
                            self.voice_segment.emit(seg, 0.0)
                        except Exception:
                            pass
                    else:
                        self.log.emit("⚠️ 一句合成失败，跳过")
        except Exception as e:
            self.log.emit(f"❌ 语音出错: {e}")
        finally:
            # 播出文本快照：整段播完的 + 当前段已播出的部分
            if ready_q is not None:
                _sid_eff = sid if sid is not None else self._stream_session
                self._voice_spoken_sid = _sid_eff
                self._voice_spoken_text = "".join(spoken_parts)
                self._voice_incomplete = not saw_end
                self._on_voice_turn_finished(
                    _sid_eff, self._voice_spoken_text, not saw_end)
            # 无论正常结束还是合成失败，都要放行被扣住的文字，避免气泡一直空着
            if sid is None or sid == self._stream_session:
                self._first_audio.set()
            self._release_deltas(sid)
            # 通知前端本轮语音结束：把没读到的尾巴（表情/代码等）补齐并定稿
            if sid is None or sid == self._stream_session:
                self.voice_stream_end.emit()
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

    @staticmethod
    def _discard_stream_item(item):
        """丢弃队列里未消费的分片：流式 seg 删临时 wav，其它条目忽略/走旧逻辑"""
        if not isinstance(item, tuple) or not item:
            return
        if item[0] == "seg":
            try:
                if len(item) >= 3 and item[2] and os.path.exists(item[2]):
                    os.remove(item[2])
            except OSError:
                pass
            return
        if item[0] in ("pcm", "eos", "os"):
            return
        ChatController._discard_wav(item)

    @staticmethod
    def _stream_write_pcm(stream, pcm, channels, interrupt):
        """把一块裸 PCM 拆成小片写声卡：write 阻塞即自然限速，片间检查打断。"""
        import numpy as np

        if not pcm or channels <= 0:
            return 0
        frame_bytes = 2 * channels  # 只接受 16bit
        usable = len(pcm) - (len(pcm) % frame_bytes)
        step = 4096 * frame_bytes  # 约 128ms@32k，保证打断响应足够快
        written = 0
        for i in range(0, usable, step):
            if interrupt.is_set():
                break
            arr = np.frombuffer(pcm[i:i + step], dtype=np.int16)
            if channels > 1:
                arr = arr.reshape(-1, channels)
            stream.write(arr)
            written += int(arr.shape[0])
        return written

    def _voice_stream_player_stream(self, ready_q, interrupt, typing_done,
                                    session_done=None, sid=None):
        """播放线程（增量）：按段把 PCM 分片实时写声卡；被打断立即停。

        流式下拿不到整段时长，voice_segment 的时长用上一段实测语速估算；
        段边界会强制对齐，漂移不会跨句累积。
        """
        import numpy as np  # noqa: F401
        import sounddevice as sd

        heard_first = False
        interrupted = False
        t0 = time.time()
        stream = None
        sr = ch = 0
        cur = None  # (seg_text, wav_path, sr, ch)
        seg_frames = 0
        announced = False
        spoken_parts = []  # 真正写完声卡的段文本（被打断时据此截断历史）
        saw_end = False    # 是否消费到队列哨兵（正常播完）

        def _chars(text):
            return len(re.sub(r"[\s，。！？、,.!?…；;:：\"“”]", "", text or ""))

        def _close_stream():
            nonlocal stream, sr, ch
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if getattr(self, "_active_stream", None) is stream:
                self._active_stream = None
            stream = None
            sr = ch = 0

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
                    saw_end = True
                    break
                if interrupt.is_set():
                    self._discard_stream_item(item)
                    interrupted = True
                    break
                kind = item[0] if isinstance(item, tuple) and item else ""
                if kind == "os":
                    # 纯内心独白段：没有音频，0ms 推给前端立刻整段显示
                    _os_text = item[1] if len(item) > 1 else ""
                    if _os_text and (sid is None or sid == self._stream_session):
                        try:
                            self.voice_segment.emit(_os_text, 0.0)
                        except Exception:
                            pass
                    continue
                if kind == "seg":
                    _, seg_text, wav_path, _sr, _ch, _sw = item
                    cur = (seg_text, wav_path, _sr, _ch)
                    seg_frames = 0
                    announced = False
                    continue
                if kind == "pcm":
                    if cur is None:
                        continue
                    seg_text, wav_path, _sr, _ch = cur
                    if stream is None or _sr != sr or _ch != ch:
                        _close_stream()
                        stream = sd.OutputStream(
                            samplerate=_sr, channels=_ch, dtype="int16",
                            blocksize=1024,
                        )
                        stream.start()
                        sr, ch = _sr, _ch
                        self._active_stream = stream
                    if not announced:
                        announced = True
                        est_ms = max(600.0, _chars(seg_text) * self._tts_ms_per_char)
                        if sid is None or sid == self._stream_session:
                            try:
                                self.voice_segment.emit(seg_text, est_ms)
                            except Exception:
                                pass
                        if not heard_first:
                            heard_first = True
                            self.log.emit(f"🔊 首句出声 {time.time() - t0:.1f}s")
                            if sid is None or sid == self._stream_session:
                                self._first_audio.set()
                            self._release_deltas(sid)
                            self.state_changed.emit("speaking")
                    seg_frames += self._stream_write_pcm(stream, item[1], ch, interrupt)
                    continue
                if kind == "eos":
                    _, frames, seg_text = item
                    frames = seg_frames or frames or 0
                    # 该段所有 PCM 已写完声卡 → 计入"已播出文本"
                    if seg_frames > 0 and seg_text:
                        spoken_parts.append(seg_text)
                    if sr > 0 and frames > 0 and seg_text:
                        _n = _chars(seg_text)
                        if _n >= 1:
                            per = frames * 1000.0 / (sr * _n)
                            self._tts_ms_per_char = max(40.0, min(400.0, per))
                            if sid is None or sid == self._stream_session:
                                try:
                                    self.voice_rhythm.emit(self._tts_ms_per_char)
                                except Exception:
                                    pass
                    if cur is not None:
                        try:
                            os.remove(cur[1])
                        except OSError:
                            pass
                    cur = None
                    seg_frames = 0
                    announced = False
                    continue
                # 不该出现：流式播放只消费 seg/pcm/eos
                self._discard_stream_item(item)
        except Exception as e:
            # 打断时 abort 会让 write 抛异常，不用当成故障刷日志
            interrupted = interrupt.is_set()
            if not interrupted:
                self.log.emit(f"❌ 语音出错: {e}")
        finally:
            # 播放端退出（含设备故障）时通知合成端停止，避免队列写满后线程卡死在 put
            interrupt.set()
            _close_stream()
            # 播出文本快照：整段播完的 + 当前段已写出的部分（按帧比例估算字数）
            if ready_q is not None:
                if cur is not None and announced and seg_frames > 0:
                    _seg = cur[0] or ""
                    _n = _chars(_seg)
                    # 注意：上面 _close_stream() 已把 sr 清零，这里用该段自身的采样率
                    _sr = cur[2] or sr
                    if _n >= 2 and _sr > 0:
                        _est_ms = max(600.0, _n * self._tts_ms_per_char)
                        _frac = min(1.0, seg_frames / (_est_ms / 1000.0 * _sr))
                        _part = self._prefix_by_chars(_seg, int(_n * _frac))
                        if _part:
                            spoken_parts.append(_part)
                _sid_eff = sid if sid is not None else self._stream_session
                self._voice_spoken_sid = _sid_eff
                self._voice_spoken_text = "".join(spoken_parts)
                self._voice_incomplete = not saw_end
                self._on_voice_turn_finished(
                    _sid_eff, self._voice_spoken_text, not saw_end)
            # 无论正常结束还是失败，都要放行被扣住的文字，避免气泡一直空着
            if sid is None or sid == self._stream_session:
                self._first_audio.set()
            self._release_deltas(sid)
            if sid is None or sid == self._stream_session:
                self.voice_stream_end.emit()
            # 清理未消费的分片与残留临时文件
            while ready_q is not None:
                try:
                    item = ready_q.get_nowait()
                except queue.Empty:
                    break
                self._discard_stream_item(item)
            if cur is not None:
                try:
                    os.remove(cur[1])
                except OSError:
                    pass
            evt = typing_done
            if evt is not None and not evt.is_set() and not interrupted:
                evt.wait(timeout=30)
            if not interrupted:
                self.state_changed.emit("idle")
            if session_done is not None:
                session_done.set()

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
            self.voice_replay_state.emit(False)
            return

        def _run():
            wav_path = os.path.join(tempfile.gettempdir(), "dania_replay_voice.wav")
            try:
                import winsound

                self._stop_stream_voice()
                clean = _strip_inner_os(_clean_display_text(text, for_tts=True))
                if not clean:
                    return  # 只有内心独白：没有可朗读的内容
                if self._voice_synth.synthesize(clean, wav_path):
                    self.voice_replay_state.emit(True)
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                else:
                    self.log.emit("⚠️ 语音合成失败")
            except Exception as e:
                self.log.emit(f"❌ 语音重播失败: {e}")
            finally:
                self.voice_replay_state.emit(False)
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
