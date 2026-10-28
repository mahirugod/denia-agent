#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户明确要求「记住」的事实。
简单追加式存储（bot/memory/facts.md），每轮按尾部截取注入对话 prompt，
保证跨会话、跨轮次都还在。
"""
import os
import time

FACTS_PATH = os.path.join(os.path.dirname(__file__), "memory", "facts.md")

_cache = {"mtime": 0.0, "text": ""}


def add_fact(text: str) -> str:
    """追加一条事实，返回写入的行"""
    text = (text or "").strip().strip("。，,；; ")
    if not text:
        return ""
    os.makedirs(os.path.dirname(FACTS_PATH), exist_ok=True)
    line = f"- [{time.strftime('%Y-%m-%d')}] {text}"
    with open(FACTS_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    _cache["mtime"] = 0.0  # 让下次读取重新读盘
    return line


def read_facts(max_chars: int = 800) -> str:
    """读最近的事实（按字符上限截取尾部），没有则返回空串"""
    try:
        mtime = os.path.getmtime(FACTS_PATH)
    except OSError:
        return ""
    if mtime != _cache["mtime"]:
        try:
            with open(FACTS_PATH, "r", encoding="utf-8") as f:
                _cache["text"] = f.read()
        except OSError:
            return ""
        _cache["mtime"] = mtime
    text = _cache["text"].strip()
    if len(text) <= max_chars:
        return text
    # 从后往前按行拼，避免把一行劈开
    out, used = [], 0
    for line in reversed(text.splitlines()):
        if used + len(line) + 1 > max_chars:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(reversed(out))
