#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音识别模块 - 使用 Whisper 将 QQ 语音转成文字
流程：解析CQ:record码 → 下载语音 → SILK解码为WAV → Whisper转写
"""

import os
import re
import html
import io
import wave
import requests
from typing import Optional
from datetime import datetime

# 配置
WHISPER_MODEL_SIZE = "base"  # tiny(~75MB)/base(~140MB)/small(~460MB)/medium(~1.5GB)
NAPCAT_HTTP_URL = "http://127.0.0.1:3000"
VOICE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_cache", "voice")
REQUEST_TIMEOUT = 60  # 语音转写超时（秒）

os.makedirs(VOICE_CACHE_DIR, exist_ok=True)

# 全局 Whisper 模型（只加载一次）
_whisper_model = None


def _get_whisper_model():
    """懒加载 Whisper 模型，避免启动时阻塞"""
    global _whisper_model
    if _whisper_model is None:
        import whisper
        print(f"[语音识别] 加载 Whisper \"{WHISPER_MODEL_SIZE}\" 模型（首次需要下载，约140MB）...")
        _whisper_model = whisper.load_model(WHISPER_MODEL_SIZE)
        print("[语音识别] Whisper 模型加载完成")
    return _whisper_model


def parse_cq_record(raw_msg: str) -> Optional[dict]:
    """
    从 QQ 消息中提取语音 CQ 码

    Returns:
        {"file": "xxx.silk", "url": "http://..."} 或 None
    """
    raw_msg = html.unescape(raw_msg)
    pattern = r'\[CQ:(?:record|voice)[^\]]*\]'

    for match in re.finditer(pattern, raw_msg):
        cq_str = match.group()
        info = {}

        file_match = re.search(r'file=([^,\]]+)', cq_str)
        url_match = re.search(r'url=([^\],]+)', cq_str)

        if file_match:
            info["file"] = file_match.group(1)
        if url_match:
            info["url"] = url_match.group(1)

        return info  # 只处理第一条语音

    return None


def download_voice_url(url: str) -> Optional[str]:
    """从 URL 下载语音文件"""
    try:
        filename = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.silk"
        filepath = os.path.join(VOICE_CACHE_DIR, filename)

        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://multimedia.nt.qq.com.cn/",
            "Accept": "*/*",
        }, timeout=15)

        if resp.status_code == 200 and len(resp.content) > 100:
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            print(f"[语音识别] 语音下载成功: {filepath} ({len(resp.content)}bytes)")
            return filepath
        else:
            print(f"[语音识别] 语音下载失败: HTTP {resp.status_code}")
            return None
    except Exception as e:
        print(f"[语音识别] 语音下载异常: {e}")
        return None


def download_voice_from_napcat(file_name: str) -> Optional[str]:
    """通过 NapCat get_record API 获取语音文件"""
    try:
        # NapCat HTTP API: POST /get_record
        url = f"{NAPCAT_HTTP_URL}/get_record"
        payload = {
            "file": file_name,
            "out_dir": VOICE_CACHE_DIR,
            "format": "silk"
        }

        resp = requests.post(url, json=payload, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            # 返回格式: {"status": "ok", "data": {"file": "path/to/file.silk"}}
            ret_file = data.get("data", {}).get("file", "")
            if ret_file and os.path.exists(ret_file):
                print(f"[语音识别] NapCat get_record 成功: {ret_file}")
                return ret_file
            else:
                print(f"[语音识别] NapCat 返回的文件路径无效: {data}")
                return None
        else:
            print(f"[语音识别] NapCat API 错误: HTTP {resp.status_code}")
            return None
    except Exception as e:
        print(f"[语音识别] NapCat get_record 异常: {e}")
        return None


def silk_to_wav(silk_path: str) -> Optional[str]:
    """使用 pysilk 将 SILK 文件解码为 WAV"""
    try:
        with open(silk_path, 'rb') as f:
            silk_data = f.read()

        input_io = io.BytesIO(silk_data)
        output_io = io.BytesIO()

        import pysilk
        pysilk.decode(input_io, output_io)
        pcm_data = output_io.getvalue()

        if not pcm_data or len(pcm_data) < 100:
            print("[语音识别] SILK 解码结果为空")
            return None

        # 写为 WAV 文件
        wav_path = os.path.splitext(silk_path)[0] + ".wav"
        with wave.open(wav_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit PCM
            wf.setframerate(24000)  # SILK 标准采样率
            wf.writeframes(pcm_data)

        print(f"[语音识别] SILK -> WAV: {wav_path} ({len(pcm_data)}bytes PCM)")
        return wav_path
    except Exception as e:
        print(f"[语音识别] SILK 解码失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def transcribe_with_whisper(wav_path: str) -> Optional[str]:
    """使用 Whisper 将音频转写为文字"""
    try:
        model = _get_whisper_model()
        # language="zh" 指定中文，提高准确率
        result = model.transcribe(wav_path, language="zh", verbose=False)
        text = result.get("text", "").strip()

        if text:
            print(f"[语音识别] Whisper 转写结果: {text}")
            return text
        else:
            print("[语音识别] Whisper 返回空文本")
            return None
    except Exception as e:
        print(f"[语音识别] Whisper 转写失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def process_voice_message(raw_msg: str) -> Optional[str]:
    """
    处理语音消息，返回转写后的文字

    Args:
        raw_msg: 原始 QQ 消息（包含 CQ:record 码）

    Returns:
        转写后的文字，失败返回 None
    """
    info = parse_cq_record(raw_msg)
    if not info:
        print("[语音识别] 未找到语音 CQ 码")
        return None

    print(f"[语音识别] 发现语音消息: file={info.get('file', '?')}")
    local_path = None
    is_temp = False

    # 策略1：尝试 URL 直接下载
    if info.get("url"):
        local_path = download_voice_url(info["url"])

    # 策略2：通过 NapCat API 获取
    if not local_path and info.get("file"):
        local_path = download_voice_from_napcat(info["file"])

    if not local_path:
        print("[语音识别] 无法获取语音文件")
        return None

    # 检测文件格式，如果不是 WAV 则需要转换
    ext = os.path.splitext(local_path)[1].lower()
    wav_path = local_path

    if ext in ('.silk', '.amr', '.slk'):
        wav_path = silk_to_wav(local_path)
        is_temp = True  # WAV 是临时生成的
    elif ext != '.wav':
        # 未知格式，尝试用 ffmpeg 转换
        try:
            import subprocess
            import shutil
            ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
            new_wav = os.path.splitext(local_path)[0] + ".wav"
            result = subprocess.run(
                [ffmpeg, "-y", "-i", local_path, "-ar", "16000", "-ac", "1", new_wav],
                capture_output=True, timeout=15
            )
            if result.returncode == 0 and os.path.exists(new_wav):
                wav_path = new_wav
                is_temp = True
            else:
                print(f"[语音识别] ffmpeg 转换失败")
                return None
        except Exception as e:
            print(f"[语音识别] ffmpeg 转换异常: {e}")
            return None

    # 转写
    text = transcribe_with_whisper(wav_path)

    # 清理临时文件
    try:
        if is_temp and os.path.exists(wav_path):
            os.remove(wav_path)
    except OSError:
        pass

    return text


if __name__ == "__main__":
    print("=== 语音识别模块测试 ===")
    print(f"Whisper 模型大小: {WHISPER_MODEL_SIZE}")
    print(f"语音缓存目录: {VOICE_CACHE_DIR}")

    # 测试模型加载
    try:
        model = _get_whisper_model()
        print(f"模型加载成功: {model}")
    except Exception as e:
        print(f"模型加载失败: {e}")
