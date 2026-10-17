#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具护栏（Tool Guard）—— 借鉴 Open Interpreter 的 SafetyCheck 思想，做轻量版。

三级策略：
  - AUTO   自动放行（查询类、纯本地只读）
  - ASK    高危前弹确认框（启动子进程、写文件、改系统状态）
  - REJECT 直接拒绝（写白名单外路径等）

工具结果统一截断到 harness 档位长度，避免 3B/7B 模型被长结果带偏。

用法：
    from tool_guard import ToolGuard
    guard = ToolGuard(harness, allow_confirm=True)
    if not guard.check(info, query):      # False = 被拒/被取消
        ...
    text = guard.truncate(info, result)
"""
import os
import re
from typing import Optional


class _Decision:
    def __init__(self, allow: bool, reason: str = ""):
        self.allow = allow
        self.reason = reason


class ToolGuard:
    """每个 DeniaLLM 实例持有独立的一份"""

    def __init__(self, harness, allow_confirm: bool = True):
        self.harness = harness
        self.allow_confirm = allow_confirm
        # 高危工具名 → 判定函数 (info, query) -> _Decision
        self._high_risk = {
            "open_app": self._check_open_app,
        }
        # 可写路径白名单（防符号链接逃逸，留待写文件类工具扩展）
        self._writable_roots = [
            os.path.expanduser("~"),
            os.getcwd(),
        ]

    # ---------- 三级检查 ----------

    def check(self, info, query: str) -> bool:
        """
        执行前置检查。False 表示被拒绝或用户取消。
        """
        checker = self._high_risk.get(info.name)
        if checker:
            d = checker(info, query)
            if not d.allow:
                print(f"[护栏] 拒绝: {info.name} -> {d.reason}")
                return False
            return True
        # 默认 AUTO 放行
        return True

    def _check_open_app(self, info, query: str) -> _Decision:
        """启动子进程 = 高危。harness.needs_confirmation 控制是否弹窗。"""
        if self.harness.needs_confirmation and self.allow_confirm:
            q = query.strip()
            if not self._confirm(f"达妮娅要帮你打开「{q[:20]}」，确认吗？",
                                 "确认打开"):
                return _Decision(False, "用户取消")
        return _Decision(True)

    def _confirm(self, title: str, text: str) -> bool:
        """
        弹窗确认（QMessageBox）。调用方需保证在 GUI 线程执行；
        若 PySide6 不可用则保守放行，避免卡死流程。
        """
        try:
            from PySide6.QtWidgets import QMessageBox
        except ImportError:
            print("[护栏] PySide6 不可用，跳过确认弹窗")
            return True

        box = QMessageBox()
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        ret = box.exec()
        return ret == QMessageBox.Yes

    # ---------- 结果截断 ----------

    def truncate(self, info, result: Optional[str]) -> Optional[str]:
        """工具结果按 harness 截断，保留头尾语义。"""
        if not result:
            return None
        limit = self.harness.max_result_chars
        if len(result) <= limit:
            return result
        # 保留前 70% 和后 20%（工具返回都是"前置摘要 + 数据"）
        head = int(limit * 0.7)
        tail = limit - head
        return result[:head] + f"\n…（已截断 {len(result) - limit} 字）\n" + result[-tail:]

    # ---------- 可写路径约束（留接口） ----------

    def is_writable_path(self, path: str) -> bool:
        """检查路径是否在白名单根下（防符号链接逃逸需 os.path.realpath）"""
        real = os.path.realpath(os.path.expanduser(path))
        for root in self._writable_roots:
            root_real = os.path.realpath(os.path.expanduser(root))
            if real.startswith(root_real):
                return True
        return False
