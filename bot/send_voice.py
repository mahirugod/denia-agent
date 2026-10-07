#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发送指定内容的语音到群聊（命令行工具）

用法:
    python send_voice.py "要说的话" [群号]

示例:
    python send_voice.py "今天天气真好呀"
    python send_voice.py "大家好~" 750033328

不传文本参数时，进入交互模式逐条输入。

注意:
    - 需要 GPT-SoVITS API 服务已启动 (http://127.0.0.1:9880)
    - 需要 NapCat 已启动 (http://127.0.0.1:3000)
    - 与机器人主进程互不影响，可同时运行
"""
import sys
import requests

from voice_synthesizer import VoiceSynthesizer
from config_napcat import NAPCAT_HTTP_URL

# 默认群号（按需修改为你常测试的群）
DEFAULT_GROUP_ID = 750033328


def send_group_voice(group_id: int, text: str) -> bool:
    """合成语音并发送到指定群（只发语音，不发文字）"""
    if not text.strip():
        print("[错误] 文本内容为空")
        return False

    print(f"[语音] 正在合成: {text[:50]}{'...' if len(text) > 50 else ''}")
    vs = VoiceSynthesizer()
    voice_cq = vs.speak(text)

    if not voice_cq:
        print("[错误] 语音合成失败，请确认 GPT-SoVITS API 服务已启动 (http://127.0.0.1:9880)")
        return False

    payload = {"group_id": group_id, "message": voice_cq}
    try:
        resp = requests.post(f"{NAPCAT_HTTP_URL}/send_group_msg", json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[成功] 已发送语音到群 {group_id}")
        return True
    except Exception as e:
        print(f"[发送失败] {e}")
        return False


def main():
    args = sys.argv[1:]

    if not args:
        # 交互模式：逐条输入，方便连续测试
        print("=" * 40)
        print("  发送语音到群聊（交互模式）")
        print(f"  目标群: {DEFAULT_GROUP_ID}（输入 q 退出）")
        print("=" * 40)
        while True:
            try:
                text = input("\n要说的内容> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见~")
                break
            if text.lower() in ("q", "quit", "exit"):
                break
            if not text:
                continue
            send_group_voice(DEFAULT_GROUP_ID, text)
        return

    # 命令行参数模式
    text = args[0]
    try:
        group_id = int(args[1]) if len(args) > 1 else DEFAULT_GROUP_ID
    except ValueError:
        print(f"[错误] 群号必须是数字，收到: {args[1]}")
        sys.exit(1)
    send_group_voice(group_id, text)


if __name__ == "__main__":
    main()
