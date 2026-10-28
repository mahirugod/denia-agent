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
import threading
from typing import Optional


# ── 确认弹窗的主线程派发 ─────────────────────────────────────────
# Qt 的 QWidget 只能在 GUI 线程创建。QQ 消息、桌面消息队列都在工作线程里跑，
# 所以确认框必须通过「信号 + 队列连接」丢回主线程，再用 Event 等结果。
_CONFIRM_TIMEOUT = 120  # 等用户点按钮的最长时间（秒），超时按「取消」处理
_RELAY = {"obj": None, "lock": threading.Lock()}

# 结果截断的按工具覆盖：读书类工具要保完整正文，不能按 harness 档位砍
_RESULT_LIMIT_OVERRIDE = {
    "read_book_chunk": 7000,   # 单块正文（含 6000 字上限 + 头尾说明）
    "continue_reading": 14000,  # 一次最多 5 块
    "note_book_chunk": 3000,   # 笔记回执
}


def _get_relay():
    """懒创建派发对象，并把它移到 GUI 线程（只能在这里的 slot 里建控件）"""
    with _RELAY["lock"]:
        obj = _RELAY["obj"]
        if obj is not None:
            return obj
        from PySide6.QtCore import QObject, Signal, Slot, Qt
        from PySide6.QtWidgets import QApplication, QMessageBox

        class _ConfirmRelay(QObject):
            request = Signal(str, str, object)

            def __init__(self):
                super().__init__()
                # 队列连接：无论从哪个线程 emit，slot 都在 GUI 线程执行
                self.request.connect(self._show, Qt.QueuedConnection)

            @Slot(str, str, object)
            def _show(self, title: str, text: str, holder: dict):
                try:
                    box = QMessageBox()
                    box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
                    box.setIcon(QMessageBox.Question)
                    box.setWindowTitle(title)
                    box.setText(text)
                    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                    box.setDefaultButton(QMessageBox.No)
                    holder["ok"] = box.exec() == QMessageBox.Yes
                except Exception as e:
                    print(f"[护栏] 确认弹窗失败: {e}")
                    holder["ok"] = False
                finally:
                    holder["ev"].set()

        obj = _ConfirmRelay()
        app = QApplication.instance()
        if app is not None and obj.thread() is not app.thread():
            obj.moveToThread(app.thread())
        _RELAY["obj"] = obj
        return obj


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
            "list_dir": self._check_file_tool,
            "read_file": self._check_file_tool,
            "write_file": self._check_file_tool,
            "organize_folder": self._check_file_tool,
        }
        # 可写路径白名单（防符号链接逃逸，留待写文件类工具扩展）
        self._writable_roots = [
            os.path.expanduser("~"),
            os.getcwd(),
        ]
        # 桌面/下载/文档 可能被用户重定向到别的盘，显式补进来
        for _sub in ("Desktop", "Downloads", "Documents"):
            _p = os.path.expanduser(f"~/{_sub}")
            if _p not in self._writable_roots:
                self._writable_roots.append(_p)

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

    def _check_file_tool(self, info, query: str) -> _Decision:
        """文件类工具 = 有副作用。不做弹窗，只做路径白名单校验（用户已选“限制范围直接执行”）。
        解析不到目标路径时放行，交给工具自身再判一次。"""
        target = None
        try:
            import file_ops  # skills/ 已由 load_skills 加入 sys.path
            target = file_ops.resolve_any(query)
        except Exception:
            target = None
        if not target:
            return _Decision(True)
        if not self.is_writable_path(target):
            return _Decision(False, f"路径不在白名单: {target}")
        return _Decision(True)

    def _confirm(self, title: str, text: str) -> bool:
        """
        弹窗确认（QMessageBox）。
        在 GUI 线程里直接弹；在工作线程里通过信号派发回 GUI 线程再等结果，
        避免「QWidget 必须在主线程创建」导致的崩溃/假死。
        无 QApplication（如 QQ 子进程）时保守放行，不阻塞流程。
        """
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            print("[护栏] PySide6 不可用，跳过确认弹窗")
            return True

        app = QApplication.instance()
        if app is None:
            print("[护栏] 无 QApplication，跳过确认弹窗")
            return True

        try:
            from PySide6.QtCore import QThread

            on_gui_thread = QThread.currentThread() is app.thread()
        except Exception:
            on_gui_thread = False

        if on_gui_thread:
            return self._ask_box(title, text)

        # 工作线程：派发回主线程弹窗，同步等用户选择
        holder = {"ok": False, "ev": threading.Event()}
        try:
            relay = _get_relay()
            relay.request.emit(title, text, holder)
        except Exception as e:
            print(f"[护栏] 派发确认弹窗失败: {e}")
            return False
        if not holder["ev"].wait(_CONFIRM_TIMEOUT):
            print(f"[护栏] 确认弹窗超时（{_CONFIRM_TIMEOUT}s），按取消处理")
            return False
        return bool(holder["ok"])

    @staticmethod
    def _ask_box(title: str, text: str) -> bool:
        """真正创建并显示确认框（必须在 GUI 线程调用）"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox()
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    # ---------- 结果截断 ----------

    def truncate(self, info, result: Optional[str]) -> Optional[str]:
        """工具结果按 harness 截断，保留头尾语义。读书类工具走单独上限，不砍正文。"""
        if not result:
            return None
        limit = _RESULT_LIMIT_OVERRIDE.get(
            getattr(info, "name", ""), self.harness.max_result_chars
        )
        if len(result) <= limit:
            return result
        # 保留前 70% 和后 20%（工具返回都是"前置摘要 + 数据"）
        head = int(limit * 0.7)
        tail = limit - head
        return result[:head] + f"\n…（已截断 {len(result) - limit} 字）\n" + result[-tail:]

    # ---------- 可写路径约束（留接口） ----------

    def is_writable_path(self, path: str) -> bool:
        """检查路径是否在白名单根下（防符号链接逃逸需 os.path.realpath）"""
        try:
            import file_ops  # skills/ 已由 load_skills 加入 sys.path
            if file_ops.is_sensitive_path(path):
                return False
        except Exception:
            pass
        real = os.path.realpath(os.path.expanduser(path))
        for root in self._writable_roots:
            root_real = os.path.realpath(os.path.expanduser(root)).rstrip("\\/")
            # 必须按目录边界比较，否则 "C:\Users\user2" 会被判成在 "C:\Users\user" 之内
            if real == root_real or real.startswith(root_real + os.sep):
                return True
        return False
