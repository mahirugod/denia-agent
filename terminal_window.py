#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅终端 - 主窗口
深色毛玻璃主题，侧边导航，圆形立绘头像，流式对话
"""
import os
import re
import math
from datetime import datetime

from PySide6.QtCore import (
    Qt, QTimer, QObject, Signal, QRect, QPoint, QSize, QPropertyAnimation,
    QEasingCurve, QThread,
)
from PySide6.QtGui import (
    QPainter, QPixmap, QColor, QFont, QFontMetrics, QBrush, QPen, QLinearGradient,
    QRadialGradient, QPainterPath, QIcon, QCursor,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QScrollArea, QFrame, QStackedWidget, QSizePolicy, QTextEdit,
    QGraphicsDropShadowEffect, QSpacerItem, QListWidget, QListWidgetItem,
    QSlider, QCheckBox, QGroupBox, QGridLayout, QProgressBar, QMenu,
    QSystemTrayIcon, QApplication,
)

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


# ============================================================
# 圆形头像组件（带呼吸光晕）
# ============================================================
class AvatarLabel(QLabel):
    """圆形头像：立绘裁切为圆形，带呼吸光晕环"""

    def __init__(self, image_path: str, size: int = 160, parent=None):
        super().__init__(parent)
        self._size = size
        self._pixmap = QPixmap(image_path)
        self._glow_phase = 0.0
        self.setFixedSize(size + 24, size + 24)
        # 呼吸动画
        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(40)
        self._glow_timer.timeout.connect(self._on_glow)
        self._glow_timer.start()

    def set_image(self, image_path: str):
        self._pixmap = QPixmap(image_path)
        self.update()

    def _on_glow(self):
        self._glow_phase += 0.05
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        cx = self.width() / 2
        cy = self.height() / 2
        r = self._size / 2

        # 呼吸光晕（青+品红霓虹）
        glow_r = r + 10 + math.sin(self._glow_phase) * 4
        grad = QRadialGradient(cx, cy, glow_r)
        grad.setColorAt(0.0, QColor(0, 245, 255, 80))
        grad.setColorAt(0.5, QColor(255, 45, 163, 50))
        grad.setColorAt(1.0, QColor(0, 245, 255, 0))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(glow_r), int(glow_r))

        # 圆形边框（青色发光）
        border = QPen(QColor(0, 245, 255, 200), 2.5)
        painter.setPen(border)
        painter.setBrush(QBrush(QColor(2, 8, 20, 220)))
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(r), int(r))

        # 裁切立绘为圆形
        if not self._pixmap.isNull():
            # 等比缩放填满
            scaled = self._pixmap.scaled(
                int(r * 2), int(r * 2),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            # 居中裁切
            ox = (scaled.width() - int(r * 2)) // 2
            oy = (scaled.height() - int(r * 2)) // 2
            cropped = scaled.copy(ox, oy, int(r * 2), int(r * 2))

            # 圆形 mask
            path = QPainterPath()
            path.addEllipse(QPoint(int(cx), int(cy)), int(r - 1), int(r - 1))
            painter.setClipPath(path)
            painter.drawPixmap(int(cx - r), int(cy - r), cropped)
            painter.setClipPath(QPainterPath())

        painter.end()


# ============================================================
# 聊天气泡（用户 / 达妮娅）
# ============================================================
class ChatBubble(QLabel):
    """自适应聊天气泡，支持打字机效果"""

    def __init__(self, text: str = "", is_user: bool = False, parent=None):
        super().__init__(parent)
        self._is_user = is_user
        self._full_text = text
        self._shown_text = ""
        self._typing_active = False
        self._typewriter_index = 0
        self._typewriter_timer = QTimer(self)
        self._typewriter_timer.setInterval(35)
        self._typewriter_timer.timeout.connect(self._typewriter_tick)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._apply_style()

    def _apply_style(self):
        if self._is_user:
            self.setStyleSheet("""
                QLabel {
                    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 rgba(255,45,163,0.85), stop:1 rgba(113,36,255,0.82));
                    color: #ffffff;
                    border-radius: 14px;
                    padding: 10px 14px;
                    font-size: 14px;
                    font-family: "Microsoft YaHei UI", "微软雅黑", sans-serif;
                    border: 1px solid rgba(255, 155, 211, 0.6);
                }
            """)
        else:
            self.setStyleSheet("""
                QLabel {
                    background-color: rgba(3, 12, 28, 0.92);
                    color: #e9fbff;
                    border-radius: 14px;
                    padding: 10px 14px;
                    font-size: 14px;
                    font-family: "Microsoft YaHei UI", "微软雅黑", sans-serif;
                    border: 1px solid rgba(0, 245, 255, 0.3);
                }
            """)

    def set_text_with_typewriter(self, text: str):
        """设置文本并启动打字机"""
        self._full_text = text
        self._shown_text = ""
        self._typewriter_index = 0
        self._typing_active = True
        self.setText("")
        self._resize_to_content()
        self._typewriter_timer.start()

    def append_text(self, text: str):
        """流式追加文本（外部驱动，不启动计时器）"""
        self._full_text += text
        self._shown_text = self._full_text
        self.setText(self._shown_text)
        self._resize_to_content()

    def finish_typing(self):
        """立即显示全文并停止打字机"""
        self._typewriter_timer.stop()
        self._typing_active = False
        self._shown_text = self._full_text
        self.setText(self._shown_text)
        self._resize_to_content()

    def _typewriter_tick(self):
        if self._typewriter_index < len(self._full_text):
            self._typewriter_index += 1
            self._shown_text = self._full_text[:self._typewriter_index]
            self.setText(self._shown_text)
            self._resize_to_content()
        else:
            self._typewriter_timer.stop()
            self._typing_active = False

    def _resize_to_content(self):
        """根据文本内容计算固定尺寸，避免 adjustSize 导致竖排"""
        max_w = 360
        text = self._shown_text or " "
        # 与样式表 font-size: 14px 精确匹配
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(14)
        fm = QFontMetrics(font)
        h_pad = 40  # 左14 + 右14 + 边框2 + 余量10
        v_pad = 24  # 上10 + 下10 + 边框2 + 余量2

        text_w = fm.horizontalAdvance(text)
        if text_w + h_pad <= max_w:
            # 单行
            w = text_w + h_pad
            h = fm.height() + v_pad
        else:
            # 多行：按最大宽度换行计算高度
            w = max_w
            rect = fm.boundingRect(
                QRect(0, 0, max_w - h_pad, 10000),
                Qt.TextFlag.TextWordWrap,
                text,
            )
            h = rect.height() + v_pad

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(max(w, 40), h)

    def is_typing(self) -> bool:
        return self._typing_active


# ============================================================
# 侧边导航按钮
# ============================================================
class NavButton(QPushButton):
    def __init__(self, icon_text: str, label: str, shortcut: str = "", parent=None):
        super().__init__(parent)
        self._label = label
        self._icon = icon_text
        self._shortcut = shortcut
        self.setFixedSize(56, 56)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip(f"{label}  ({shortcut})" if shortcut else label)
        self._active = False
        self._apply_style()

    def set_active(self, active: bool):
        self._active = active
        self._apply_style()

    def _apply_style(self):
        if self._active:
            self.setStyleSheet("""
                QPushButton {
                    background-color: rgba(0, 245, 255, 0.18);
                    border: 1px solid rgba(0, 245, 255, 0.7);
                    border-radius: 12px;
                    color: #00f5ff;
                    font-size: 22px;
                }
                QPushButton:hover { background-color: rgba(0, 245, 255, 0.28); }
            """)
        else:
            self.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    border: 1px solid transparent;
                    border-radius: 12px;
                    color: #5a7a8a;
                    font-size: 22px;
                }
                QPushButton:hover {
                    background-color: rgba(255, 45, 163, 0.18);
                    color: #ff9bd3;
                }
            """)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 绘制 emoji 图标
        painter.setPen(QColor("#00f5ff" if self._active else "#5a7a8a"))
        font = QFont()
        font.setPointSize(18)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._icon)
        painter.end()


# ============================================================
# 状态卡片（当前任务 / 最近记忆）
# ============================================================
class StatusCard(QFrame):
    def __init__(self, title: str, icon: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: rgba(3, 10, 24, 0.7);
                border: 1px solid rgba(0, 245, 255, 0.2);
                border-radius: 14px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        header = QHBoxLayout()
        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet("font-size: 16px;")
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("color: #a9fbff; font-size: 12px; font-weight: bold;")
        header.addWidget(icon_lbl)
        header.addWidget(title_lbl)
        header.addStretch()
        layout.addLayout(header)

        self.content_lbl = QLabel("暂无")
        self.content_lbl.setStyleSheet("color: #e9fbff; font-size: 13px;")
        self.content_lbl.setWordWrap(True)
        layout.addWidget(self.content_lbl)

    def set_content(self, text: str):
        self.content_lbl.setText(text)


# ============================================================
# 自绘顶栏（无边框窗口）
# ============================================================
class TitleBar(QFrame):
    """霓虹自绘标题栏：logo + 标题 + 在线状态 + 最小化/关闭，可拖动"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window
        self.setFixedHeight(42)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setStyleSheet("""
            QFrame {
                background-color: rgba(1, 6, 16, 0.96);
                border-bottom: 1px solid rgba(0, 245, 255, 0.25);
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(12)

        logo = QLabel("🌙")
        logo.setStyleSheet("font-size: 18px;")
        layout.addWidget(logo)

        title = QLabel("DANIA AGENT · 达妮娅终端")
        title.setStyleSheet("color: #00f5ff; font-size: 13px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(title)
        layout.addStretch()

        self.status = QLabel("● 在线")
        self.status.setStyleSheet("color: #7fff9a; font-size: 12px;")
        layout.addWidget(self.status)

        self.min_btn = QPushButton("—")
        self.min_btn.setFixedSize(30, 24)
        self.min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.min_btn.setStyleSheet("""
            QPushButton { background: transparent; border: 1px solid rgba(0,245,255,0.4);
                border-radius: 6px; color: #00f5ff; font-size: 13px; }
            QPushButton:hover { background: rgba(0,245,255,0.18); }
        """)
        self.min_btn.clicked.connect(self._win.showMinimized)
        layout.addWidget(self.min_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(30, 24)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet("""
            QPushButton { background: transparent; border: 1px solid rgba(255,92,92,0.5);
                border-radius: 6px; color: #ff8a8a; font-size: 13px; }
            QPushButton:hover { background: rgba(255,92,92,0.25); }
        """)
        self.close_btn.clicked.connect(self._win.close)
        layout.addWidget(self.close_btn)

    def set_online(self, online: bool):
        self.status.setText("● 在线" if online else "● 离线")
        self.status.setStyleSheet(
            "color: #7fff9a; font-size: 12px;" if online else "color: #ff7a7a; font-size: 12px;"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if hasattr(self, "_drag_pos"):
            self._win.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        if hasattr(self, "_drag_pos"):
            del self._drag_pos
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()

    def enterEvent(self, event):
        pass


# ============================================================
# 主终端窗口
# ============================================================
class TerminalWindow(QMainWindow):

    # 对外信号
    chat_message_sent = Signal(str)          # 用户发送文本
    voice_input_requested = Signal()          # 请求语音输入
    history_requested = Signal()              # 请求历史
    backtrack_requested = Signal(int)         # 回溯历史
    service_start_requested = Signal()
    service_stop_requested = Signal()
    service_start_single = Signal(str)       # 单个服务启动 (内部键名)
    service_stop_single = Signal(str)        # 单个服务停止 (内部键名)
    minimize_to_pet = Signal()                # 最小化为悬浮小人
    open_console_requested = Signal()         # 打开 Web 控制台
    settings_save_requested = Signal()        # 请求保存设置

    # 内部控制信号（打字机/流式）
    _typing_finished_internal = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DANIA AGENT  达妮娅终端")
        # 无边框
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(920, 600)
        self.resize(1000, 680)
        self._setup_ui()
        self._setup_tray()
        self._apply_background()

        # 打字机状态
        self._current_bot_bubble = None
        self._stream_active = False
        self._stream_raw = ""
        self._typewriter_full = ""
        self._typewriter_index = 0
        self._typing_active = False
        self._typewriter_timer = QTimer(self)
        self._typewriter_timer.setInterval(40)
        self._typewriter_timer.timeout.connect(self._typewriter_tick)

        # 情绪→立绘映射
        self._state_images = {
            "idle": os.path.join(ASSETS_DIR, "dania_idle.png"),
            "happy": os.path.join(ASSETS_DIR, "dania_happy.png"),
            "sleeping": os.path.join(ASSETS_DIR, "dania_sleeping.png"),
            "speaking": os.path.join(ASSETS_DIR, "dania_speaking.png"),
            "thinking": os.path.join(ASSETS_DIR, "dania_thinking.png"),
        }

    # ---------- UI 构建 ----------
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 自绘顶栏（无边框窗口）
        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)

        # 主体：侧边导航 + 主内容区
        body = QWidget()
        root = QHBoxLayout(body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        outer.addWidget(body, 1)

        # 侧边导航
        self.sidebar = self._build_sidebar()
        root.addWidget(self.sidebar)

        # 主内容区（堆叠面板）
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_home_panel())    # 0 首页（含对话）
        self.stack.addWidget(self._build_memory_panel())  # 1 记忆
        self.stack.addWidget(self._build_task_panel())    # 2 任务
        self.stack.addWidget(self._build_agent_panel())   # 3 智能体
        self.stack.addWidget(self._build_settings_panel())# 4 设置
        root.addWidget(self.stack, 1)

        # 侧边栏在 stack 之后激活（避免 stack 未定义）
        self._switch_panel(0)

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedWidth(72)
        bar.setStyleSheet("""
            QFrame {
                background-color: rgba(1, 6, 16, 0.92);
                border-right: 1px solid rgba(0, 245, 255, 0.15);
            }
        """)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(8, 16, 8, 16)
        layout.setSpacing(8)

        # Logo
        logo = QLabel("🌙")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("font-size: 26px;")
        logo.setFixedHeight(40)
        layout.addWidget(logo)
        layout.addSpacing(10)

        # 导航按钮
        self.nav_buttons = []
        navs = [
            ("🏠", "首页", "H"),
            ("🧠", "记忆", "M"),
            ("📋", "任务", "T"),
            ("🤖", "智能体", "A"),
            ("⚙️", "设置", "S"),
        ]
        for i, (icon, label, sc) in enumerate(navs):
            btn = NavButton(icon, label, sc)
            btn.clicked.connect(lambda checked=False, idx=i: self._switch_panel(idx))
            self.nav_buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch()

        # 最小化为小人
        pet_btn = NavButton("🐾", "悬浮小人", "P")
        pet_btn.clicked.connect(self.minimize_to_pet.emit)
        layout.addWidget(pet_btn)

        return bar

    def _switch_panel(self, idx: int):
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self.nav_buttons):
            btn.set_active(i == idx)

    # ---------- 首页 ----------
    def _build_home_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 12, 24, 16)
        layout.setSpacing(10)

        # 头像 + 名字 + 问候（紧凑排列）
        header = QHBoxLayout()
        header.setSpacing(12)
        self.avatar = AvatarLabel(os.path.join(ASSETS_DIR, "dania_idle.png"), size=72)
        header.addWidget(self.avatar)
        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        name_lbl = QLabel("达妮娅")
        name_lbl.setStyleSheet("color: #ff63bb; font-size: 16px; font-weight: bold;")
        info_col.addWidget(name_lbl)
        self.greeting_lbl = QLabel(self._get_greeting())
        self.greeting_lbl.setStyleSheet("color: #a9fbff; font-size: 12px;")
        info_col.addWidget(self.greeting_lbl)
        header.addLayout(info_col)
        header.addStretch()
        layout.addLayout(header)

        # 聊天滚动区（首页主对话区）
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 6px; }
            QScrollBar::handle:vertical {
                background: rgba(0, 245, 255, 0.3); border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover { background: rgba(0, 245, 255, 0.5); }
        """)
        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(4, 4, 4, 4)
        self.chat_layout.setSpacing(12)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_container)
        layout.addWidget(self.chat_scroll, 1)

        # 输入区
        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("和达妮娅说点什么…  (Enter 发送)")
        self.input_box.setFixedHeight(44)
        self.input_box.setStyleSheet("""
            QLineEdit {
                background-color: rgba(2, 8, 20, 0.96);
                border: 1px solid rgba(0, 245, 255, 0.35);
                border-radius: 22px;
                padding: 0 20px;
                color: #e9fbff;
                font-size: 14px;
                selection-background-color: rgba(0, 245, 255, 0.3);
            }
            QLineEdit:focus {
                border: 1px solid rgba(0, 245, 255, 0.8);
            }
        """)
        self.input_box.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_box, 1)

        self.mic_btn = QPushButton("🎤")
        self.mic_btn.setFixedSize(44, 44)
        self.mic_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.mic_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(0, 245, 255, 0.12);
                border: 1px solid rgba(0, 245, 255, 0.5);
                border-radius: 22px;
                font-size: 18px;
            }
            QPushButton:hover { background-color: rgba(0, 245, 255, 0.25); }
            QPushButton:pressed { background-color: rgba(0, 245, 255, 0.35); }
        """)
        self.mic_btn.clicked.connect(self.voice_input_requested.emit)
        input_row.addWidget(self.mic_btn)

        self.send_btn = QPushButton("➤")
        self.send_btn.setFixedSize(44, 44)
        self.send_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.send_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255, 45, 163, 0.15);
                border: 1px solid rgba(255, 99, 187, 0.7);
                border-radius: 22px;
                font-size: 18px;
                color: #ff9bd3;
            }
            QPushButton:hover { background-color: rgba(255, 45, 163, 0.3); }
        """)
        self.send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self.send_btn)

        layout.addLayout(input_row)

        # 状态卡片（紧凑）
        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)
        self.task_card = StatusCard("当前任务", "📋")
        self.memory_card = StatusCard("最近记忆", "🧠")
        cards_row.addWidget(self.task_card, 1)
        cards_row.addWidget(self.memory_card, 1)
        layout.addLayout(cards_row)

        layout.addStretch()
        return panel

    def _get_greeting(self) -> str:
        h = datetime.now().hour
        if h < 6:
            return "深夜了…还不睡吗？"
        elif h < 11:
            return "早安~ 今天也要元气满满哦"
        elif h < 13:
            return "中午好~ 吃饭了吗？"
        elif h < 18:
            return "下午好~ 今天想一起做什么？"
        elif h < 22:
            return "晚上好~ 累了吧？"
        else:
            return "夜深了…早点休息哦"

    # ---------- 对话面板 ----------
    def _build_chat_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # 标题
        title = QLabel("💬 对话")
        title.setStyleSheet("color: #00f5ff; font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        # 消息滚动区
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setStyleSheet("""
            QScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
            }
            QScrollBar::handle:vertical {
                background: rgba(0, 245, 255, 0.3);
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(0, 245, 255, 0.5);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(8, 8, 8, 8)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_container)
        layout.addWidget(self.chat_scroll, 1)

        # 底部输入
        input_row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("输入消息…  (Enter 发送)")
        self.chat_input.setFixedHeight(42)
        self.chat_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(2, 8, 20, 0.96);
                border: 1px solid rgba(0, 245, 255, 0.35);
                border-radius: 21px;
                padding: 0 18px;
                color: #e9fbff;
                font-size: 14px;
            }
            QLineEdit:focus { border: 1px solid rgba(0, 245, 255, 0.8); }
        """)
        self.chat_input.returnPressed.connect(self._on_send_chat)
        input_row.addWidget(self.chat_input, 1)

        send = QPushButton("➤")
        send.setFixedSize(42, 42)
        send.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        send.setStyleSheet("""
            QPushButton {
                background-color: rgba(255, 45, 163, 0.15);
                border: 1px solid rgba(255, 99, 187, 0.7);
                border-radius: 21px;
                color: #ff9bd3; font-size: 16px;
            }
            QPushButton:hover { background-color: rgba(255, 45, 163, 0.3); }
        """)
        send.clicked.connect(self._on_send_chat)
        input_row.addWidget(send)
        layout.addLayout(input_row)

        return panel

    # ---------- 记忆面板 ----------
    def _build_memory_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("🧠 记忆")
        title.setStyleSheet("color: #00f5ff; font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("🔄 刷新历史")
        refresh_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        refresh_btn.setStyleSheet(self._btn_style())
        refresh_btn.clicked.connect(self.history_requested.emit)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.history_list = QListWidget()
        self.history_list.setStyleSheet("""
            QListWidget {
                background-color: rgba(2, 8, 20, 0.7);
                border: 1px solid rgba(0, 245, 255, 0.2);
                border-radius: 10px;
                color: #e9fbff;
                font-size: 13px;
                padding: 6px;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid rgba(0, 245, 255, 0.1);
            }
            QListWidget::item:selected {
                background-color: rgba(0, 245, 255, 0.15);
            }
        """)
        layout.addWidget(self.history_list, 1)

        # 回溯控制
        back_row = QHBoxLayout()
        back_row.addWidget(QLabel("回溯到第"))
        self.backtrack_spin = QLineEdit("0")
        self.backtrack_spin.setFixedWidth(60)
        self.backtrack_spin.setStyleSheet("""
            QLineEdit {
                background-color: rgba(2, 8, 20, 0.96);
                border: 1px solid rgba(0, 245, 255, 0.35);
                border-radius: 6px;
                padding: 4px 8px;
                color: #e9fbff;
            }
        """)
        back_row.addWidget(self.backtrack_spin)
        back_row.addWidget(QLabel("条记录"))
        back_btn = QPushButton("🕘 回溯")
        back_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        back_btn.setStyleSheet(self._btn_style())
        back_btn.clicked.connect(self._on_backtrack)
        back_row.addWidget(back_btn)
        back_row.addStretch()
        layout.addLayout(back_row)

        return panel

    def _on_backtrack(self):
        try:
            keep = int(self.backtrack_spin.text())
            self.backtrack_requested.emit(keep)
        except ValueError:
            pass

    # ---------- 任务面板 ----------
    def _build_task_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("📋 任务 / 提醒")
        title.setStyleSheet("color: #00f5ff; font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        tip = QLabel("在对话中对达妮娅说「X分钟后提醒我…」即可创建定时提醒。")
        tip.setStyleSheet("color: #a9fbff; font-size: 13px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.task_list = QListWidget()
        self.task_list.setStyleSheet("""
            QListWidget {
                background-color: rgba(2, 8, 20, 0.7);
                border: 1px solid rgba(0, 245, 255, 0.2);
                border-radius: 10px;
                color: #e9fbff;
                font-size: 13px;
                padding: 6px;
            }
            QListWidget::item { padding: 8px; }
        """)
        layout.addWidget(self.task_list, 1)

        return panel

    # ---------- 智能体面板 ----------
    def _build_agent_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("🤖 智能体状态")
        title.setStyleSheet("color: #00f5ff; font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        # 服务状态卡片 (显示名, 内部键名, 描述, 端口)
        self.service_status = {}
        services = [
            ("Ollama", "ollama", "大语言模型", 11434),
            ("NapCat + QQ 机器人", "napcat", "QQ 消息转发（含机器人）", 3000),
            ("GPT-SoVITS", "gpt_sovits", "语音合成", 9880),
        ]
        for display_name, key, desc, port in services:
            card = self._build_service_card(display_name, key, desc, port)
            layout.addWidget(card)

        layout.addSpacing(10)
        ctrl_row = QHBoxLayout()
        start_all = QPushButton("🚀 启动所有服务")
        start_all.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        start_all.setStyleSheet(self._btn_style(primary=True))
        start_all.clicked.connect(self.service_start_requested.emit)
        ctrl_row.addWidget(start_all)

        stop_all = QPushButton("⏹ 停止所有服务")
        stop_all.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        stop_all.setStyleSheet(self._btn_style())
        stop_all.clicked.connect(self.service_stop_requested.emit)
        ctrl_row.addWidget(stop_all)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        layout.addStretch()
        return panel

    def _build_service_card(self, display_name: str, key: str, desc: str, port: int) -> QFrame:
        card = QFrame()
        card.setStyleSheet("""
            QFrame {
                background-color: rgba(3, 10, 24, 0.7);
                border: 1px solid rgba(0, 245, 255, 0.2);
                border-radius: 12px;
            }
        """)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        info = QVBoxLayout()
        n = QLabel(display_name)
        n.setStyleSheet("color: #e9fbff; font-size: 14px; font-weight: bold;")
        d = QLabel(f"{desc}  ·  端口 {port}")
        d.setStyleSheet("color: #a9fbff; font-size: 12px;")
        info.addWidget(n)
        info.addWidget(d)
        lay.addLayout(info, 1)

        # 状态标签
        status = QLabel("检测中…")
        status.setStyleSheet("color: #f0c040; font-size: 12px;")
        status.setMinimumWidth(70)
        self.service_status[display_name] = status
        lay.addWidget(status)

        # 独立启停按钮
        start_btn = QPushButton("▶")
        start_btn.setFixedSize(32, 28)
        start_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        start_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(0, 245, 255, 0.12);
                border: 1px solid rgba(0, 245, 255, 0.5);
                border-radius: 6px;
                color: #00f5ff;
                font-size: 12px;
            }
            QPushButton:hover { background-color: rgba(0, 245, 255, 0.28); }
            QPushButton:pressed { background-color: rgba(0, 245, 255, 0.4); }
        """)
        start_btn.setToolTip(f"启动 {display_name}")
        start_btn.clicked.connect(lambda: self.service_start_single.emit(key))
        lay.addWidget(start_btn)

        stop_btn = QPushButton("⏹")
        stop_btn.setFixedSize(32, 28)
        stop_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        stop_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255, 92, 92, 0.12);
                border: 1px solid rgba(255, 92, 92, 0.5);
                border-radius: 6px;
                color: #ff8a8a;
                font-size: 12px;
            }
            QPushButton:hover { background-color: rgba(255, 92, 92, 0.28); }
            QPushButton:pressed { background-color: rgba(255, 92, 92, 0.4); }
        """)
        stop_btn.setToolTip(f"停止 {display_name}")
        stop_btn.clicked.connect(lambda: self.service_stop_single.emit(key))
        lay.addWidget(stop_btn)

        return card

    # ---------- 设置面板 ----------
    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("⚙️ 设置")
        title.setStyleSheet("color: #00f5ff; font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        # LLM 设置
        llm_group = QGroupBox("LLM 配置")
        llm_group.setStyleSheet("""
            QGroupBox {
                color: #a9fbff; font-size: 13px; font-weight: bold;
                border: 1px solid rgba(0, 245, 255, 0.2);
                border-radius: 10px; margin-top: 10px; padding-top: 10px;
            }
        """)
        gl = QGridLayout(llm_group)
        gl.setSpacing(8)
        gl.addWidget(QLabel("模型:"), 0, 0)
        self.llm_model = QLineEdit("qwen2.5:7b")
        self.llm_model.setStyleSheet(self._input_style())
        gl.addWidget(self.llm_model, 0, 1)
        gl.addWidget(QLabel("API 地址:"), 1, 0)
        self.llm_url = QLineEdit("http://127.0.0.1:11434/api/chat")
        self.llm_url.setStyleSheet(self._input_style())
        gl.addWidget(self.llm_url, 1, 1)
        layout.addWidget(llm_group)

        # 语音设置
        voice_group = QGroupBox("语音合成")
        voice_group.setStyleSheet(llm_group.styleSheet())
        vl = QGridLayout(voice_group)
        vl.setSpacing(8)
        vl.addWidget(QLabel("TTS 地址:"), 0, 0)
        self.tts_url = QLineEdit("http://127.0.0.1:9880")
        self.tts_url.setStyleSheet(self._input_style())
        vl.addWidget(self.tts_url, 0, 1)
        self.voice_enabled = QCheckBox("启用语音合成")
        self.voice_enabled.setChecked(True)
        self.voice_enabled.setStyleSheet("color: #e9fbff; font-size: 13px;")
        vl.addWidget(self.voice_enabled, 1, 0, 1, 2)
        layout.addWidget(voice_group)

        # 行为设置
        behav_group = QGroupBox("行为")
        behav_group.setStyleSheet(llm_group.styleSheet())
        bl = QVBoxLayout(behav_group)
        self.auto_start = QCheckBox("启动时自动检测并补齐服务")
        self.auto_start.setChecked(True)
        self.auto_start.setStyleSheet("color: #e9fbff; font-size: 13px;")
        bl.addWidget(self.auto_start)
        self.napcat_enabled = QCheckBox("启用 NapCat（QQ 消息转发，默认关闭）")
        self.napcat_enabled.setChecked(False)
        self.napcat_enabled.setStyleSheet("color: #e9fbff; font-size: 13px;")
        bl.addWidget(self.napcat_enabled)
        layout.addWidget(behav_group)

        layout.addStretch()

        save_btn = QPushButton("💾 保存设置")
        save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        save_btn.setStyleSheet(self._btn_style(primary=True))
        save_btn.clicked.connect(self._save_settings)
        layout.addWidget(save_btn)

        return panel

    def _save_settings(self):
        self.settings_save_requested.emit()
        if hasattr(self, "title_bar"):
            self.title_bar.status.setText("● 已保存")
            QTimer.singleShot(2000, lambda: self.title_bar.status.setText("● 在线"))

    # ---------- 样式辅助 ----------
    def _btn_style(self, primary: bool = False) -> str:
        if primary:
            return """
                QPushButton {
                    background-color: rgba(255, 45, 163, 0.18);
                    border: 1px solid rgba(255, 99, 187, 0.7);
                    border-radius: 8px;
                    padding: 8px 16px;
                    color: #ff9bd3;
                    font-size: 13px;
                }
                QPushButton:hover { background-color: rgba(255, 45, 163, 0.32); }
            """
        return """
            QPushButton {
                background-color: rgba(0, 245, 255, 0.08);
                border: 1px solid rgba(0, 245, 255, 0.4);
                border-radius: 8px;
                padding: 8px 16px;
                color: #a9fbff;
                font-size: 13px;
            }
            QPushButton:hover { background-color: rgba(0, 245, 255, 0.18); }
        """

    def _input_style(self) -> str:
        return """
            QLineEdit {
                background-color: rgba(2, 8, 20, 0.96);
                border: 1px solid rgba(0, 245, 255, 0.35);
                border-radius: 6px;
                padding: 6px 10px;
                color: #e9fbff;
                font-size: 13px;
            }
            QLineEdit:focus { border: 1px solid rgba(0, 245, 255, 0.8); }
        """

    # ---------- 背景 ----------
    def _apply_background(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #020814;
            }
        """)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # neon-night-city：深蓝黑 → 深紫渐变
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0.0, QColor(2, 8, 20))
        grad.setColorAt(0.5, QColor(19, 3, 27))
        grad.setColorAt(1.0, QColor(1, 5, 14))
        painter.fillRect(self.rect(), QBrush(grad))

        # 品红霓虹辉光（右下角）
        w, h = self.width(), self.height()
        glow = QRadialGradient(w * 0.85, h * 0.9, w * 0.5)
        glow.setColorAt(0.0, QColor(255, 45, 163, 18))
        glow.setColorAt(1.0, QColor(255, 45, 163, 0))
        painter.setBrush(QBrush(glow))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(self.rect())

        # 青色霓虹辉光（左上角）
        glow2 = QRadialGradient(w * 0.15, h * 0.1, w * 0.5)
        glow2.setColorAt(0.0, QColor(0, 245, 255, 12))
        glow2.setColorAt(1.0, QColor(0, 245, 255, 0))
        painter.setBrush(QBrush(glow2))
        painter.drawRect(self.rect())

        painter.end()

    # ---------- 托盘 ----------
    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        tray_path = os.path.join(ASSETS_DIR, "tray_dania.png")
        if os.path.exists(tray_path):
            self.tray_icon.setIcon(QIcon(tray_path))
        self.tray_icon.setToolTip("达妮娅终端")
        menu = QMenu()
        console_act = menu.addAction("🖥️ 打开控制台")
        console_act.triggered.connect(self.open_console_requested.emit)
        pet_act = menu.addAction("🐾 悬浮小人")
        pet_act.triggered.connect(self.minimize_to_pet.emit)
        menu.addSeparator()
        show_act = menu.addAction("🔓 显示原始终端")
        show_act.triggered.connect(self.showNormal)
        menu.addSeparator()
        quit_act = menu.addAction("❌ 退出")
        quit_act.triggered.connect(QApplication.instance().quit)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            # 左键托盘 → 打开 Web 主界面（控制台）
            self.open_console_requested.emit()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.tray_icon.showMessage("达妮娅终端", "已最小化到托盘", QSystemTrayIcon.MessageIcon.Information, 1500)

    # ---------- 消息发送 ----------
    def _on_send(self):
        text = self.input_box.text().strip()
        if not text:
            return
        self.input_box.clear()
        self._handle_send(text)

    def _on_send_chat(self):
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        self._handle_send(text)

    def _handle_send(self, text: str):
        # 添加用户气泡
        self._append_message(text, is_user=True)
        self.chat_message_sent.emit(text)

    # ---------- 聊天消息渲染 ----------
    def _append_message(self, text: str, is_user: bool):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        bubble = ChatBubble("", is_user=is_user)
        bubble.set_text_with_typewriter(text)

        if is_user:
            # 用户消息：气泡右对齐
            row.addStretch()
            row.addWidget(bubble)
        else:
            # 达妮娅消息：小头像 + 名字 + 气泡
            avatar = AvatarLabel(os.path.join(ASSETS_DIR, "dania_idle.png"), size=36)
            col = QVBoxLayout()
            col.setSpacing(2)
            name = QLabel("达妮娅")
            name.setStyleSheet("color: #a9fbff; font-size: 11px; padding-left: 4px;")
            col.addWidget(name)
            col.addWidget(bubble)
            row.addWidget(avatar)
            row.addLayout(col)
            row.addStretch()

        # 插入到 stretch 之前
        self.chat_layout.insertLayout(self.chat_layout.count() - 1, row)

        # 自动滚动到底
        QTimer.singleShot(50, self._scroll_to_bottom)
        return bubble

    def _scroll_to_bottom(self):
        sb = self.chat_scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------- 流式打字机（达妮娅回复） ----------
    def stream_begin(self):
        """流式开始：创建达妮娅气泡占位"""
        self._stream_active = True
        self._stream_raw = ""
        self._typewriter_full = "…"
        self._typewriter_index = 0
        self._typing_active = True
        self._current_bot_bubble = self._append_message("", is_user=False)
        # 停掉 ChatBubble 内部打字机，由 TerminalWindow 统一驱动
        self._current_bot_bubble._typewriter_timer.stop()
        self._current_bot_bubble._shown_text = "…"
        self._current_bot_bubble._full_text = "…"
        self._current_bot_bubble.setText("…")
        self._current_bot_bubble._resize_to_content()
        self._typewriter_timer.start()

    def stream_append(self, text: str):
        """流式增量追加"""
        if not self._stream_active or not text:
            return
        self._stream_raw += text
        full = self._clean_tags(self._stream_raw).strip()
        if not full:
            full = "…"
        self._typewriter_full = full

    def stream_finish(self, final_text: str = ""):
        """流式结束，打字机追赶；final_text 为错误提示时显示给用户"""
        if self._stream_active:
            self._stream_active = False
            # 如果没有收到任何文本（失败/空回复），显示错误提示
            if not self._typewriter_full or self._typewriter_full == "…":
                if final_text:
                    self._typewriter_full = final_text
                else:
                    self._typewriter_full = "（暂无回复）"

    def _clean_tags(self, text: str) -> str:
        text = re.sub(r'\[情绪:\w+\]', '', text)
        text = re.sub(r'\[@[^\]]*\]', '', text)
        text = re.sub(r'\[[\u4e00-\u9fff][^\]]*\]', '', text)
        text = re.sub(r'\[[^\[\]]+\]', '', text)
        text = re.sub(r'【[^【】]+】', '', text)
        # 流式末尾不完整标签
        text = re.sub(r'\[情绪:[^\]]*\Z', '', text)
        text = re.sub(r'\[@[^\]]*\Z', '', text)
        return text

    def _typewriter_tick(self):
        full = self._typewriter_full
        b = self._current_bot_bubble
        if b is None:
            return
        if self._typewriter_index < len(full):
            remain = len(full) - self._typewriter_index
            step = 1
            if not self._stream_active and remain > 6:
                step = max(1, remain // 6)
            self._typewriter_index = min(len(full), self._typewriter_index + step)
            shown = full[:self._typewriter_index]
            b._shown_text = shown
            b.setText(shown)
            b._resize_to_content()
            self._scroll_to_bottom()
        elif self._stream_active:
            pass  # 等更多文本
        else:
            self._typewriter_timer.stop()
            self._typing_active = False
            b._typing_active = False

    # ---------- 对外接口（供控制器调用） ----------
    def set_avatar_state(self, state: str):
        """切换立绘状态（idle/happy/sleeping/speaking/thinking）"""
        path = self._state_images.get(state, self._state_images["idle"])
        if os.path.exists(path):
            self.avatar.set_image(path)

    def show_reply_text(self, text: str):
        """非流式：直接显示达妮娅回复（带打字机）"""
        b = self._append_message(text, is_user=False)
        self._current_bot_bubble = b

    def flash_message(self, text: str):
        """临时提示气泡"""
        self._append_message(text, is_user=False)

    def set_online_status(self, online: bool):
        if hasattr(self, "title_bar"):
            self.title_bar.set_online(online)

    def update_service_status(self, name: str, status: str):
        lbl = self.service_status.get(name)
        if lbl is None:
            return
        color_map = {
            "running": ("#00f5ff", "● 运行中"),
            "starting": ("#f0c040", "● 启动中…"),
            "stopped": ("#5a7a8a", "● 已停止"),
            "failed": ("#ff5c5c", "● 失败"),
        }
        color, text = color_map.get(status, ("#f0c040", f"● {status}"))
        lbl.setText(text)
        lbl.setStyleSheet(f"color: {color}; font-size: 12px;")

    def update_task_card(self, text: str):
        self.task_card.set_content(text)

    def update_memory_card(self, text: str):
        self.memory_card.set_content(text)

    def populate_history(self, history: list):
        self.history_list.clear()
        for i, item in enumerate(history):
            if isinstance(item, dict):
                role = item.get("role", "")
                content = item.get("content", "")
                prefix = "你: " if role == "user" else "达妮娅: "
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                prefix = "你: "
                content = f"{item[0]}  →  {item[1]}"
            else:
                prefix = ""
                content = str(item)
            self.history_list.addItem(f"[{i}] {prefix}{content[:80]}")

    def populate_tasks(self, tasks: list):
        self.task_list.clear()
        for t in tasks:
            self.task_list.addItem(str(t))
