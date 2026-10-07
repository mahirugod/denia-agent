#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零依赖麦克风流式采集模块
- 使用 Windows winmm waveIn API（ctypes），无需 sounddevice/pyaudio
- 16kHz 单声道 16bit PCM，按块回调输出
- CALLBACK_EVENT 模式：后台线程等待事件，避免 ctypes 回调风险
"""
import ctypes
import threading

WAVE_MAPPER = 0xFFFFFFFF
CALLBACK_EVENT = 0x00050000
WHDR_DONE = 0x00000001


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint),
        ("dwBytesRecorded", ctypes.c_uint),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("dwLoops", ctypes.c_uint),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


class MicStream:
    """麦克风流式采集：start 后持续通过 on_block(bytes) 回调 PCM 数据"""

    NUM_BUFFERS = 8  # 8 个缓冲轮转，防丢块

    def __init__(self, sample_rate: int = 16000, block_ms: int = 50, on_block=None):
        self.rate = sample_rate
        self.block_bytes = int(sample_rate * block_ms / 1000) * 2  # 16bit mono
        self.on_block = on_block
        self._winmm = ctypes.windll.winmm
        self._kernel32 = ctypes.windll.kernel32
        self._handle = None
        self._event = None
        self._bufs = []
        self._hdrs = []
        self._thread = None
        self._running = False

    def start(self):
        wfx = _WAVEFORMATEX(1, 1, self.rate, self.rate * 2, 2, 16, 0)
        self._event = self._kernel32.CreateEventW(None, False, False, None)
        h = ctypes.c_void_p()
        rc = self._winmm.waveInOpen(
            ctypes.byref(h), WAVE_MAPPER, ctypes.byref(wfx),
            ctypes.c_void_p(self._event), 0, CALLBACK_EVENT
        )
        if rc != 0:
            self._cleanup()
            raise RuntimeError(f"waveInOpen 失败 (code={rc})，可能没有可用麦克风")
        self._handle = h

        for _ in range(self.NUM_BUFFERS):
            buf = ctypes.create_string_buffer(self.block_bytes)
            hdr = _WAVEHDR()
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = self.block_bytes
            self._winmm.waveInPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self._winmm.waveInAddBuffer(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self._bufs.append(buf)
            self._hdrs.append(hdr)

        self._running = True
        rc = self._winmm.waveInStart(h)
        if rc != 0:
            self._cleanup()
            raise RuntimeError(f"waveInStart 失败 (code={rc})")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            if self._kernel32.WaitForSingleObject(self._event, 300) != 0:
                continue
            for hdr in self._hdrs:
                if not (hdr.dwFlags & WHDR_DONE):
                    continue
                n = hdr.dwBytesRecorded
                if n:
                    try:
                        data = ctypes.string_at(hdr.lpData, n)
                        if self.on_block:
                            self.on_block(data)
                    except Exception:
                        pass
                # 重新排队这块缓冲
                hdr.dwFlags = 0
                hdr.dwBytesRecorded = 0
                self._winmm.waveInPrepareHeader(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
                self._winmm.waveInAddBuffer(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
        self._cleanup()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _cleanup(self):
        if self._handle:
            try:
                self._winmm.waveInReset(self._handle)
                for hdr in self._hdrs:
                    self._winmm.waveInUnprepareHeader(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
                self._winmm.waveInClose(self._handle)
            except Exception:
                pass
            self._handle = None
        self._hdrs = []
        self._bufs = []
        if self._event:
            try:
                self._kernel32.CloseHandle(self._event)
            except Exception:
                pass
            self._event = None
