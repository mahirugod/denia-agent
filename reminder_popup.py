#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅终端 - 提醒弹窗
到点弹出独立置顶窗口，不影响主窗口布局。
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGraphicsDropShadowEffect,
)
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor


class ReminderPopup(QWidget):
    """提醒到点弹出的独立窗口（置顶 + 无边框 + 阴影）"""

    def __init__(self, text: str, parent=None):
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setMinimumWidth(320)
        self.setStyleSheet(
            "QLabel { color:#00f5ff; font-size:14px; }"
            "QPushButton { background:rgba(255,45,163,0.2); color:#ff63bb; border:1px solid rgba(255,45,163,0.4); border-radius:6px; padding:6px 18px; }"
            "QPushButton:hover { background:rgba(255,45,163,0.4); }"
            "QWidget { background:#0a0e1a; }"
        )

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        title = QLabel("⏰ 达妮娅的提醒")
        title.setStyleSheet("color:#ff63bb; font-size:16px; font-weight:bold;")
        v.addWidget(title)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setStyleSheet("color:#e8f0ff; font-size:14px; line-height:1.5;")
        v.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(10)
        ok = QPushButton("知道了")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self.close)
        row.addStretch()
        row.addWidget(ok)
        v.addLayout(row)

        # 阴影
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(40)
        shadow.setColor(QColor(0, 245, 255, 80))
        self.setGraphicsEffect(shadow)

        self.adjustSize()

    def show_centered(self):
        """屏幕中央偏上弹出"""
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.x() + (geo.width() - self.width()) // 2
            y = geo.y() + int(geo.height() * 0.3)
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
