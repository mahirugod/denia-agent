#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅工具集（向后兼容 shim）。

工具实现已拆成技能包（skills/ 目录），本文件只做转发，
保持旧导入路径不破坏（main.py / chat_controller.py 仍可直接 import dania_tools）。

新增工具请去 bot/skills/ 下加一个技能包（一个 .py 文件 + @tool 声明）。
"""
import importlib

# 触发全部技能包加载（__init__ 末尾 load_skills()）
import skills as _skills_pkg  # noqa: F401

# ── 提醒 API 转发（chat_controller.py / main.py 直接 import dania_tools 取这些） ──
from skills import set_reminder_callback, get_active_reminders, fire_reminder_for_test
from skills import _reminder_callback  # noqa: F401

# 暴露 _active_reminders 给 main.py（getattr(dania_tools, "_active_reminders", [])）
import skills.reminder as _rem

_active_reminders = _rem._active_reminders


def load_skills():
    """手动触发重新扫描技能包（去重，幂等）"""
    return _skills_pkg.load_skills()
