#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅终端 - 主入口
独立桌面应用：现代化终端 UI + LLM 流式对话 + 语音合成 + 悬浮小人
"""
import os
import sys
import json
import base64
import signal
import time
import collections

# Windows 控制台强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 终端自身目录优先（避免与同名模块冲突）
TERMINAL_DIR = os.path.dirname(os.path.abspath(__file__))
if TERMINAL_DIR not in sys.path:
    sys.path.insert(0, TERMINAL_DIR)

# bot 后端模块随项目内迁（整合自 d:\denia_chatbot）
BOT_DIR = os.path.join(TERMINAL_DIR, "bot")
if BOT_DIR not in sys.path:
    sys.path.append(BOT_DIR)

from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from chat_controller import ChatController
from pet_widget import FloatingPet
from console_window import ConsoleWindow
from process_manager import ProcessManager, SERVICE_CONFIGS
from weather import WeatherWorker
from system_status import SystemStatusWorker
from PySide6.QtWidgets import QMessageBox

# 主动性监控（定时巡检 Ollama/磁盘/待办，异常时主动发声 + 弹窗 + 通知）
sys.path.append(os.path.join(TERMINAL_DIR, "bot"))
try:
    from bot.active_monitor import get_default_monitor
except Exception as _e:
    get_default_monitor = None
    print(f"[主动性] 加载 active_monitor 失败: {_e}")


ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
SETTINGS_FILE = os.path.join(TERMINAL_DIR, "settings.json")


class TerminalApp(QObject):
    """达妮娅终端应用主控"""

    def __init__(self):
        super().__init__()
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setApplicationName("达妮娅终端")

        # 组件
        self.pet = FloatingPet()
        self.controller = ChatController()
        self.process_mgr = ProcessManager()
        self.console_window = None  # Web 控制台（懒加载）
        self.tray = None            # 托盘图标（_setup_tray 创建）
        # 日志环形缓冲：控制台后打开也能看到之前的服务日志
        self._log_buffer = collections.deque(maxlen=500)

        self._wire_signals()
        self._setup_tray()

        # 全局异常
        sys.excepthook = self._global_exception_handler

        # 加载持久化设置
        self._load_settings()

        # 天气（wttr.in 免费接口，IP 定位或 settings.weather_city 指定城市，30 分钟刷新）
        self.weather = WeatherWorker(self._settings.get("weather_city", ""))
        self.weather.start()

        # 电脑状态（psutil，3 秒一次）
        self.system_status = SystemStatusWorker()
        self.system_status.start()

        # 主动性监控：定时巡检 Ollama/磁盘/待办，异常主动发声 + 弹窗 + 通知
        self.active_monitor = None
        if get_default_monitor is not None:
            self.active_monitor = get_default_monitor(interval_ms=60000)
            self.active_monitor.set_callback(self._on_active_alert)
            self._update_ollama_monitor()
            print("[主动性] 监控已启动（60s 巡检）")

    def _update_ollama_monitor(self):
        """根据当前模型类型开关 Ollama 巡检：仅本地 Ollama 模型才巡检本地推理后端"""
        if self.active_monitor is None:
            return
        # 现在只走 agnes 云端一条路：Ollama 已不再作为启停对象，默认关闭 Ollama 巡检
        self.active_monitor.set_ollama_enabled(False)

    def _load_settings(self):
        """从 settings.json 加载设置"""
        defaults = {"napcat_enabled": False, "auto_start": True}
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    defaults.update(data)
        except Exception as e:
            print(f"[设置] 加载失败，使用默认: {e}")
        self._settings = defaults
        # 只走 agnes 云端一条路：非 agnes 模型强制切回
        model = self._settings.get("model_name", "")
        if not model.startswith("agnes"):
            self._settings["model_name"] = "agnes-3.0-flash"
            self._on_log("🔒 模型已锁定为云端 agnes-3.0-flash（本地推理入口已下线）")
            try:
                self._save_settings()
            except Exception:
                pass
        # 设置只保存在 self._settings，不再回填原生窗口控件（Web 控制台为唯一 UI）

    def _save_settings(self, updates: dict = None):
        """保存设置到 settings.json

        传入 updates 时只覆盖这些键，其余键（weather_city / bilibili_* /
        proactive_* 等）原样保留；不传则原样重写当前配置。
        """
        if updates:
            self._settings.update(updates)
        # 先写临时文件再原子替换：写一半崩溃/断电也不会留下半个 settings.json
        tmp = SETTINGS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_FILE)
            self._on_log("💾 设置已保存")
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            self._on_log(f"❌ 保存设置失败: {e}")

    def napcat_enabled(self) -> bool:
        """NapCat 是否启用（读 settings.json，默认关闭）"""
        return bool(self._settings.get("napcat_enabled", False))

    def _wire_signals(self):
        c = self.controller
        pm = self.process_mgr

        # 控制器 → 应用级（Web 控制台的桥接在 _get_or_create_console 里接）
        c.state_changed.connect(self._on_state_changed)
        c.log.connect(self._on_log)

        # 悬浮小人
        self.pet.restore_requested.connect(self._restore_window)

        # 进程管理器
        pm.log_received.connect(self._on_log)

    def _setup_tray(self):
        """托盘图标与菜单（应用级，不依赖任何窗口）"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[托盘] 系统托盘不可用（图标会落到通知区域折叠区），改用悬浮小人入口")
            return
        tray_path = os.path.join(ASSETS_DIR, "tray_dania.png")
        if not os.path.exists(tray_path):
            print(f"[托盘] 图标文件缺失: {tray_path}")
            return
        self.app.setWindowIcon(QIcon(tray_path))
        self.tray = QSystemTrayIcon(QIcon(tray_path), self.app)
        self.tray.setToolTip("达妮娅终端")
        menu = QMenu()
        menu.addAction("🖥️ 打开控制台", self.show_console)
        menu.addAction("🐾 悬浮小人", self._show_pet)
        menu.addSeparator()
        menu.addAction("❌ 退出", self._quit)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu  # 必须持有引用，否则菜单会被回收
        self.tray.activated.connect(
            lambda reason: self.show_console()
            if reason == QSystemTrayIcon.Trigger else None
        )
        self.tray.show()
        print(f"[托盘] 已创建 (visible={self.tray.isVisible()})，"
              f"图标可能被 Windows 收进任务栏通知区域，点击 ^ 可展开")

    def _quit(self):
        """托盘退出：停服务、卸键盘监听后退出"""
        try:
            self.controller.uninstall_system_space_listener()
        except Exception:
            pass
        try:
            self.process_mgr.stop_all()
        except Exception:
            pass
        self.app.quit()

    # ---------- Web 控制台 ----------
    def _get_or_create_console(self):
        """懒加载创建 Web 控制台，并把控制器流式信号桥接过去"""
        if self.console_window is None:
            self.console_window = ConsoleWindow(self)
            b = self.console_window.bridge
            c = self.controller
            c.stream_begin.connect(b.chat_started.emit)
            c.stream_delta.connect(b.chat_delta.emit)
            c.stream_stall.connect(b.chat_stall.emit)
            c.stream_clear.connect(b.chat_clear.emit)
            c.voice_partial.connect(b.chat_partial.emit)
            c.stream_finish.connect(b.chat_finished.emit)
            c.stream_thinking.connect(b.chat_thinking.emit)
            c.tool_event.connect(b.tool_event.emit)
            # c.log 已经连到 _on_log，由 _on_log 统一转发到 b.log，避免重复
            c.reminder_fired.connect(b.reminder_fired.emit)
            c.state_changed.connect(b.emotion_changed.emit)
            c.user_message.connect(b.user_message.emit)
            self.weather.weather_ready.connect(
                lambda d: b.weather_updated.emit(json.dumps(d, ensure_ascii=False)))
            self.system_status.system_ready.connect(
                lambda d: b.system_updated.emit(json.dumps(d, ensure_ascii=False)))
            # 桌面端发图出口：page_image / image_search 等工具在没有 QQ 会话时推到控制台
            try:
                from qq_outbox import set_local_sink

                set_local_sink(self._push_console_images)
            except Exception as e:
                print(f"[发图] 注册控制台出口失败: {e}")
        return self.console_window

    def _push_console_images(self, items):
        """把工具抓到的图片转成 data URL 推给控制台，显示为图片气泡"""
        win = self.console_window
        if win is None:
            return
        imgs = []
        for item in items or []:
            src = str(item or "").strip()
            if not src:
                continue
            if src.lower().startswith(("http://", "https://")):
                imgs.append({"src": src, "name": ""})
                continue
            try:
                if not os.path.isfile(src) or os.path.getsize(src) > 8 * 1024 * 1024:
                    continue
                ext = os.path.splitext(src)[1].lower().lstrip(".") or "png"
                mime = {
                    "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
                    "gif": "image/gif", "bmp": "image/bmp",
                }.get(ext, "image/png")
                with open(src, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                imgs.append({"src": f"data:{mime};base64,{b64}", "name": os.path.basename(src)})
            except Exception:
                continue
        if not imgs:
            return
        try:
            win.bridge.image_message.emit(json.dumps({"images": imgs}, ensure_ascii=False))
        except Exception as e:
            print(f"[发图] 推送控制台失败: {e}")

    def show_console(self):
        self.weather.refresh()  # 打开控制台时顺带刷新天气（5 分钟节流）
        w = self._get_or_create_console()
        # 首次显示：默认最大化
        if not getattr(self, "_console_positioned", False):
            self._console_positioned = True
            w.showMaximized()
        else:
            w.show()
        w.raise_()
        w.activateWindow()

    # ----- 控制台桥接适配方法（供 console_window.BridgeObject 调用）-----
    def get_weather(self) -> str:
        """最近天气缓存（JSON 字符串），无缓存时返回 {}"""
        try:
            return json.dumps(self.weather.latest() or {}, ensure_ascii=False)
        except Exception:
            return "{}"

    def get_system_status(self) -> str:
        """最近一次电脑状态（JSON 字符串）"""
        try:
            return json.dumps(self.system_status.latest() or {}, ensure_ascii=False)
        except Exception:
            return "{}"

    def console_send_message(self, text: str):
        """控制台聊天 → 交给 ChatController（与主窗口共用同一条流式链路）"""
        self.controller.send_message(text)

    # ----- Cloudflare WARP 开关（顶栏胶囊）-----
    def get_warp_status(self) -> str:
        """WARP 当前状态（JSON 字符串）"""
        try:
            import warp_control
            return json.dumps(warp_control.status(), ensure_ascii=False)
        except Exception:
            return "{}"

    def toggle_warp(self) -> dict:
        """连接 / 断开 WARP"""
        import warp_control
        return warp_control.toggle()

    def _on_voice_interrupt(self):
        """控制台打断语音 → 转发给控制器"""
        try:
            self.controller.interrupt_voice()
        except Exception:
            pass

    def get_memories(self) -> list:
        """记忆库已移除，返回空列表（控制台 memory 页显示"暂无记忆"）"""
        return []

    def get_tasks(self) -> list:
        tasks = []
        try:
            import dania_tools

            for r in getattr(dania_tools, "_active_reminders", []):
                tasks.append(
                    {"text": r.get("content", ""), "done": False, "prio": "中"}
                )
        except Exception:
            pass
        if not tasks:
            tasks.append({"text": "暂无提醒任务", "done": False, "prio": "低"})
        return tasks[:10]

    def get_services(self) -> list:
        result = []
        for name, cfg in SERVICE_CONFIGS.items():
            if name == "bot":
                continue  # bot 合并到 napcat 条目，不单独显示
            state = self.process_mgr.get_service_status(name)
            display = cfg.display_name
            if name == "napcat":
                display = "NapCat + QQ 机器人"
                # 状态综合：napcat 运行中但 bot 未就绪 → 视为启动中
                bot_state = self.process_mgr.get_service_status("bot")
                if state == "running" and bot_state != "running":
                    state = "starting"
            result.append({"name": name, "display": display, "state": state})
        # WARP 开关行：状态映射 connected→running / connecting→starting / 其它→stopped
        try:
            import warp_control
            w = warp_control.status() or {}
            ws = w.get("state", "missing")
            if not w.get("available"):
                w_state = "failed"
            elif ws == "connected":
                w_state = "running"
            elif ws == "connecting":
                w_state = "starting"
            else:
                w_state = "stopped"
            result.insert(0, {"name": "warp", "display": "Cloudflare WARP",
                             "state": w_state, "note": w.get("text", "")})
        except Exception as e:
            result.insert(0, {"name": "warp", "display": "Cloudflare WARP",
                             "state": "failed", "note": str(e)[:60]})
        return result

    def warp_start(self) -> None:
        """启动页 WARP「启动」按钮：连接 WARP（后台执行，不卡界面）"""
        import threading

        def work():
            try:
                import warp_control
                data = warp_control.set_enabled(True)
                self._on_log(f"🌐 WARP: {data.get('text', '')[:80]}")
            except Exception as e:
                self._on_log(f"⚠️ WARP 连接失败: {str(e)[:80]}")

        threading.Thread(target=work, name="warp-start", daemon=True).start()

    def warp_stop(self) -> None:
        """启动页 WARP「停止」按钮：断开 WARP（后台执行，不卡界面）"""
        import threading

        def work():
            try:
                import warp_control
                data = warp_control.set_enabled(False)
                self._on_log(f"🌐 WARP: {data.get('text', '')[:80]}")
            except Exception as e:
                self._on_log(f"⚠️ WARP 断开失败: {str(e)[:80]}")

        threading.Thread(target=work, name="warp-stop", daemon=True).start()

    def get_settings(self) -> list:
        # 只走 agnes 云端一条路：模型只列云端，不再展示本地推理配置项
        model_name = self._settings.get("model_name", "agnes-3.0-flash")
        options = [
            {"value": "agnes-3.0-flash", "label": "云端 agnes-3.0-flash"},
        ]
        model_items: list = [
            {"label": "云端 API Key", "key": "cloud_api_key", "value": self._settings.get("cloud_api_key", ""), "type": "str", "placeholder": "sk-... 或其它 Bearer token"},
            {"label": "云端 Base URL", "key": "cloud_base_url", "value": self._settings.get("cloud_base_url", ""), "type": "str", "placeholder": "如 https://api.agnes-ai.cn/v1"},
        ]
        model_items.append({"label": "提示：模型已锁定为云端 agnes（本地推理入口已下线），改配置后会自动重新初始化", "key": "_model_hint", "value": "", "type": "hint"})

        return [
            {"label": "角色形象", "key": "character", "value": "达妮娅", "type": "str"},
            {"label": "默认缩放", "key": "scale", "value": "0.7x", "type": "str"},
            {"label": "语音合成", "key": "tts", "value": "GPT-SoVITS", "type": "str"},
            {
                "label": "LLM 模型",
                "key": "model_name",
                "value": model_name,
                "type": "model_select",
                "options": options,
            },
            {
                "label": "回复长度",
                "key": "reply_len",
                "value": "简短（1-2句）",
                "type": "str",
            },
            *model_items,
            {
                "label": "B站 登录凭证（官号动态）",
                "key": "bilibili_login",
                "type": "action",
                "button": "扫码登录",
                "value": self._bili_state_text(),
            },
            {
                "label": "启用 NapCat（QQ 消息转发）",
                "key": "napcat",
                "value": self.napcat_enabled(),
                "type": "bool",
            },
            {
                "label": "启动时自动检测并补齐服务",
                "key": "autostart",
                "value": bool(self._settings.get("auto_start", True)),
                "type": "bool",
            },
        ]

    def _bili_state_text(self) -> str:
        """设置页展示的 B站 凭证状态文案"""
        if not (self._settings.get("bilibili_sesdata") or "").strip():
            return "未配置（只能搜B站视频，拿不到官方公告）"
        ok = getattr(self, "_bili_cookie_ok", None)
        if ok is False:
            return "已失效，请重新扫码"
        if ok is True:
            return "已配置（有效）"
        return "已配置"

    def save_bilibili_cookie(self, sesdata: str, bili_jct: str = ""):
        """保存扫码登录拿到的 B站 凭证（web_search 按 mtime 缓存，无需重启）"""
        if not sesdata:
            return
        updates = {"bilibili_sesdata": sesdata}
        if bili_jct:
            updates["bilibili_jct"] = bili_jct
        self._save_settings(updates)
        self._bili_cookie_ok = True
        self._on_log("✅ B站 登录成功，官号动态已启用")

    def save_setting(self, key: str, value: str):
        self._on_log(f"⚙️ 设置变更: {key} = {value}")
        if key == "napcat":
            checked = value.lower() == "true"
            self._save_settings({"napcat_enabled": checked})
        elif key == "autostart":
            checked = value.lower() == "true"
            self._save_settings({"auto_start": checked})
        elif key in ("cloud_api_key", "cloud_base_url", "cloud_model",
                      "llm_url", "llm_api_key"):
            # 模型配置项（通用云端 / 本地推理后端）：直接写 settings.json
            # 其中 llm_url / 云端三项 改完需要重新初始化 LLM 才生效
            self._save_settings({key: value})
            # 影响 LLM 路由/请求的参数，保存后立即重新初始化
            if key in ("llm_url", "llm_api_key", "cloud_api_key", "cloud_base_url"):
                import threading
                threading.Thread(
                    target=lambda: self.controller.reinit_bot(self._do_init_bot),
                    daemon=True,
                ).start()
        elif key == "model_name":
            # 切换 LLM 模型：写 settings.json + 后台重新初始化
            self._save_settings({"model_name": value})
            label_map = {"qwen2.5:3b": "本地 qwen2.5:3b", "agnes-3.0-flash": "云端 agnes-3.0-flash"}
            label = label_map.get(value)
            if label is None:
                label = f"云端 {value}" if (value.startswith("agnes") or value.startswith("cloud:")) else f"本地 {value}"
            self._on_log(f"🔄 切换模型: {label}")
            self._update_ollama_monitor()
            import threading
            threading.Thread(
                target=lambda: self.controller.reinit_bot(
                    self._do_init_bot,
                ),
                daemon=True,
            ).start()

    # ---------- 状态 ----------
    def _on_state_changed(self, state: str):
        self.pet.set_state(state)

    # ---------- 日志 ----------
    def _on_log(self, text: str):
        """所有日志统一出口：写盘 / 控制台缓冲 / Web 控制台日志面板"""
        print(text, flush=True)
        line = str(text or "")
        try:
            self._log_buffer.append(line)
        except Exception:
            pass
        if self.console_window is not None:
            try:
                self.console_window.bridge.log.emit(line)
            except Exception:
                pass

    def get_recent_logs(self) -> str:
        """控制台打开时拉取打开前的日志（JSON 数组字符串）"""
        try:
            return json.dumps(list(self._log_buffer), ensure_ascii=False)
        except Exception:
            return "[]"

    def get_recent_traces(self) -> str:
        """控制台「最近对话」：读 bot/data/qq_trace.jsonl 末尾 30 轮"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "bot", "data", "qq_trace.jsonl")
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()[-30:]
            rows = []
            for ln in lines:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
            return json.dumps(rows, ensure_ascii=False)
        except Exception:
            return "[]"

    # ---------- 主动性告警 ----------
    def _on_active_alert(self, alert: dict):
        """
        主动性监控回调（ActiveMonitor 后台线程调用）。
        发现 Ollama 挂了 / 磁盘偏紧 / 有待办 等异常时，主动发声 + 弹窗 + 通知。
        需要切回 GUI 线程执行弹窗（用 Qt 信号桥接）。
        """
        key = alert.get("key", "")
        severity = alert.get("severity", "info")
        title = alert.get("title", "")
        detail = alert.get("detail", "")
        ts = alert.get("ts", "")

        print(f"[主动性][{ts}] {title} —— {detail}")

        # 切到 GUI 线程执行弹窗 + 语音
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._show_alert_popup(title, detail, severity))
            # 异常时语音发声（达妮娅亲口说）
            if key in ("ollama_down", "disk_low", "has_pending"):
                voice_text = self._alert_voice_text(key, detail)
                if voice_text:
                    QTimer.singleShot(0, lambda: self._speak_alert(voice_text))
        except Exception as e:
            print(f"[主动性] 切线程异常: {e}")

    def _alert_voice_text(self, key: str, detail: str) -> str:
        """异常 → 达妮娅人设口语（喂给 TTS）"""
        if key == "ollama_down":
            return "哎呀，Ollama 服务好像断掉了，我这边说话不太方便。你帮我看看是不是 `ollama serve` 没跑起来？"
        elif key == "disk_low":
            return detail  # detail 已含「C 盘剩 X GB」，达妮娅直接念
        elif key == "has_pending":
            return "你还有几个待办提醒没处理呢，记得看一眼～"
        return ""

    def _speak_alert(self, text: str):
        """GUI 线程：达妮娅语音说异常（复用 replay_voice 同款 TTS 链路）"""
        try:
            self.controller.replay_voice(text)
            print(f"[主动性] 语音告警: {text}")
        except Exception as e:
            print(f"[主动性] 语音发声异常: {e}")

    def _show_alert_popup(self, title: str, detail: str, severity: str):
        """GUI 线程：弹窗 + 托盘通知（异常时）"""
        icon = {
            "warn": QMessageBox.Icon.Warning,
            "info": QMessageBox.Icon.Information,
            "ok": QMessageBox.Icon.Information,
        }.get(severity, QMessageBox.Icon.Information)

        # 1. 托盘通知（后台也有提示）
        try:
            if self.tray and self.tray.isVisible():
                msg_type = {
                    "warn": QSystemTrayIcon.MessageIcon.Warning,
                    "info": QSystemTrayIcon.MessageIcon.Information,
                    "ok": QSystemTrayIcon.MessageIcon.Information,
                }.get(severity, QSystemTrayIcon.MessageIcon.Information)
                self.tray.showMessage(title, detail, msg_type, 5000)
        except Exception:
            pass

        # 2. 只有 warn 才弹窗（ok 类「已恢复」不打扰用户）
        if severity == "warn":
            try:
                box = QMessageBox()
                box.setIcon(icon)
                box.setWindowTitle("达妮娅提醒")
                box.setText(title)
                box.setInformativeText(detail)
                box.setStandardButtons(QMessageBox.Ok)
                box.show()
            except Exception as e:
                print(f"[主动性] 弹窗异常: {e}")

    # ---------- 服务 ----------
    def _on_start_all(self):
        self._on_log("🚀 启动所有服务...")
        # 启动已改为 QTimer 异步（不再 sleep 等端口），必须在 GUI 线程调用
        skip = [] if self.napcat_enabled() else ["napcat", "bot"]
        # Ollama 已下线：只走 agnes 云端一条路，不再启动本地推理后端
        self.process_mgr.start_all(skip=skip)

    def _on_stop_all(self):
        self._on_log("⏹ 停止所有服务...")
        self.process_mgr.stop_all()

    def _on_start_single(self, service_key: str):
        display_map = {
            "warp": "Cloudflare WARP",
            "napcat": "NapCat + QQ 机器人",
            "gpt_sovits": "GPT-SoVITS",
        }
        name = display_map.get(service_key, service_key)
        self._on_log(f"🚀 启动 {name}...")
        # 启动已改为 QTimer 异步，直接在当前（GUI）线程调用
        if service_key == "warp":
            self.warp_start()
            return
        self.process_mgr.start_service(service_key)
        if service_key == "napcat":
            # 等 NapCat WebSocket 就绪后再拉起机器人（不阻塞界面）
            QTimer.singleShot(
                3000, lambda: self.process_mgr.start_service("bot"))

    def _on_stop_single(self, service_key: str):
        display_map = {
            "warp": "Cloudflare WARP",
            "napcat": "NapCat + QQ 机器人",
            "gpt_sovits": "GPT-SoVITS",
        }
        name = display_map.get(service_key, service_key)
        self._on_log(f"⏹ 停止 {name}...")
        if service_key == "warp":
            self.warp_stop()
            return
        if service_key == "napcat":
            self.process_mgr.stop_service("bot")
        self.process_mgr.stop_service(service_key)

    # ---------- 悬浮小人 ----------
    def _place_pet(self):
        """把小人放到主屏右下角"""
        screen = self.app.primaryScreen().geometry()
        x = screen.width() - self.pet.width() - 60
        y = screen.height() - self.pet.height() - 100
        self.pet.move(x, y)

    def _show_pet(self):
        """隐藏控制台，收成右下角悬浮小人"""
        if self.console_window is not None:
            self.console_window.hide()
        self._place_pet()
        self.pet.show()
        self._pet_active_transition = time.time()

    def _restore_window(self):
        """小人双击 → 回到 Web 主界面（控制台）"""
        self.pet.hide()
        self.show_console()
        self._pet_active_transition = time.time()

    def _start_pet_monitor(self):
        """
        控制台"失顶层"自动收小人：
        每秒巡检一次 —— 当前激活的 Qt 窗口不是控制台，且小人尚未显示，
        就收小人；重新激活控制台后再恢复。
        5 秒冷却避免 _show_pet / _restore_window 切换瞬间反向震荡。
        """
        if getattr(self, "_pet_monitor_timer", None):
            return
        self._pet_monitor_timer = QTimer(self.app)
        self._pet_monitor_timer.setInterval(1000)
        self._pet_monitor_timer.timeout.connect(self._check_pet_trigger)
        self._pet_monitor_timer.start()

    def _check_pet_trigger(self):
        if self.pet.isVisible():
            return
        # 5 秒冷却：切换瞬间不做反向判断
        last_transition = getattr(self, "_pet_active_transition", 0)
        if time.time() - last_transition < 5:
            return
        active = self.app.activeWindow()
        console = self.console_window
        # 控制台当前可见，且用户切走了 → 收小人
        if console and console.isVisible() and active is not console:
            self._on_log("🐾 终端不在顶层，自动收小人")
            self._show_pet()

    # ---------- 异常 ----------
    def _global_exception_handler(self, exc_type, exc_value, exc_tb):
        import traceback

        err = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(f"[崩溃] {err}", file=sys.stderr)
        try:
            crash_path = os.path.join(os.path.dirname(__file__), "crash.log")
            with open(crash_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{err}\n")
        except Exception:
            pass

    # ---------- 启动 ----------
    def run(self) -> int:
        # 默认主界面 = Web 霓虹控制台；原生 Qt 窗口收进托盘备用
        self.show_console()

        # 系统级空格监听：即使 Web 引擎吞键，也能触发录音 / 识别 / 打断
        self.controller.install_system_space_listener()

        # 控制台失顶层自动收小人
        self._start_pet_monitor()

        print("=" * 50)
        print("✨ 达妮娅终端已启动（Web 主界面）")
        print("   左侧导航：首页/对话/记忆/任务/服务/设置")
        print("   不在顶层：自动收起为右下角悬浮小人；双击小人回到控制台")
        print("=" * 50)

        # 延迟初始化机器人（避免启动卡顿）
        QTimer.singleShot(800, self._lazy_init)

        # 自动检测服务
        QTimer.singleShot(1500, self._auto_detect_services)

        # 启动后按需自动连 WARP（x.com 直连不通且 WARP 可用时）
        QTimer.singleShot(6000, self._auto_warp)

        # 启动后校验 B站 登录凭证，失效则提示重新扫码
        QTimer.singleShot(8000, self._check_bili_cookie)

        return self.app.exec()

    def _check_bili_cookie(self):
        """后台校验 bilibili_sesdata 是否仍然有效（结果供设置页显示）"""
        import threading

        def work():
            try:
                import requests
                from web_search import get_bilibili_cookie

                cookie = get_bilibili_cookie()
                if not cookie:
                    self._bili_cookie_ok = None
                    return
                resp = requests.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/124.0.0.0 Safari/537.36",
                        "Referer": "https://www.bilibili.com/",
                        "Cookie": cookie,
                    },
                    timeout=8,
                )
                ok = bool(((resp.json() or {}).get("data") or {}).get("isLogin"))
                self._bili_cookie_ok = ok
                if not ok:
                    self._on_log("⚠️ B站 登录凭证已失效，去设置页点「扫码登录」更新")
            except Exception as exc:
                print(f"[B站校验] {exc}")

        threading.Thread(target=work, name="bili-cookie-check", daemon=True).start()

    def _auto_warp(self):
        """后台线程探测外网并在需要时连 WARP，不阻塞界面"""
        import threading

        def work():
            try:
                import warp_control
                result = warp_control.maybe_auto_connect()
                print(f"[WARP] 启动检查结果: {result}")
                if result == "connected" and self.console_window is not None:
                    data = warp_control.status()
                    self.console_window.bridge.warp_updated.emit(
                        json.dumps(data, ensure_ascii=False))
            except Exception as e:
                print(f"[WARP] 自动连接检查失败: {e}")

        threading.Thread(target=work, name="warp-auto", daemon=True).start()

    def _lazy_init(self):
        self._on_log("🤖 正在初始化达妮娅...")
        # 后台线程初始化（涉及网络探测/子进程，避免阻塞 UI）
        import threading

        threading.Thread(target=self._do_init_bot, daemon=True).start()
        # 顺手清理过期缓存（媒体缓存 7 天 / Edge 缓存），低频后台任务
        threading.Thread(target=self._cleanup_caches, daemon=True).start()

    def _cleanup_caches(self):
        """启动时清理媒体缓存与 Edge 缓存，避免目录无限增长"""
        try:
            from bot.media_downloader import cleanup_media_cache
            cleanup_media_cache(days=7)
        except Exception as e:
            print(f"[缓存清理] 媒体缓存清理失败: {e}")
        try:
            from bot import browser_session
            browser_session.cleanup_profile_cache()
        except Exception as e:
            print(f"[缓存清理] 浏览器缓存清理失败: {e}")

    def _do_init_bot(self):
        try:
            # 模型名从 settings.json 读，切换本地/云端只需改 settings.json 的 model_name
            model_name = self._settings.get("model_name", "qwen2.5:3b")
            # ags 与通用云端共用 cloud_api_key / cloud_base_url（设置页只暴露一组）
            if model_name.startswith("agnes") or model_name.startswith("cloud:"):
                base_url = self._settings.get("cloud_base_url", "https://apihub.agnes-ai.com/v1")
                api_key = self._settings.get("cloud_api_key", "")
            else:
                # 本地模型：默认 Ollama，但 base_url 可在 settings.json / 设置页自定义（指向任意兼容推理后端）
                base_url = self._settings.get("llm_url", "http://127.0.0.1:11434/api/chat")
                api_key = self._settings.get("llm_api_key", "ollama")
            self.controller.init_bot(
                llm_model=model_name,
                llm_api_key=api_key,
                llm_base_url=base_url,
                voice_api_url="http://127.0.0.1:9880",
                use_voice=True,
            )
            # 恢复未触发的提醒（pending_reminders.json）
            try:
                from bot.skills.reminder import restore_pending_reminders
                restore_pending_reminders()
            except Exception as e:
                self._on_log(f"[提醒] 恢复失败: {e}")
        except Exception as e:
            self._on_log(f"❌ 初始化异常: {e}")

    def _auto_detect_services(self):
        import socket

        stopped = []
        for name, cfg in SERVICE_CONFIGS.items():
            if cfg.port == 0:
                continue
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.2)
                port_ok = s.connect_ex(("127.0.0.1", cfg.port)) == 0
                s.close()
                if port_ok:
                    self.process_mgr._update_state(name, ProcessManager.STATE_RUNNING)
                    self._on_log(f"✅ {cfg.display_name} 已运行 (端口 {cfg.port})")
                else:
                    self.process_mgr._update_state(name, ProcessManager.STATE_STOPPED)
                    stopped.append(name)
            except Exception:
                self.process_mgr._update_state(name, ProcessManager.STATE_STOPPED)
                stopped.append(name)

        # NapCat 已在运行但机器人没起来 → 补拉一次机器人（避免重启后只剩 NapCat 空转）
        if self.napcat_enabled() and "napcat" not in stopped:
            if self.process_mgr._states.get("bot") != ProcessManager.STATE_RUNNING:
                self._on_log("🚀 检测到 NapCat 在运行，补启机器人...")
                QTimer.singleShot(1000, lambda: self.process_mgr.start_service("bot"))

        # 自动补齐未运行的服务（Ollama 已下线；GPT-SoVITS 可选语音）
        if self._settings.get("auto_start", True):
            # GPT-SoVITS 提供达妮娅原声，延迟启动避免抢占资源
            if "gpt_sovits" in stopped:
                QTimer.singleShot(
                    3000, lambda: self.process_mgr.start_service("gpt_sovits"))
            # NapCat 默认不自动启动（需手动开启）

    def cleanup(self):
        self.controller.cleanup()
        self.process_mgr.cleanup()
        # 停主动性巡检
        if getattr(self, "active_monitor", None):
            try:
                self.active_monitor.stop()
            except Exception:
                pass
        # 关闭 MCP 子进程（若已加载）
        try:
            import sys
            if "bot" not in sys.path:
                sys.path.insert(0, os.path.join(TERMINAL_DIR, "bot"))
            import mcp_client
            mcp_client.close_all()
        except Exception:
            pass
        # 退出时断开 WARP（控制台连上的隧道别留在后台）
        try:
            import sys
            _bot = os.path.join(TERMINAL_DIR, "bot")
            if _bot not in sys.path:
                sys.path.insert(0, _bot)
            import warp_control
            warp_control.disconnect_on_exit()
        except Exception as e:
            print(f"[WARP] 退出时断开跳过: {e}")


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = TerminalApp()
    try:
        rc = app.run()
    finally:
        app.cleanup()
    sys.exit(rc)


if __name__ == "__main__":
    main()
