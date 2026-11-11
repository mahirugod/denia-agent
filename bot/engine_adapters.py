#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTS 多引擎抽象层（参考 Shinsekai 的 Adapter + Factory 模式）
- TTSAdapter: 引擎适配器基类
- GPTSoVITSAdapter: GPT-SoVITS HTTP 引擎（唯一引擎，失败即不发声）
- TTSFactory: 引擎注册表 + 按优先级取可用引擎
"""
import json
import os
import random
import re
import subprocess

import requests


def _parse_wav_header(buf: bytes):
    """解析流式 wav 头：返回 (header_end, samplerate, channels, sampwidth)。

    GPT-SoVITS 流式响应的第一块是标准 wav 头（data 段长度为 0），其后是裸 PCM；
    头可能被网络拆成多块，攒够再解析，解析不出来返回 None。
    """
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return None
    pos = 12
    sr = ch = width = None
    while pos + 8 <= len(buf):
        cid = buf[pos:pos + 4]
        size = int.from_bytes(buf[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            if len(buf) < pos + 8 + size:
                return None
            fmt = buf[pos + 8:pos + 8 + size]
            if len(fmt) < 16:
                return None
            ch = int.from_bytes(fmt[2:4], "little")
            sr = int.from_bytes(fmt[4:8], "little")
            bits = int.from_bytes(fmt[14:16], "little")
            width = max(1, (bits + 7) // 8)
        elif cid == b"data":
            if sr is None or ch is None or width is None:
                return None
            return pos + 8, sr, ch, width
        pos += 8 + size
    return None


class TTSAdapter:
    """TTS 引擎适配器基类"""
    name = "base"

    def synthesize(self, text: str, output_path: str) -> bool:
        raise NotImplementedError

    def is_available(self) -> bool:
        raise NotImplementedError


class GPTSoVITSAdapter(TTSAdapter):
    """GPT-SoVITS 引擎（达妮娅原声）"""
    name = "gpt-sovits"

    REF_AUDIO = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "GPT-SoVITS", "output.wav_0009342720_0009558400.wav",
    ).replace("\\", "/")
    PROMPT_TEXT = "怎么啊？如果有你在也不放心，那就干脆给我也装个限制器或者炸弹喽。"

    # 采样/语速参数文件（可调，让语气更自然）
    _PARAMS_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tts_params.json",
    )
    # 默认值（文件不存在时用）
    _DEFAULTS = {
        "temperature": 0.6,   # 降低随机性，减少偶发怪读/错字；0.6~1.0 自然
        "top_P": 0.7,
        "top_K": 10,
        "speed_factor": 1.0,  # 语速 1.0=正常，1.1=稍快，0.9=稍慢
        "random_seed": True,  # True=每次随机采样（语气不重复），False=固定稳定
        # 首句抢先出声：首句在第一个逗号处切分后，再让 GPT-SoVITS 用 cut5 切一次，
        # 第一块语义 token 更早凑够 → 首包更快。后续句仍用 cut0，避免二次切词吞字。
        "first_cut5": True,
        "first_split_method": "cut5",
        "fragment_interval": 0.3,  # 分片之间的静音间隔
        "min_chunk_length": 16,    # 每块最小语义 token 数（调小 → 首包更早、更碎）
        "overlap_length": 2,       # 流式块之间的重叠 token
    }

    def __init__(self, api_url: str = "http://127.0.0.1:9880"):
        self.api_url = api_url
        self.params = self._load_params()

    def _load_params(self) -> dict:
        p = dict(self._DEFAULTS)
        try:
            if os.path.exists(self._PARAMS_FILE):
                with open(self._PARAMS_FILE, "r", encoding="utf-8") as f:
                    p.update(json.load(f))
        except Exception as e:
            print(f"[TTS] 读 tts_params.json 失败，用默认: {e}")
        return p

    def _payload(self, text: str, streaming_mode=False, first=False) -> dict:
        """构造 /tts 请求体；streaming_mode=int 时按 GPT-SoVITS 的流式档位传。

        first=True 时（本轮第一句）改用 first_split_method（默认 cut5）：
        上游已把首句切到第一个逗号，这里再切一次能让首块更快凑够 token。
        后续句保持 cut0，避免二次切断词导致吞字/串音。
        """
        # 随机种子：每次换采样，语气不重复（设为 False 可回退固定 42）
        if self.params.get("random_seed", True):
            seed = random.randint(0, 100000)
        else:
            seed = 42
        split_method = "cut0"
        if first and self.params.get("first_cut5", True):
            split_method = self.params.get("first_split_method", "cut5")
        payload = {
            "text": text,
            "text_lang": "zh",
            "ref_audio_path": self.REF_AUDIO,
            "prompt_text": self.PROMPT_TEXT,
            "prompt_lang": "zh",
            # cut0=不再二次切分：上游已按 80 字+标点切好，cut5 会二次切断词，导致吞字/串音
            "text_split_method": split_method,
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": streaming_mode,
            "seed": seed,
            "temperature": self.params.get("temperature", 0.8),
            "top_P": self.params.get("top_P", 0.8),
            "top_K": self.params.get("top_K", 20),
            "speed_factor": self.params.get("speed_factor", 1.0),
        }
        if streaming_mode:
            for key in ("fragment_interval", "min_chunk_length", "overlap_length"):
                if key in self.params:
                    payload[key] = self.params[key]
        return payload

    def synthesize(self, text: str, output_path: str) -> bool:
        try:
            payload = self._payload(text, False)
            response = requests.post(f"{self.api_url}/tts", json=payload, timeout=60)
            if response.status_code == 200 and len(response.content) > 100:
                with open(output_path, "wb") as f:
                    f.write(response.content)
                return True
            print(f"[TTS] GPT-SoVITS 失败: HTTP {response.status_code} {response.content[:120]}")
            return False
        except Exception as e:
            print(f"[TTS] GPT-SoVITS 异常: {e}")
            return False

    def synthesize_stream(self, text: str, output_path: str, on_start=None,
                          on_frames=None, should_stop=None, first=False):
        """流式合成（streaming_mode=2）：边收边把裸 PCM 回调给播放层。

        first=True 表示本轮第一句，改用 cut5 抢先出声（可通过 tts_params.json
        的 first_cut5 关闭，做 A/B 对比）。

        GPT-SoVITS 的流式响应是「wav 头（第一块）+ 裸 PCM（后续块）」，
        这里解析头拿到采样率/声道，把后续帧交给 on_frames；写声卡阻塞会形成背压。
        返回 (samplerate, channels, sampwidth, frames)；完全没收到音频返回 None。
        """
        import wave as _wave

        wf = None
        header_done = False
        sr = ch = width = 0
        total = 0
        try:
            payload = self._payload(text, 2, first=first)
            with requests.post(f"{self.api_url}/tts", json=payload,
                               timeout=(10, 20), stream=True) as resp:
                if resp.status_code != 200:
                    print(f"[TTS] GPT-SoVITS 流式失败: HTTP {resp.status_code} {resp.content[:120]}")
                    return None
                buf = b""
                for raw in resp.iter_content(chunk_size=4096):
                    if should_stop is not None and should_stop():
                        break
                    if not raw:
                        continue
                    if not header_done:
                        buf += raw
                        parsed = _parse_wav_header(buf)
                        if parsed is None:
                            continue
                        header_len, sr, ch, width = parsed
                        data = buf[header_len:]
                        buf = b""
                        if width != 2:
                            # 播放层只接受 16bit PCM；其它位深直接放弃流式，交给整段合成
                            print(f"[TTS] 流式返回 {width * 8}bit，放弃流式")
                            return None
                        wf = _wave.open(output_path, "wb")
                        wf.setnchannels(ch)
                        wf.setsampwidth(width)
                        wf.setframerate(sr)
                        header_done = True
                        if on_start is not None:
                            on_start(sr, ch, width)
                    else:
                        data = raw
                    if data:
                        wf.writeframes(data)
                        total += len(data) // max(1, width * ch)
                        if on_frames is not None:
                            on_frames(data)
            if not header_done:
                print("[TTS] GPT-SoVITS 流式返回无有效音频")
                return None
            return (sr, ch, width, total)
        except Exception as e:
            print(f"[TTS] GPT-SoVITS 流式异常: {e}")
            # 已经播过的部分不回退重放，只报告已收帧数
            return (sr, ch, width, total) if (header_done and total > 0) else None
        finally:
            if wf is not None:
                try:
                    wf.close()
                except Exception:
                    pass

    def is_available(self) -> bool:
        """轻量探测：服务是否在线（端口+模型就绪），不实际合成"""
        import urllib.parse
        parsed = urllib.parse.urlsplit(self.api_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9880
        try:
            # 先尝试 /docs 或 /openapi.json（FastAPI 自带）
            response = requests.get(f"{self.api_url}/docs", timeout=3)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        try:
            # 再试端口连通性（用 api_url 的 host:port，而非写死 127.0.0.1:9880）
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            return False


class TTSFactory:
    """
    TTS 引擎工厂（参考 Shinsekai TTSAdapterFactory）
    - register: 注册新引擎
    - create: 按名字取引擎
    - priority_chain: 按优先级返回可用引擎列表（降级链）
    """
    _adapters = {
        "gpt-sovits": GPTSoVITSAdapter,
    }
    _priority = ["gpt-sovits"]  # 唯一引擎：失败即不发声，不降级

    @classmethod
    def register(cls, name: str, adapter_class, priority: int = None):
        """注册新引擎；priority 插入优先级位置（默认最低）"""
        cls._adapters[name.lower()] = adapter_class
        if priority is None:
            cls._priority.append(name.lower())
        else:
            cls._priority.insert(min(priority, len(cls._priority)), name.lower())

    @classmethod
    def create(cls, name: str) -> TTSAdapter:
        adapter_class = cls._adapters.get(name.lower())
        if adapter_class is None:
            raise ValueError(f"未注册的TTS引擎: {name}，可选: {list(cls._adapters)}")
        return adapter_class()

    @classmethod
    def priority_chain(cls, probe: bool = True) -> list:
        """
        返回按优先级排序的可用引擎实例列表。
        probe=True 时逐个探测可用性（GPT-SoVITS 探测较快，放最前）。
        """
        chain = []
        for name in cls._priority:
            try:
                adapter = cls.create(name)
            except ValueError:
                continue
            if not probe or adapter.is_available():
                chain.append(adapter)
        return chain


# 数字读法规范化（TTS 长文本处理细节）
_CN_DIGITS = "零一二三四五六七八九"


def _num_to_cn(n: int) -> str:
    """整数转中文读法（0-9999 足够日常对话）"""
    if n == 0:
        return "零"
    digits = [int(d) for d in str(n)]
    units = ["", "十", "百", "千"]
    result = []
    length = len(digits)
    for i, d in enumerate(digits):
        unit = units[length - 1 - i]
        if d == 0:
            if result and result[-1] != "零":
                result.append("零")
        elif d == 1 and unit == "十" and not result:
            result.append("十")
        else:
            result.append(_CN_DIGITS[d] + unit)
    text = "".join(result).rstrip("零")
    return text or "零"


def normalize_for_tts(text: str) -> str:
    """
    数字读法规范化，让 GPT-SoVITS 读得更自然：
    - 温度 15℃ / -5℃ / 15°C → 十五摄氏度 / 零下五摄氏度 / 十五摄氏度
    - 时间 20:30 → 二十点三十分
    - 百分比 85% → 百分之八十五
    - 小数 3.5 → 三点五
    - 普通整数 → 中文读法
    """
    if not text:
        return text

    # 温度（必须在数字读法之前处理，避免数字先被转成中文）
    # 先处理范围温度 "15~23℃" / "-1到2℃" / "15到23度" → "十五到二十三摄氏度"
    def _temp_range_repl(m):
        lo_str, hi = m.group(1), int(m.group(2))
        lo = int(lo_str)
        lo_cn = f"零下{_num_to_cn(abs(lo))}" if lo < 0 else _num_to_cn(lo)
        hi_cn = f"零下{_num_to_cn(abs(hi))}" if hi < 0 else _num_to_cn(hi)
        return f"{lo_cn}到{hi_cn}摄氏度"
    text = re.sub(r'(-?\d+)\s*(?:[~～]|到)\s*(-?\d+)\s*(?:℃|°C|度)', _temp_range_repl, text)
    # 再处理单温度 -5℃ / 23°C / 15度
    def _temp_repl(m):
        sign, num = m.group(1) or "", m.group(2)
        cn_num = _num_to_cn(int(num))
        if sign:
            return f"零下{cn_num}摄氏度"
        return f"{cn_num}摄氏度"
    text = re.sub(r'(-?)(\d+)(?:℃|°C|度)', _temp_repl, text)

    # 时间 HH:MM / HH点MM
    def _time_repl(m):
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{_num_to_cn(h)}点{_num_to_cn(mi)}分" if mi else f"{_num_to_cn(h)}点整"
        return m.group(0)
    text = re.sub(r'(\d{1,2}):(\d{2})', _time_repl, text)

    # 百分比
    def _pct_repl(m):
        return f"百分之{_num_to_cn(int(m.group(1)))}"
    text = re.sub(r'(\d+)%', _pct_repl, text)

    # 小数
    def _dec_repl(m):
        int_part, dec_part = int(m.group(1)), m.group(2)
        dec_cn = "".join(_CN_DIGITS[int(d)] for d in dec_part if d.isdigit())
        return f"{_num_to_cn(int_part)}点{dec_cn}"
    text = re.sub(r'(\d+)\.(\d+)', _dec_repl, text)

    # 普通整数（词边界，4位以内转中文，长数字如手机号保留）
    def _int_repl(m):
        n = int(m.group(0))
        return _num_to_cn(n) if n < 10000 else m.group(0)
    text = re.sub(r'\d+', _int_repl, text)

    return text
