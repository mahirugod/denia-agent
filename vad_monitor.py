#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Silero VAD 说话监视器
- 达妮娅播放语音时监听麦克风，检测到用户持续开口 → 打断播放并捕获这句语音
- 回声误触发防护：概率阈值 + RMS 能量阈值 + 持续时长三重门槛
- 返回捕获的 PCM(16k/16bit/mono)，由调用方负责识别与提交
"""
import time
import queue
import threading
from collections import deque

BLOCK_MS = 32  # 512 样本 @16k，silero-vad 标准窗口
SAMPLE_RATE = 16000

_VAD_MODEL = None


def _load_model():
    """懒加载 silero-vad 模型（进程内缓存）"""
    global _VAD_MODEL
    if _VAD_MODEL is None:
        from silero_vad import load_silero_vad
        _VAD_MODEL = load_silero_vad()
        _VAD_MODEL.eval()
    return _VAD_MODEL


def _speech_prob(model, pcm_block: bytes) -> float:
    import torch
    import numpy as np
    samples = np.frombuffer(pcm_block, dtype=np.int16).astype(np.float32) / 32768.0
    with torch.no_grad():
        return float(model(torch.from_numpy(samples), SAMPLE_RATE).item())


def _rms(pcm_block: bytes) -> float:
    import numpy as np
    if not pcm_block:
        return 0.0
    samples = np.frombuffer(pcm_block, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples)))


class VadMonitor:
    """一次监听会话：run() 阻塞直到 触发打断并捕获完一句话 / 被外部停止"""

    def __init__(self,
                 prob_threshold: float = 0.72,
                 energy_threshold: float = 260.0,
                 min_speech_ms: int = 600,    # 持续说话时长门槛（防瞬时噪声/回声）
                 silence_ms: int = 1100,      # 触发后静音多久算说完了
                 max_capture_s: float = 15.0, # 单次捕获上限
                 should_stop=None,            # 前置阶段中止（如本会话语音已自然结束）
                 hard_stop=None,              # 捕获阶段强制中止（如应用退出）
                 on_state=None):              # 状态回调（"listening" / "capturing"）
        self.prob_threshold = prob_threshold
        self.energy_threshold = energy_threshold
        self.min_speech_ms = min_speech_ms
        self.silence_ms = silence_ms
        self.max_capture_s = max_capture_s
        self.should_stop = should_stop or (lambda: False)
        self.hard_stop = hard_stop or (lambda: False)
        self.on_state = on_state or (lambda s: None)

    def run(self):
        """返回捕获的 PCM bytes；未触发返回 None；麦克风不可用抛异常"""
        model = _load_model()

        q = queue.Queue(maxsize=64)
        mic = None
        try:
            from mic_stream import MicStream
            mic = MicStream(sample_rate=SAMPLE_RATE, block_ms=BLOCK_MS,
                            on_block=lambda d: self._offer(q, d))
            mic.start()
        except Exception:
            raise

        ring = deque(maxlen=int(700 / BLOCK_MS))  # 触发前滚动缓存 ~700ms（含起头）
        speech_ms = 0
        silence_ms = 0
        triggered = False
        captured = bytearray()
        t_start = time.time()

        try:
            while True:
                try:
                    block = q.get(timeout=0.3)
                except queue.Empty:
                    if triggered and time.time() - t_start > self.max_capture_s + 2:
                        break
                    if not triggered and self.should_stop():
                        return None
                    continue

                prob = _speech_prob(model, block)
                loud = _rms(block) >= self.energy_threshold
                is_speech = prob >= self.prob_threshold and loud

                if not triggered:
                    if self.hard_stop() or self.should_stop():
                        return None
                    if is_speech:
                        speech_ms += BLOCK_MS
                        ring.append(block)
                        if speech_ms >= self.min_speech_ms:
                            triggered = True
                            captured.extend(b"".join(ring))
                            self.on_state("capturing")
                    else:
                        speech_ms = max(0, speech_ms - BLOCK_MS * 2)
                        ring.append(block)
                    continue

                # 已触发：持续收集，静音达到阈值即认为说完了
                captured.extend(block)
                if is_speech:
                    silence_ms = 0
                else:
                    silence_ms += BLOCK_MS
                if silence_ms >= self.silence_ms:
                    break
                if time.time() - t_start > self.max_capture_s:
                    break
                if self.hard_stop():
                    break
        finally:
            if mic:
                try:
                    mic.stop()
                except Exception:
                    pass

        if triggered and len(captured) > SAMPLE_RATE // 2:  # 至少 0.5s 音频
            return bytes(captured)
        return None

    @staticmethod
    def _offer(q, data):
        try:
            q.put_nowait(data)
        except queue.Full:
            pass  # 满了丢最旧不如直接丢新块，VAD 捕获允许轻微丢帧
