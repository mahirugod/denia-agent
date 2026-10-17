#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅终端 - 主入口
独立桌面应用：现代化终端 UI + LLM 流式对话 + 语音合成 + 悬浮小人
"""
import os
import sys
import json
import signal
import time

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
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from terminal_window import TerminalWindow
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
        self.window = TerminalWindow()
        self.pet = FloatingPet()
        self.controller = ChatController()
        self.process_mgr = ProcessManager()
        self.console_window = None  # Web 控制台（懒加载）

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
        model_name = self._settings.get("model_name", "qwen2.5:3b")
        llm_url = self._settings.get("llm_url", "http://127.0.0.1:11434")
        is_ollama = (
            not model_name.startswith("cloud:")
            and not model_name.startswith("agnes")
            and ("ollama" in llm_url.lower() or "11434" in llm_url)
        )
        self.active_monitor.set_ollama_enabled(is_ollama)

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
        # 同步到 UI 控件
        if hasattr(self.window, "napcat_enabled"):
            self.window.napcat_enabled.setChecked(
                self._settings.get("napcat_enabled", False)
            )
        if hasattr(self.window, "auto_start"):
            self.window.auto_start.setChecked(self._settings.get("auto_start", True))
        # 回填 LLM + 云端配置到 UI
        if hasattr(self.window, "llm_model"):
            self.window.llm_model.setText(self._settings.get("model_name", "qwen2.5:3b"))
        if hasattr(self.window, "llm_url"):
            self.window.llm_url.setText(self._settings.get("llm_url", "http://127.0.0.1:11434/api/chat"))
        if hasattr(self.window, "cloud_api_key"):
            self.window.cloud_api_key.setText(self._settings.get("cloud_api_key", ""))
        if hasattr(self.window, "cloud_base_url"):
            self.window.cloud_base_url.setText(self._settings.get("cloud_base_url", ""))
        if hasattr(self.window, "cloud_model"):
            self.window.cloud_model.setText(self._settings.get("cloud_model", ""))

    def _save_settings(self):
        """保存设置到 settings.json"""
        try:
            data = {
                "napcat_enabled": self.window.napcat_enabled.isChecked(),
                "auto_start": self.window.auto_start.isChecked(),
                "model_name": getattr(self.window, "llm_model", None) and self.window.llm_model.text() or self._settings.get("model_name", "qwen2.5:3b"),
                "llm_url": getattr(self.window, "llm_url", None) and self.window.llm_url.text() or "http://127.0.0.1:11434/api/chat",
                "cloud_api_key": getattr(self.window, "cloud_api_key", None) and self.window.cloud_api_key.text() or "",
                "cloud_base_url": getattr(self.window, "cloud_base_url", None) and self.window.cloud_base_url.text() or "",
                "cloud_model": getattr(self.window, "cloud_model", None) and self.window.cloud_model.text() or "",
            }
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._on_log("💾 设置已保存")
        except Exception as e:
            self._on_log(f"❌ 保存设置失败: {e}")

    def napcat_enabled(self) -> bool:
        """NapCat 是否启用（优先 UI 控件，后 settings.json，默认关闭）"""
        if hasattr(self.window, "napcat_enabled"):
            return self.window.napcat_enabled.isChecked()
        return self._settings.get("napcat_enabled", False)

    def _wire_signals(self):
        w = self.window
        c = self.controller
        pm = self.process_mgr

        # 窗口 → 控制器
        w.chat_message_sent.connect(c.send_message)
        w.voice_input_started.connect(c.start_mic)
        w.voice_input_stopped.connect(c.stop_mic)
        w.voice_input_cancelled.connect(c.cancel_mic)
        w.service_start_requested.connect(self._on_start_all)
        w.service_stop_requested.connect(self._on_stop_all)
        w.service_start_single.connect(self._on_start_single)
        w.service_stop_single.connect(self._on_stop_single)
        w.minimize_to_pet.connect(self._show_pet)
        w.open_console_requested.connect(self.show_console)
        w.settings_save_requested.connect(self._save_settings)

        # 控制器 → 窗口
        c.stream_begin.connect(w.stream_begin)
        c.stream_delta.connect(w.stream_append)
        c.stream_finish.connect(w.stream_finish)
        c.state_changed.connect(self._on_state_changed)
        c.log.connect(self._on_log)
        c.reminder_fired.connect(w.flash_message)
        c.user_message.connect(w._append_user_message)
        # 识别结果由控制器自动发送，不再回填旧输入框；此信号保留用于兼容与日志

        # 悬浮小人
        self.pet.restore_requested.connect(self._restore_window)

        # 进程管理器
        pm.log_received.connect(self._on_log)
        pm.status_changed.connect(self._on_service_status)

        # 提醒卡片定期刷新（30s 一次，倒计时准点变"即将到点"）
        self._reminder_timer = QTimer()
        self._reminder_timer.setInterval(30000)
        self._reminder_timer.timeout.connect(self._refresh_reminder_card)
        self._reminder_timer.start()
        self._refresh_reminder_card()

    def _refresh_reminder_card(self):
        """把待触发提醒推给主窗口卡片"""
        try:
            reminders = self.controller.get_active_reminders()
            self.window.update_reminder_card(reminders)
        except Exception:
            pass

    def _setup_tray(self):
        tray_path = os.path.join(ASSETS_DIR, "tray_dania.png")
        if os.path.exists(tray_path):
            self.app.setWindowIcon(QIcon(tray_path))

    # ---------- Web 控制台 ----------
    def _get_or_create_console(self):
        """懒加载创建 Web 控制台，并把控制器流式信号桥接过去"""
        if self.console_window is None:
            self.console_window = ConsoleWindow(self)
            b = self.console_window.bridge
            c = self.controller
            c.stream_begin.connect(b.chat_started.emit)
            c.stream_delta.connect(b.chat_delta.emit)
            c.reply_ready.connect(b.chat_finished.emit)
            c.tool_event.connect(b.tool_event.emit)
            c.log.connect(b.log.emit)
            c.reminder_fired.connect(b.reminder_fired.emit)
            c.state_changed.connect(b.emotion_changed.emit)
            c.voice_playing.connect(b.voice_playback.emit)
            c.user_message.connect(b.user_message.emit)
            self.weather.weather_ready.connect(
                lambda d: b.weather_updated.emit(json.dumps(d, ensure_ascii=False)))
            self.system_status.system_ready.connect(
                lambda d: b.system_updated.emit(json.dumps(d, ensure_ascii=False)))
        return self.console_window

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
        return result

    def get_settings(self) -> list:
        model_name = self._settings.get("model_name", "qwen2.5:3b")
        options = [
            {"value": "qwen2.5:3b", "label": "本地 qwen2.5:3b"},
            {"value": "agnes-3.0-flash", "label": "云端 agnes-3.0-flash"},
        ]
        # 动态填充：扫描本地 models 文件夹，列出可用模型
        try:
            from bot.skills.model_detect import detect_local_models
            local_models = detect_local_models()
            for m in local_models:
                if not any(o["value"] == m for o in options):
                    options.append({"value": m, "label": f"本地 {m}"})
            # 当前 model_name 不在选项里（如手动改过 settings.json）时补进去
            if model_name and not any(o["value"] == model_name for o in options):
                options.append({"value": model_name, "label": f"本地 {model_name}"})
        except Exception as e:
            print(f"[模型检测] 填充设置页选项失败: {e}")

        # 按当前模型类型动态展示对应配置项（避免一次性堆太多条目）
        # ags 与通用云端（cloud:）同属「云端模型」一组；模型名跟随 model_name 下拉，不重复填
        model_items: list = []
        if model_name.startswith("cloud:") or model_name.startswith("agnes"):
            model_items = [
                {"label": "云端 API Key", "key": "cloud_api_key", "value": self._settings.get("cloud_api_key", ""), "type": "str", "placeholder": "sk-... 或其它 Bearer token"},
                {"label": "云端 Base URL", "key": "cloud_base_url", "value": self._settings.get("cloud_base_url", ""), "type": "str", "placeholder": "如 https://api.openai.com/v1 或 https://dashscope.aliyuncs.com/compatible-mode/v1"},
            ]
        else:
            model_items = [
                {"label": "本地推理后端 Base URL", "key": "llm_url", "value": self._settings.get("llm_url", "http://127.0.0.1:11434/api/chat"), "type": "str", "placeholder": "如 http://127.0.0.1:1234/v1（LM Studio/vLLM 等）；Ollama 默认无需配置"},
                {"label": "本地推理后端 API Key", "key": "llm_api_key", "value": self._settings.get("llm_api_key", "ollama"), "type": "str", "placeholder": "Ollama 默认 ollama，其它后端按需填写"},
            ]
        model_items.append({"label": "提示：切换「LLM 模型」后，配置项会随之变化（云端/本地）", "key": "_model_hint", "value": "", "type": "hint"})

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
                "label": "启用 NapCat（QQ 消息转发）",
                "key": "napcat",
                "value": self.napcat_enabled(),
                "type": "bool",
            },
            {
                "label": "启动时自动检测并补齐服务",
                "key": "autostart",
                "value": self.window.auto_start.isChecked(),
                "type": "bool",
            },
        ]

    def save_setting(self, key: str, value: str):
        self._on_log(f"⚙️ 设置变更: {key} = {value}")
        if key == "napcat":
            self.window.napcat_enabled.setChecked(value.lower() == "true")
            self._save_settings()
        elif key == "autostart":
            self.window.auto_start.setChecked(value.lower() == "true")
            self._save_settings()
        elif key in ("cloud_api_key", "cloud_base_url", "cloud_model",
                      "llm_url", "llm_api_key"):
            # 模型配置项（通用云端 / 本地推理后端）：直接写 settings.json
            # 其中 llm_url / 云端三项 改完需要重新初始化 LLM 才生效
            self._settings[key] = value
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data[key] = value
            try:
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._on_log(f"💾 {key} 已保存")
            except Exception as e:
                self._on_log(f"❌ 保存 {key} 失败: {e}")
            # 影响 LLM 路由/请求的参数，保存后立即重新初始化
            if key in ("llm_url", "llm_api_key", "cloud_api_key", "cloud_base_url"):
                import threading
                threading.Thread(
                    target=lambda: self.controller.reinit_bot(self._do_init_bot),
                    daemon=True,
                ).start()
        elif key == "model_name":
            # 切换 LLM 模型：写 settings.json + 后台重新初始化
            self._settings["model_name"] = value
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data["model_name"] = value
            try:
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self._on_log(f"❌ 保存 model_name 失败: {e}")
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
        self.window.set_avatar_state(state)
        self.pet.set_state(state)

    # ---------- 日志 ----------
    def _on_log(self, text: str):
        print(text, flush=True)
        # 更新首页状态卡片
        if "提醒" in text or "⏰" in text:
            self.window.update_task_card(text)

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
            if self.window.tray_icon and self.window.tray_icon.isVisible():
                msg_type = {
                    "warn": QSystemTrayIcon.MessageIcon.Warning,
                    "info": QSystemTrayIcon.MessageIcon.Information,
                    "ok": QSystemTrayIcon.MessageIcon.Information,
                }.get(severity, QSystemTrayIcon.MessageIcon.Information)
                self.window.tray_icon.showMessage(title, detail, msg_type, 5000)
        except Exception:
            pass

        # 2. 只有 warn 才弹窗（ok 类「已恢复」不打扰用户）
        if severity == "warn":
            try:
                box = QMessageBox(self.window)
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
        # start_all 内部有 time.sleep 和端口等待，放后台线程避免阻塞 UI
        import threading

        skip = [] if self.napcat_enabled() else ["napcat", "bot"]
        # Ollama 为可选本地推理后端：云端/ags 模型无需启动
        model_name = self._settings.get("model_name", "qwen2.5:3b")
        skip_ollama = model_name.startswith("cloud:") or model_name.startswith("agnes")
        threading.Thread(
            target=self.process_mgr.start_all,
            args=(skip,),
            kwargs={"skip_ollama": skip_ollama},
            daemon=True,
        ).start()

    def _on_stop_all(self):
        self._on_log("⏹ 停止所有服务...")
        self.process_mgr.stop_all()

    def _on_start_single(self, service_key: str):
        display_map = {
            "ollama": "Ollama",
            "napcat": "NapCat + QQ 机器人",
            "gpt_sovits": "GPT-SoVITS",
        }
        name = display_map.get(service_key, service_key)
        self._on_log(f"🚀 启动 {name}...")
        # start_service 内部有 time.sleep 和端口等待，放后台线程避免阻塞 UI
        import threading

        def _run():
            self.process_mgr.start_service(service_key)
            if service_key == "napcat":
                import time as _t

                _t.sleep(3)  # 等待 NapCat WebSocket 就绪
                self.process_mgr.start_service("bot")

        threading.Thread(target=_run, daemon=True).start()

    def _on_stop_single(self, service_key: str):
        display_map = {
            "ollama": "Ollama",
            "napcat": "NapCat + QQ 机器人",
            "gpt_sovits": "GPT-SoVITS",
        }
        name = display_map.get(service_key, service_key)
        self._on_log(f"⏹ 停止 {name}...")
        if service_key == "napcat":
            self.process_mgr.stop_service("bot")
        self.process_mgr.stop_service(service_key)

    def _on_service_status(self, service: str, status: str):
        name_map = {
            "ollama": "Ollama",
            "napcat": "NapCat",
            "gpt_sovits": "GPT-SoVITS",
            "bot": "达妮娅机器人",
        }
        display = name_map.get(service, service)
        self.window.update_service_status(display, status)

    # ---------- 悬浮小人 ----------
    def _show_pet(self):
        self.window.hide()
        if self.console_window is not None:
            self.console_window.hide()
        screen = self.app.primaryScreen().geometry()
        x = screen.width() - self.pet.width() - 60
        y = screen.height() - self.pet.height() - 100
        self.pet.move(x, y)
        self.pet.show()
        self.window.tray_icon.showMessage(
            "达妮娅终端",
            "已切换为悬浮小人",
            QSystemTrayIcon.MessageIcon.Information,
            1500,
        )

    def _restore_window(self):
        """小人双击 → 回到 Web 主界面（控制台）"""
        self.pet.hide()
        self.show_console()

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

        print("=" * 50)
        print("✨ 达妮娅终端已启动（Web 主界面）")
        print("   左侧导航：首页/对话/记忆/任务/服务/设置")
        print("   托盘菜单：显示原始终端 / 悬浮小人 / 打开控制台")
        print("=" * 50)

        # 延迟初始化机器人（避免启动卡顿）
        QTimer.singleShot(800, self._lazy_init)

        # 自动检测服务
        QTimer.singleShot(1500, self._auto_detect_services)

        return self.app.exec()

    def _lazy_init(self):
        self._on_log("🤖 正在初始化达妮娅...")
        # 后台线程初始化（涉及网络探测/子进程，避免阻塞 UI）
        import threading

        threading.Thread(target=self._do_init_bot, daemon=True).start()

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
                s.settimeout(0.5)
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

        # 自动补齐未运行的服务（Ollama 仅在本地 Ollama 模型时启动，GPT-SoVITS 可选语音）
        if (
            getattr(self.window, "auto_start", None)
            and self.window.auto_start.isChecked()
        ):
            import threading

            # 仅当当前模型是本地 Ollama 模型时才自动启动 Ollama（云端/ags 模型无需本地推理后端）
            model_name = self._settings.get("model_name", "qwen2.5:3b")
            is_cloud_model = model_name.startswith("cloud:") or model_name.startswith("agnes")
            is_ollama = not is_cloud_model and "ollama" in self._settings.get("llm_url", "http://127.0.0.1:11434").lower()
            if is_ollama and "ollama" in stopped:
                self._on_log("🚀 自动启动本地推理后端 (Ollama)...")
                threading.Thread(
                    target=self.process_mgr.start_service,
                    args=("ollama",),
                    daemon=True,
                ).start()
            elif not is_cloud_model and "ollama" in stopped:
                # 本地模型但 base_url 不是 Ollama → 提示用户确认后端服务已启动
                self._on_log(f"ℹ️ 当前模型 {model_name} 指向非 Ollama 后端（{self._settings.get('llm_url', '默认')}），请确认该服务已运行")
            # GPT-SoVITS 提供达妮娅原声，延迟启动避免抢占资源
            if "gpt_sovits" in stopped:
                QTimer.singleShot(
                    3000,
                    lambda: threading.Thread(
                        target=self.process_mgr.start_service,
                        args=("gpt_sovits",),
                        daemon=True,
                    ).start(),
                )
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


def main():
    # Ollama 模型存储重定向到项目内可写路径（避免默认 D:\ollama_models 权限问题）
    os.environ.setdefault("OLLAMA_MODELS", os.path.join(TERMINAL_DIR, "models"))
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = TerminalApp()
    try:
        rc = app.run()
    finally:
        app.cleanup()
    sys.exit(rc)


if __name__ == "__main__":
    main()
