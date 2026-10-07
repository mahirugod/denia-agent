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
import threading
import tempfile

from PySide6.QtCore import QObject, Signal, QTimer

# 复用 bot 模块（项目内 bot 目录）
BOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot")
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)


def _clean_display_text(text: str) -> str:
    """清洗显示文本：去掉表情标签（含流式输出时不完整的标签）"""
    text = re.sub(r'\[情绪:\w+\]', '', text)
    text = re.sub(r'\[@[^\]]*\]', '', text)
    text = re.sub(r'\[[\u4e00-\u9fff][^\]]*\]', '', text)
    text = re.sub(r'\[[^\[\]]+\]', '', text)
    text = re.sub(r'【[^【】]+】', '', text)
    # 流式末尾可能有不完整的标签（如 [情绪:i），一并去掉
    text = re.sub(r'\[情绪:[^\]]*\Z', '', text)
    text = re.sub(r'\[@[^\]]*\Z', '', text)
    return text.strip()


class ChatController(QObject):
    """对话控制器：管理 LLM、语音合成、麦克风输入"""

    # 对外信号
    stream_begin = Signal()
    stream_delta = Signal(str)
    stream_finish = Signal(str)   # 最终回复文本，空串表示失败/无回复
    reply_ready = Signal(str)            # 完整回复文本（清洗后）
    state_changed = Signal(str)          # idle/thinking/speaking/happy/sleeping
    log = Signal(str)
    reminder_fired = Signal(str)
    history_loaded = Signal(list)
    tool_event = Signal(str)             # 工具执行事件（JSON，协作流展示）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bot = None
        self._voice_synth = None
        self._voice_enabled = True

        # 语音线程
        self._voice_thread = None
        self._voice_interrupt = threading.Event()
        self._typing_done_event = threading.Event()
        self._stream_seg_q = None
        self._stream_had_text = False

        # 麦克风
        self._mic = None
        self._mic_active = False
        self._asr = None  # 语音识别器（懒加载）

    # ---------- 初始化 ----------
    def init_bot(self, llm_model="qwen2.5:7b", llm_api_key="ollama",
                 llm_base_url="http://127.0.0.1:11434/api/chat",
                 voice_api_url="http://127.0.0.1:9880", use_voice=True):
        """初始化机器人（可延迟，避免启动卡顿）"""
        self._voice_enabled = use_voice
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
                if self._voice_synth.is_available():
                    self.log.emit("🔊 语音合成服务已连接")
                else:
                    self.log.emit("⚠️ 语音合成服务不可用（GPT-SoVITS 未启动？）")
                    self._voice_enabled = False
            except Exception as e:
                self.log.emit(f"⚠️ 语音合成初始化失败: {e}")
                self._voice_enabled = False

        # 注册提醒回调
        try:
            from dania_tools import set_reminder_callback
            set_reminder_callback(self._on_reminder)
        except Exception:
            pass

        # 注册工具执行事件回调（协作流可视化）
        try:
            from tool_registry import set_event_callback
            set_event_callback(self._on_tool_event)
        except Exception as e:
            self.log.emit(f"⚠️ 工具事件钩子注册失败: {e}")

        # 预热 Ollama 模型（首次对话前先加载到内存，避免 500 错误）
        threading.Thread(target=self._warmup_model, daemon=True).start()

        # 后台预加载语音识别模型（首次使用需 30s，提前加载避免卡顿）
        self._whisper_model = None
        threading.Thread(target=self._preload_asr, daemon=True).start()

    def _preload_asr(self):
        """后台预加载 faster-whisper 模型"""
        try:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from faster_whisper import WhisperModel
            self._whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
            self.log.emit("🎤 语音识别模型已就绪")
        except Exception as e:
            self.log.emit(f"⚠️ 语音识别模型加载失败: {e}")

    def _warmup_model(self):
        """后台预热模型：发送一条最小请求让 Ollama 加载模型到显存/内存"""
        try:
            import requests
            self.log.emit("🔥 正在预热模型...")
            payload = {"model": "qwen2.5:7b", "messages": [
                {"role": "user", "content": "你好"}
            ], "stream": False, "keep_alive": -1, "options": {}}
            resp = requests.post("http://127.0.0.1:11434/api/chat",
                                 json=payload, timeout=120)
            # CUDA 失败时回退 CPU
            if resp.status_code != 200 and "CUDA" in resp.text:
                self.log.emit("⚠️ GPU 不可用，改用 CPU 预热...")
                payload["options"] = {"num_gpu": 0}
                resp = requests.post("http://127.0.0.1:11434/api/chat",
                                     json=payload, timeout=300)
            if resp.status_code == 200:
                self.log.emit("✅ 模型预热完成")
            else:
                self.log.emit(f"⚠️ 模型预热返回 {resp.status_code}，首次对话可能较慢")
        except Exception as e:
            self.log.emit(f"⚠️ 模型预热失败（首次对话将自动加载）: {str(e)[:60]}")

    # ---------- 发送消息 ----------
    def send_message(self, text: str):
        """发送用户消息（后台线程流式获取回复）"""
        if self._bot is None:
            self.log.emit("⚠️ 机器人尚未初始化")
            return

        text = text.strip()
        if not text:
            return

        self.state_changed.emit("thinking")

        # 停止上一轮语音
        self._stop_stream_voice()

        # 后台线程执行流式请求
        threading.Thread(
            target=self._stream_worker,
            args=(text,),
            daemon=True,
        ).start()

    def _stream_worker(self, text: str):
        try:
            seg_q = queue.Queue()
            self._stream_seg_q = seg_q
            self._stream_had_text = False
            self._typing_done_event = threading.Event()

            self.stream_begin.emit()

            # 启动流式语音线程（首句与生成并行）
            if self._voice_enabled and self._voice_synth is not None:
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
                        if attempt == 0 and ("500" in err_msg or "Internal Server Error" in err_msg):
                            self.log.emit("⏳ 模型加载中，3 秒后重试...")
                            time.sleep(3)
                            continue
                        # 判断是否连接失败（服务未启动）
                        if "11434" in err_msg or "refused" in err_msg or "Max retries" in err_msg:
                            stream_error = "⚠️ Ollama 未启动，请先在「智能体」页面启动服务"
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
                # 情绪标签 → 状态
                em = re.search(r'\[情绪:(\w+)\]', reply)
                state = em.group(1) if em else "idle"
                if state not in ("happy", "idle", "sleeping"):
                    state = "idle"
                self.state_changed.emit(state)
                self.log.emit(f"✨ 达妮娅: {clean[:60]}")
            else:
                self.log.emit("⚠️ 回复为空")
                self.state_changed.emit("idle")
        except Exception as e:
            self.log.emit(f"❌ 出错: {e}")

    def _emit_delta(self, d: str):
        self._stream_had_text = True
        # 打字机增量也要清洗，避免 [情绪:xxx] 在打字过程中闪现
        clean_d = _clean_display_text(d)
        if clean_d:
            self.stream_delta.emit(clean_d)

    # ---------- 语音合成（流式分句播放） ----------
    def _start_stream_voice(self):
        if self._voice_thread and self._voice_thread.is_alive():
            return
        self._voice_interrupt = threading.Event()
        self._typing_done_event = threading.Event()
        self._voice_thread = threading.Thread(target=self._voice_stream_player, daemon=True)
        self._voice_thread.start()

    def _stop_stream_voice(self):
        t = getattr(self, "_voice_thread", None)
        if t is not None and t.is_alive():
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
            t.join(timeout=3)

    def _voice_stream_player(self):
        import winsound
        voice_ok = False
        heard_first = False
        interrupted = False
        try:
            synth = self._voice_synth
            if synth is None:
                return
            wav_path = os.path.join(tempfile.gettempdir(), "dania_terminal_voice.wav")
            q = getattr(self, "_stream_seg_q", None)
            if q is None:
                return
            while True:
                seg = q.get()
                if seg is None:
                    break
                if self._voice_interrupt.is_set():
                    interrupted = True
                    break
                if not seg.strip():
                    continue
                clean_seg = _clean_display_text(seg)
                if not clean_seg:
                    continue
                if synth.synthesize(clean_seg, wav_path):
                    voice_ok = True
                    if not heard_first:
                        heard_first = True
                        self.state_changed.emit("speaking")
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                    try:
                        if os.path.exists(wav_path):
                            os.remove(wav_path)
                    except OSError:
                        pass
                    if self._voice_interrupt.is_set():
                        interrupted = True
                        break
                else:
                    self.log.emit("⚠️ 一句合成失败，跳过")
            if interrupted:
                try:
                    winsound.PlaySound(None, winsound.SND_PURGE)
                except Exception:
                    pass
        except Exception as e:
            self.log.emit(f"❌ 语音出错: {e}")
        finally:
            # 等打字机完成
            evt = getattr(self, "_typing_done_event", None)
            if evt is not None and not evt.is_set() and not interrupted:
                if not evt.wait(timeout=30):
                    pass
            if voice_ok:
                pass
            if not interrupted:
                self.state_changed.emit("idle")

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
    def toggle_mic(self):
        """切换麦克风录音"""
        if self._mic_active:
            self._stop_mic()
        else:
            self._start_mic()

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

    def _recognize_audio(self):
        """将录制的 PCM 保存为 WAV 并识别"""
        buf = getattr(self, "_audio_buffer", None)
        if not buf:
            return
        try:
            import wave
            wav_path = os.path.join(tempfile.gettempdir(), "dania_mic_input.wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(bytes(buf))

            # 尝试使用语音识别器
            text = self._run_asr(wav_path)
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except OSError:
                pass

            if text:
                self.log.emit(f"🎤 识别: {text}")
                self.send_message(text)
            else:
                self.log.emit("⚠️ 未识别到语音内容")
        except Exception as e:
            self.log.emit(f"❌ 语音识别失败: {e}")

    def _run_asr(self, wav_path: str) -> str:
        """语音识别：使用 faster-whisper（CPU int8）"""
        try:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from faster_whisper import WhisperModel
            if not hasattr(self, "_whisper_model") or self._whisper_model is None:
                self._whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
            segments, _ = self._whisper_model.transcribe(wav_path, language="zh")
            return "".join(s.text for s in segments).strip()
        except Exception as e:
            self.log.emit(f"⚠️ 语音识别异常: {e}")
            return ""

    # ---------- 历史 ----------
    def load_history(self):
        if self._bot is None:
            return
        try:
            history = self._bot.get_history()
            self.history_loaded.emit(history)
        except Exception as e:
            self.log.emit(f"❌ 加载历史失败: {e}")

    def backtrack(self, keep: int):
        if self._bot is None:
            return
        try:
            self._bot.backtrack_history(keep)
            self.log.emit(f"🕘 历史已回溯，保留前 {keep} 条")
            self.load_history()
        except Exception as e:
            self.log.emit(f"❌ 回溯失败: {e}")

    # ---------- 提醒 ----------
    def _on_reminder(self, content: str):
        self.reminder_fired.emit(content)

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
        self._stop_stream_voice()
        self._stop_mic()
