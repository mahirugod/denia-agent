# -*- coding: utf-8 -*-
"""B站扫码登录窗口

流程：加载 passport.bilibili.com 登录页 → 用户用 B站 APP 扫码
     → 从 QWebEngineProfile 的 cookieStore 里取 SESSDATA/bili_jct
     → 调 nav 接口校验 isLogin → 通过则回传凭证

不需要用户手动复制 cookie；登录态持久化到 storage_dir，下次打开可直接复用。
"""

import threading

import requests
from PySide6.QtCore import QDir, QUrl, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView

LOGIN_URL = "https://passport.bilibili.com/login"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class BiliLoginDialog(QDialog):
    """扫码登录，成功后通过 logged_in 回传 (SESSDATA, bili_jct)"""

    logged_in = Signal(str, str)

    def __init__(self, parent=None, storage_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle("B站 扫码登录")
        self.resize(540, 720)
        self._cookies = {}
        self._verifying = False

        self._profile = QWebEngineProfile("bilibili-login", self)
        if storage_dir:
            QDir().mkpath(storage_dir)
            self._profile.setPersistentStoragePath(storage_dir)
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self._profile.cookieStore().cookieAdded.connect(self._on_cookie)

        self.view = QWebEngineView(self)
        self.view.setPage(QWebEnginePage(self._profile, self.view))

        self.status = QLabel("用 B站 手机 APP 扫码登录，成功后窗口会自动关闭")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#888; padding:8px; font-size:12px;")
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 4, 8, 8)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view, 1)
        layout.addLayout(bottom)

        self.view.load(QUrl(LOGIN_URL))
        # 已持久化的 cookie 会逐条触发 cookieAdded（上次登录过就不用再扫）
        self._profile.cookieStore().loadAllCookies()

    def _on_cookie(self, cookie):
        try:
            name = bytes(cookie.name()).decode("utf-8", "ignore").strip()
            value = bytes(cookie.value()).decode("utf-8", "ignore").strip()
        except Exception:
            return
        if not name:
            return
        self._cookies[name] = value
        if name == "SESSDATA" and value and not self._verifying:
            self._verifying = True
            self.status.setText("已拿到登录凭证，正在校验…")
            threading.Thread(
                target=self._verify, args=(value,), name="bili-login-verify", daemon=True
            ).start()

    def _verify(self, sesdata: str):
        jct = self._cookies.get("bili_jct", "")
        cookie = f"SESSDATA={sesdata}"
        if jct:
            cookie += f"; bili_jct={jct}"
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={
                    "User-Agent": _UA,
                    "Referer": "https://www.bilibili.com/",
                    "Cookie": cookie,
                },
                timeout=8,
            )
            data = resp.json() or {}
            payload = data.get("data") or {}
            logged = bool(payload.get("isLogin"))
            uname = payload.get("uname") or ""
        except Exception as exc:
            print(f"[B站登录] 校验异常: {exc}")
            logged, uname = False, ""

        if logged:
            print(f"[B站登录] 成功: {uname}")
            self.status.setText(f"登录成功（{uname}），凭证已获取")
            self.logged_in.emit(sesdata, jct)
        else:
            self._verifying = False
            self.status.setText("还没登录成功，请用 B站 APP 扫码")
