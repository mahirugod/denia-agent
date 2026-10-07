#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅终端 - 悬浮小人窗口（最小化形态）
透明无边框，立绘 + 呼吸浮动，双击恢复终端
"""
import os
import math

from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QPixmap, QColor, QCursor, QPainterPath, QRegion
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


class FloatingPet(QWidget):
    """悬浮小人：透明窗口，立绘居中，呼吸上下浮动"""

    restore_requested = Signal()
    double_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFixedSize(140, 180)
        self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))

        # 立绘
        self._char_label = QLabel(self)
        self._char_label.setFixedSize(120, 160)
        self._char_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._base_y = 10
        self._load_image("dania_idle.png")

        # 呼吸浮动
        self._float_phase = 0.0
        self._float_timer = QTimer(self)
        self._float_timer.setInterval(50)
        self._float_timer.timeout.connect(self._on_float)
        self._float_timer.start()

        # 拖动
        self._drag_pos = None

        # 双击恢复
        self._click_count = 0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(250)
        self._click_timer.timeout.connect(self._on_click_timeout)

    def _load_image(self, name: str):
        path = os.path.join(ASSETS_DIR, name)
        if os.path.exists(path):
            pm = QPixmap(path)
            if not pm.isNull():
                scaled = pm.scaled(
                    120, 160,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._char_label.setPixmap(scaled)

    def set_state(self, state: str):
        img_map = {
            "idle": "dania_idle.png",
            "happy": "dania_happy.png",
            "sleeping": "dania_sleeping.png",
            "speaking": "dania_speaking.png",
            "thinking": "dania_thinking.png",
        }
        self._load_image(img_map.get(state, "dania_idle.png"))

    def _on_float(self):
        self._float_phase += 0.04
        dy = int(math.sin(self._float_phase) * 3)
        self._char_label.move(10, self._base_y + dy)

    def _on_click_timeout(self):
        self._click_count = 0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event):
        self.restore_requested.emit()
        self.double_clicked.emit()
