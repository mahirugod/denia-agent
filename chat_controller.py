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


def _clean_display_text(text: str) -> str:
    """清洗显示文本：去掉表情标签（含流式输出时不完整的标签）"""
    text = re.sub(r"\[情绪:\w+\]", "", text)
    text = re.sub(r"\[@[^\]]*\]", "", text)
    text = re.sub(r"\[[\u4e00-\u9fff][^\]]*\]", "", text)
    text = re.sub(r"\[[^\[\]]+\]", "", text)
    text = re.sub(r"【[^【】]+】", "", text)
    # 流式末尾可能有不完整的标签（如 [情绪:i），一并去掉
    text = re.sub(r"\[情绪:[^\]]*\Z", "", text)
    text = re.sub(r"\[@[^\]]*\Z", "", text)
    return text.strip()


# 情绪标签 → 立绘状态（扩充词映射到现有立绘资产；Web 端另有 CSS 动画区分细腻情绪）
class ChatController(QObject):
    """对话控制器：管理 LLM、语音合成、麦克风输入"""

    # 对外信号
    stream_begin = Signal()
    stream_delta = Signal(str)
    stream_finish = Signal(str)  # 最终回复文本，空串表示失败/无回复
    reply_ready = Signal(str)  # 完整回复文本（清洗后）
    state_changed = Signal(str)  # idle/thinking/speaking/sleeping
    voice_playing = Signal(bool)  # 语音实际播放中（Web 端口型同步用）
    log = Signal(str)
    reminder_fired = Signal(str)
    _reminder_popup = Signal(str)  # 内部：调度到 GUI 线程弹窗
    tool_event = Signal(str)  # 工具执行事件（JSON，协作流展示）
    user_message = Signal(str)  # 语音识别出的用户文本（用于回显为用户气泡）
    voice_input_result = Signal(str)  # 识别结果/打断后写回输入框（空串表示被打断）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bot = None
        self._voice_synth = None
        self._voice_enabled = True

        # 语音线程（合成/播放双线程流水线）
        self._voice_thread = None
        self._voice_synth_thread = None
        self._stream_ready_q = None
        self._voice_interrupt = threading.Event()
        self._typing_done_event = threading.Event()
        self._stream_seg_q = None
        self._stream_had_text = False

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
        """后台预加载 faster-whisper 模型"""
        try:
            # 禁用 HuggingFace CAS 分片下载（走 hf-mirror 直连，避免 xethub 401）
            os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
            os.environ["HF_HUB_DOWNLOAD_INBackground"] = "0"
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from faster_whisper import WhisperModel

            self._whisper_model = WhisperModel(
                "small", device="cpu", compute_type="int8",
                download_root=os.path.join(os.path.dirname(__file__), "models", "whisper"),
            )
            self.log.emit("🎤 语音识别模型已就绪")
        except Exception as e:
            self.log.emit(f"⚠️ 语音识别模型加载失败: {e}")

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

    # ---------- 发送消息 ----------
    def send_message(self, text: str):
        """发送用户消息（新开线程流式处理）"""
        if self._bot is None:
            self.log.emit("⚠️ 机器人尚未初始化")
            return

        text = text.strip()
        if not text:
            return

        threading.Thread(target=self._stream_worker, args=(text,), daemon=True).start()

    def _stream_worker(self, text: str):
        try:
            seg_q = queue.Queue()
            self._stream_seg_q = seg_q
            self._stream_had_text = False
            self._typing_done_event = threading.Event()

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

            self.log.emit(f"⏳ 思考中 ({text[:20]}...)")

            reply = ""
            stream_error = ""
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
                        # 500 错误通常是模型正在加载，等待 3 秒后重试
                        if attempt == 0 and (
                            "500" in err_msg or "Internal Server Error" in err_msg
                        ):
                            self.log.emit("⏳ 模型加载中，3 秒后重试...")
                            time.sleep(3)
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
                            _local_name = "本地推理服务"
                            _is_ollama = "ollama" in (self._llm_base_url or "").lower() or "11434" in (self._llm_base_url or "")
                            if _is_ollama:
                                _local_name = "Ollama"
                            stream_error = f"⚠️ {_local_name} 未启动，请先在「智能体」页面启动服务"
                        else:
                            stream_error = f"⚠️ 回复失败: {err_msg[:40]}"
                        reply = ""
                        break
            finally:
                self.stream_finish.emit(stream_error)
                seg_q.put(None)  # 语音队列哨兵

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

    def _emit_delta(self, d: str):
        self._stream_had_text = True
        # 打字机增量也要清洗，避免 [情绪:xxx] 在打字过程中闪现
        clean_d = _clean_display_text(d)
        if clean_d:
            self.stream_delta.emit(clean_d)

    # ---------- 语音合成（流式分句，合成/播放流水线） ----------
    def _start_stream_voice(self):
        # 每次会话独立线程组：队列/事件按会话闭包传递，旧线程卡死也不影响新一轮
        interrupt = threading.Event()
        self._voice_interrupt = interrupt
        self._typing_done_event = threading.Event()
        session_done = threading.Event()
        ready_q = queue.Queue(maxsize=3)
        self._stream_ready_q = ready_q
        self._voice_synth_thread = threading.Thread(
            target=self._voice_synth_worker, args=(ready_q, interrupt), daemon=True
        )
        self._voice_thread = threading.Thread(
            target=self._voice_stream_player,
            args=(ready_q, interrupt, self._typing_done_event, session_done),
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
        try:
            from vad_monitor import VadMonitor

            mon = VadMonitor(
                should_stop=lambda: interrupt.is_set() or session_done.is_set(),
                hard_stop=lambda: self._vad_abort.is_set(),
            )
            data = mon.run()
        except Exception as e:
            self.log.emit(f"⚠️ VAD 监听不可用: {str(e)[:60]}")
            return
        if not data or session_done.is_set():
            return
        try:
            self._stop_stream_voice()  # 打断播放（player 退出后会话标记 done）
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
                clean_seg = _clean_display_text(seg)
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

    def _voice_stream_player(self, ready_q, interrupt, typing_done, session_done=None):
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
                        self.voice_playing.emit(True)
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
            self.voice_playing.emit(False)
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
            return "".join(s.text for s in segments).strip()
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
                clean = _clean_display_text(text)
                if self._voice_synth.synthesize(clean, wav_path):
                    self.voice_playing.emit(True)
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                    self.voice_playing.emit(False)
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
