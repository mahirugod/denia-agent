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

    def _save_settings(self):
        """保存设置到 settings.json"""
        try:
            data = {
                "napcat_enabled": self.window.napcat_enabled.isChecked(),
                "auto_start": self.window.auto_start.isChecked(),
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
        w.voice_input_requested.connect(c.toggle_mic)
        w.history_requested.connect(c.load_history)
        w.backtrack_requested.connect(c.backtrack)
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
        c.history_loaded.connect(w.populate_history)

        # 悬浮小人
        self.pet.restore_requested.connect(self._restore_window)

        # 进程管理器
        pm.log_received.connect(self._on_log)
        pm.status_changed.connect(self._on_service_status)

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
            c.stream_finish.connect(b.chat_finished.emit)
            c.reply_ready.connect(b.chat_finished.emit)
            c.tool_event.connect(b.tool_event.emit)
            c.log.connect(b.log.emit)
            c.reminder_fired.connect(b.reminder_fired.emit)
        return self.console_window

    def show_console(self):
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
        try:
            from memory_store import MemoryStore

            store = MemoryStore()
            result = []
            for f in store._memories.get("facts", []):
                result.append(
                    {
                        "text": f.get("text", ""),
                        "source": f.get("source", "记忆"),
                        "time": time.strftime(
                            "%m-%d", time.localtime(f.get("created", time.time()))
                        ),
                    }
                )
            for k, v in store._memories.get("user_info", {}).items():
                result.append({"text": f"{k}：{v}", "source": "资料", "time": ""})
            return list(reversed(result))[:20]
        except Exception as e:
            return [{"text": f"读取失败: {e}", "source": "错误", "time": ""}]

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
        return [
            {"label": "角色形象", "key": "character", "value": "达妮娅", "type": "str"},
            {"label": "默认缩放", "key": "scale", "value": "0.7x", "type": "str"},
            {"label": "语音合成", "key": "tts", "value": "GPT-SoVITS", "type": "str"},
            {"label": "LLM 模型", "key": "llm", "value": "qwen2.5:7b", "type": "str"},
            {
                "label": "回复长度",
                "key": "reply_len",
                "value": "简短（1-2句）",
                "type": "str",
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
        elif "✨ 达妮娅" in text or "回复" in text:
            self.window.update_memory_card(text)

    # ---------- 服务 ----------
    def _on_start_all(self):
        self._on_log("🚀 启动所有服务...")
        # start_all 内部有 time.sleep 和端口等待，放后台线程避免阻塞 UI
        import threading

        skip = [] if self.napcat_enabled() else ["napcat", "bot"]
        threading.Thread(
            target=self.process_mgr.start_all, args=(skip,), daemon=True
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
            self.controller.init_bot(
                llm_model="qwen2.5:7b",
                llm_api_key="ollama",
                llm_base_url="http://127.0.0.1:11434/api/chat",
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

        # 自动补齐未运行的核心服务（Ollama 必需，GPT-SoVITS 可选语音）
        if (
            getattr(self.window, "auto_start", None)
            and self.window.auto_start.isChecked()
        ):
            import threading

            # Ollama 是对话必需，优先启动
            if "ollama" in stopped:
                self._on_log("🚀 自动启动 Ollama...")
                threading.Thread(
                    target=self.process_mgr.start_service,
                    args=("ollama",),
                    daemon=True,
                ).start()
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
