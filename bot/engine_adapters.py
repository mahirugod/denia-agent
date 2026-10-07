#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTS 多引擎抽象层（参考 Shinsekai 的 Adapter + Factory 模式）
- TTSAdapter: 引擎适配器基类
- GPTSoVITSAdapter: GPT-SoVITS HTTP 引擎（唯一引擎，失败即不发声）
- TTSFactory: 引擎注册表 + 按优先级取可用引擎
"""
import os
import re
import subprocess

import requests


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

    def __init__(self, api_url: str = "http://127.0.0.1:9880"):
        self.api_url = api_url

    def synthesize(self, text: str, output_path: str) -> bool:
        try:
            # POST JSON 避免 URL 编码路径问题
            payload = {
                "text": text,
                "text_lang": "zh",
                "ref_audio_path": self.REF_AUDIO,
                "prompt_text": self.PROMPT_TEXT,
                "prompt_lang": "zh",
                "text_split_method": "cut5",
                "batch_size": 1,
                "media_type": "wav",
                "streaming_mode": False,
            }
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

    def is_available(self) -> bool:
        """轻量探测：服务是否在线（端口+模型就绪），不实际合成"""
        try:
            # 先尝试 /docs 或 /openapi.json（FastAPI 自带）
            response = requests.get(f"{self.api_url}/docs", timeout=3)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        try:
            # 再试端口连通性
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(("127.0.0.1", 9880))
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
    - 时间 20:30 → 二十点三十分
    - 百分比 85% → 百分之八十五
    - 小数 3.5 → 三点五
    - 普通整数 → 中文读法
    """
    if not text:
        return text

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
