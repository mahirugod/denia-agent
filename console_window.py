#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅控制台窗口
- 用 QWebEngineView 嵌入 Web 原型
- 通过 QWebChannel 实现 Python <-> JS 双向通信
- 对接聊天、记忆、任务、服务状态、设置
"""
import os
import json

from PySide6.QtCore import QObject, Slot, Signal, QUrl, Qt, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebChannel import QWebChannel


class DebugPage(QWebEnginePage):
    """捕获 JS console 输出，便于排查 QWebChannel 问题"""
    def javaScriptConsoleMessage(self, level, message, line, source):
        tag = {0: "JS DEBUG", 1: "JS LOG", 2: "JS WARN", 3: "JS ERROR"}.get(level, "JS")
        print(f"[{tag}] {message}  ({source}:{line})")


class BridgeObject(QObject):
    """暴露给 JS 的桥接对象"""

    # → JS 的信号
    chat_started = Signal()            # 流式开始
    chat_delta = Signal(str)           # 流式增量
    chat_finished = Signal(str)        # 流式结束（带完整文本）
    chat_failed = Signal(str)          # 出错
    service_updated = Signal(str)      # 服务状态更新（JSON 字符串）
    tool_event = Signal(str)           # 工具执行事件（JSON，协作流）
    log = Signal(str)                  # 日志转发
    win_state_changed = Signal(bool)   # 最大化状态变化
    reminder_fired = Signal(str)       # 定时提醒到点（提醒内容）

    def __init__(self, app):
        super().__init__()
        self.app = app  # DaniaApp 实例
        self.win = None  # ConsoleWindow 引用，由窗口创建后设置

    # ===== 聊天 =====
    @Slot(str)
    def send_message(self, text: str):
        """JS 调用：发送聊天消息"""
        text = text.strip()
        if not text:
            return
        self.app.console_send_message(text)

    @Slot()
    def interrupt_voice(self):
        """JS 调用：打断语音"""
        self.app._on_voice_interrupt()

    @Slot(str)
    def replay_voice(self, text: str):
        """JS 调用：重播某条回复的语音"""
        try:
            self.app.controller.replay_voice(text)
        except Exception:
            pass

    @Slot(str)
    def copy_text(self, text: str):
        """JS 调用：复制文本到剪贴板（QWebEngine 内 navigator.clipboard 可能无权限，走 Qt 剪贴板）"""
        try:
            QApplication.clipboard().setText(text or "")
        except Exception:
            pass

    # ===== 记忆 =====
    @Slot(result=str)
    def get_memories(self) -> str:
        """JS 调用：获取记忆列表（JSON）"""
        try:
            memories = self.app.get_memories()
            return json.dumps(memories, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ===== 任务/提醒 =====
    @Slot(result=str)
    def get_tasks(self) -> str:
        """JS 调用：获取任务列表（JSON）"""
        try:
            tasks = self.app.get_tasks()
            return json.dumps(tasks, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ===== 服务状态 =====
    @Slot(result=str)
    def get_services(self) -> str:
        """JS 调用：获取服务状态（JSON）"""
        try:
            services = self.app.get_services()
            return json.dumps(services, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @Slot(str)
    def start_service(self, name: str):
        """JS 调用：启动服务（napcat 连带启动 bot）"""
        self.app.process_mgr.start_service(name)
        if name == "napcat":
            import threading, time
            def _start_bot():
                time.sleep(3)  # 等待 NapCat WebSocket 就绪
                self.app.process_mgr.start_service("bot")
            threading.Thread(target=_start_bot, daemon=True).start()

    @Slot(str)
    def stop_service(self, name: str):
        """JS 调用：停止服务（napcat 连带停止 bot）"""
        if name == "napcat":
            self.app.process_mgr.stop_service("bot")
        self.app.process_mgr.stop_service(name)

    @Slot()
    def start_all_services(self):
        skip = [] if self.app.napcat_enabled() else ["napcat", "bot"]
        self.app.process_mgr.start_all(skip=skip)

    @Slot()
    def stop_all_services(self):
        self.app.process_mgr.stop_all()

    # ===== 设置 =====
    @Slot(result=str)
    def get_settings(self) -> str:
        """JS 调用：获取设置（JSON）"""
        try:
            settings = self.app.get_settings()
            return json.dumps(settings, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @Slot(str, str)
    def save_setting(self, key: str, value: str):
        """JS 调用：保存设置"""
        self.app.save_setting(key, value)

    # ===== 无边框窗口控制 =====
    @Slot()
    def minimize_window(self):
        if self.win:
            self.win.showMinimized()

    @Slot()
    def toggle_max_window(self):
        if self.win:
            if self.win.isMaximized():
                self.win.showNormal()
            else:
                self.win.showMaximized()

    @Slot(result=bool)
    def is_maximized(self) -> bool:
        """JS 调用：查询当前是否最大化"""
        return bool(self.win and self.win.isMaximized())

    @Slot()
    def close_window(self):
        if self.win:
            self.win.close()  # closeEvent 里隐藏而非销毁

    @Slot()
    def drag_window(self):
        """JS 调用：按下顶栏后开始系统级拖动"""
        if self.win and self.win.windowHandle():
            self.win.windowHandle().startSystemMove()

    @Slot(str)
    def resize_window(self, edge: str):
        """JS 调用：按下窗口边缘后开始系统级拉伸"""
        edges = {
            "n": Qt.Edge.TopEdge, "s": Qt.Edge.BottomEdge,
            "e": Qt.Edge.RightEdge, "w": Qt.Edge.LeftEdge,
            "ne": Qt.Edge.TopEdge | Qt.Edge.RightEdge,
            "nw": Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
            "se": Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
            "sw": Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
        }.get(edge)
        if edges is not None and self.win and self.win.windowHandle():
            self.win.windowHandle().startSystemResize(edges)


class ConsoleWindow(QWidget):
    """控制台窗口：嵌入 Web 原型"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("达妮娅 · 陪伴系统")
        self.resize(1080, 720)
        self.setMinimumSize(860, 600)

        # 无边框（配合网页顶栏的拖动区与窗口控制按钮）
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)

        # 桥接对象
        self.bridge = BridgeObject(app)
        self.bridge.win = self

        # WebChannel
        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)

        # WebEngineView
        self.view = QWebEngineView()
        self.view.setPage(DebugPage(self.view))
        self.view.page().setWebChannel(self.channel)
        # 页面底色设为深色，消除默认白底在窗口边缘/加载时露出的白边
        self.view.page().setBackgroundColor(QColor("#020814"))
        self.view.setStyleSheet("border: none; background: #020814;")

        # 加载本地 HTML
        html_path = os.path.join(os.path.dirname(__file__), "console", "index.html")
        self.view.setUrl(QUrl.fromLocalFile(os.path.abspath(html_path)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view)
        self.setStyleSheet("ConsoleWindow { background: #020814; }")

        # 转发服务状态变化
        self.app.process_mgr.status_changed.connect(self._on_service_status)

    def _on_service_status(self, service: str, state: str):
        """服务状态变化 → 推给 JS"""
        data = json.dumps({"service": service, "state": state}, ensure_ascii=False)
        self.bridge.service_updated.emit(data)

    def changeEvent(self, event):
        """最大化状态变化 → 推给 JS 更新按钮图标"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.bridge.win_state_changed.emit(self.isMaximized())

    def closeEvent(self, event):
        """关闭时隐藏而非销毁（保持 WebEngine 状态）"""
        event.ignore()
        self.hide()
