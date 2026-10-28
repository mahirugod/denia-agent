# -*- coding: utf-8 -*-
"""
本地流式语音识别（sherpa-onnx）

用 OnlineRecognizer 边收麦克风帧边出字，句尾只需 finalize 一次即可拿到完整文本，
不再把整段录音丢给 faster-whisper 重新识别。
模型缺失或依赖不可用时 available() 返回 False，调用方回落到原 Whisper 路径。
"""
import glob
import os
import re
import threading
import time

_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models",
    "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
)

_recognizer = None
_recognizer_lock = threading.Lock()


def _pick(pattern: str) -> str:
    files = sorted(glob.glob(os.path.join(_MODEL_DIR, pattern)))
    if not files:
        return ""
    # 同一模型可能同时带 fp32/int8，优先 int8（CPU 上更快，识别质量差异很小）
    for f in files:
        if "int8" in os.path.basename(f).lower():
            return f
    return files[0]


def available() -> bool:
    if not os.path.isdir(_MODEL_DIR):
        return False
    if not os.path.isfile(os.path.join(_MODEL_DIR, "tokens.txt")):
        return False
    return bool(_pick("*encoder*.onnx") and _pick("*decoder*.onnx") and _pick("*joiner*.onnx"))


def _get_recognizer():
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    with _recognizer_lock:
        if _recognizer is not None:
            return _recognizer
        import sherpa_onnx

        _recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=os.path.join(_MODEL_DIR, "tokens.txt"),
            encoder=_pick("*encoder*.onnx"),
            decoder=_pick("*decoder*.onnx"),
            joiner=_pick("*joiner*.onnx"),
            num_threads=2,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=False,
        )
        return _recognizer


class StreamingASR:
    """一句话一个实例：accept() 送入 16k/16bit/mono PCM，finalize() 取最终文本"""

    def __init__(self, on_partial=None, partial_interval: float = 0.2):
        self._rec = _get_recognizer()
        self._stream = self._rec.create_stream()
        self._on_partial = on_partial
        self._interval = partial_interval
        self._last_emit = 0.0
        self._last_text = ""
        self._lock = threading.Lock()

    def _decode(self):
        rec, stream = self._rec, self._stream
        while True:
            if hasattr(rec, "is_ready"):
                ready = rec.is_ready(stream)
            else:
                ready = stream.is_ready()
            if not ready:
                break
            rec.decode_stream(stream)

    def _result(self) -> str:
        text = self._rec.get_result(self._stream) or ""
        # 模型会在中文里夹大写英文（MONDAY / LIBR），统一小写；去掉汉字间多余空格
        text = re.sub(r"[A-Z]{2,}", lambda m: m.group(0).lower(), text)
        text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
        return text.strip()

    def accept(self, pcm: bytes, ts: float = 0.0):
        if not pcm:
            return
        if not ts:
            ts = time.monotonic()
        try:
            import numpy as np

            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            with self._lock:
                self._stream.accept_waveform(16000, samples)
                self._decode()
                if (self._on_partial is not None
                        and ts - self._last_emit >= self._interval):
                    text = self._result()
                    self._last_emit = ts
                    if text and text != self._last_text:
                        self._last_text = text
                        self._on_partial(text)
        except Exception:
            # 单帧失败不打断整句采集
            pass

    def finalize(self) -> str:
        try:
            import numpy as np

            with self._lock:
                # 补 0.3s 静音，让尾部音素解码完
                self._stream.accept_waveform(16000, np.zeros(int(16000 * 0.3), dtype=np.float32))
                self._stream.input_finished()
                self._decode()
                return self._result()
        except Exception:
            return ""
