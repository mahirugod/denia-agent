#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程管理器 - 统一启停 NapCat / GPT-SoVITS / 机器人
- 使用 QProcess 异步管理子进程
- 捕获各进程 stdout/stderr 并通过信号输出日志
- 监控端口状态，检测服务就绪
- 优化：并行启动 + 复用已运行服务
"""
import os
import sys
import time
import signal
import socket
import threading
from typing import Optional, List, Dict

# Windows 控制台默认 GBK 编码，emoji 会崩溃，强制 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal, QTimer

# 项目根目录（process_manager.py 位于根目录，bot/GPT-SoVITS/napcat 均随项目走）
_ROOT = os.path.dirname(os.path.abspath(__file__))

# NapCat 注入参数（v4.18.41 起：NapCatWinBootMain.exe 自带 node 运行时，
# 需传两个位置参数 QQPath + Hook DLL，并由环境变量告知补丁包/加载脚本/主脚本路径，
# 详见 napcat/launcher-user.bat）
_QQNT_PATH = os.environ.get("DANIA_QQNT", "D:\\QQNT\\QQ.exe")
_NAPCAT_DIR = os.path.join(_ROOT, "napcat")
_NAPCAT_BOOT_EXE = os.path.join(_NAPCAT_DIR, "NapCatWinBootMain.exe")
_NAPCAT_HOOK_DLL = os.path.join(_NAPCAT_DIR, "NapCatWinBootHook.dll")
_NAPCAT_LOAD_JS = os.path.join(_NAPCAT_DIR, "loadNapCat.js")
_NAPCAT_MAIN_MJS = os.path.join(_NAPCAT_DIR, "napcat.mjs")
_NAPCAT_QQNT_JSON = os.path.join(_NAPCAT_DIR, "qqnt.json")


def _napcat_quick_uin() -> str:
    """读取 settings.json 的 napcat_qq_uin，用于 NapCat 启动时 -q 快速登录"""
    try:
        import json as _json
        with open(os.path.join(_ROOT, "settings.json"), encoding="utf-8") as f:
            val = str(_json.load(f).get("napcat_qq_uin", "") or "").strip()
        return val if val.isdigit() else ""
    except Exception:
        return ""


# 各服务配置
class ServiceConfig:
    def __init__(self, name: str, display_name: str, command: str,
                 args: List[str], cwd: str, port: int = 0,
                 startup_wait: int = 5, critical: bool = True):
        self.name = name              # 内部名: napcat / gpt_sovits / bot
        self.display_name = display_name  # 显示名
        self.command = command
        self.args = args
        self.cwd = cwd
        self.port = port              # 0 = 不检查端口
        self.startup_wait = startup_wait  # 启动后等待秒数
        self.critical = critical      # 是否关键服务（失败会报警）


# 各服务配置（Ollama 已下线：只走 agnes 云端一条路，不再作为启停对象）
SERVICE_CONFIGS = {
    "napcat": ServiceConfig(
        name="napcat",
        display_name="NapCat",
        command=_NAPCAT_BOOT_EXE,
        # v4.18.41 起必须传 QQPath + Hook DLL 两个位置参数，否则启动器直接退出、无任何窗口
        args=[_QQNT_PATH, _NAPCAT_HOOK_DLL],
        cwd=_NAPCAT_DIR,
        port=3000,
        startup_wait=10,
        critical=True,
    ),
    "gpt_sovits": ServiceConfig(
        name="gpt_sovits",
        display_name="GPT-SoVITS 语音",
        command=os.path.join(_ROOT, "GPT-SoVITS", "runtime", "python.exe"),
        args=["api_v2.py"],
        cwd=os.path.join(_ROOT, "GPT-SoVITS"),
        port=9880,
        startup_wait=50,  # GPU 模式首次加载需初始化 CUDA，比 CPU 慢，留足余量
        critical=False,
    ),
    "bot": ServiceConfig(
        name="bot",
        display_name="达妮娅机器人",
        command=sys.executable,
        args=["-u", "napcat_adapter_ws.py"],
        cwd=os.path.join(_ROOT, "bot"),
        port=0,  # 机器人不监听端口
        startup_wait=3,
        critical=True,
    ),
}


class ProcessManager(QObject):
    """子进程管理器"""

    # 信号
    log_received = Signal(str)          # 日志输出
    service_started = Signal(str)       # 服务名
    service_stopped = Signal(str)       # 服务名
    service_failed = Signal(str, str)   # 服务名 + 错误信息
    all_started = Signal()              # 全部启动完成
    all_stopped = Signal()              # 全部停止
    status_changed = Signal(str, str)   # 服务名 + 新状态
    status_probe_done = Signal(dict)    # 后台端口探测结果（服务名 -> 是否被占用）

    # 状态常量
    STATE_STOPPED = "stopped"
    STATE_STARTING = "starting"
    STATE_RUNNING = "running"
    STATE_FAILED = "failed"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._processes: Dict[str, QProcess] = {}
        self._states: Dict[str, str] = {}
        self._retry_counts: Dict[str, int] = {}  # 服务重试次数
        self._max_retries: int = 3  # 最大重试次数
        self._pending_stops: Dict[str, bool] = {}  # 正在停止的服务
        self._probe_busy = False                   # 后台端口探测是否进行中
        self.status_probe_done.connect(self._apply_status_probe)
        self._init_status_timer()

        # 初始化所有服务状态
        for name in SERVICE_CONFIGS:
            self._states[name] = self.STATE_STOPPED
            self._retry_counts[name] = 0
            self._pending_stops[name] = False

    def _init_status_timer(self):
        """定时检查服务状态（优化：降低轮询频率）"""
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(10000)  # 10 秒检查一次（原 5 秒）
        self._status_timer.timeout.connect(self._check_all_status)
        self._status_timer.start()

    def _is_port_free(self, port: int, timeout: float = 0.5) -> bool:
        """检查端口是否空闲（True=空闲，False=被占用）

        注意：本机防火墙对未监听端口会静默丢包，connect 要等满超时才返回。
        这里默认 0.5s，调用方可按场景再调小，避免阻塞 GUI 线程。
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex(("127.0.0.1", port)) == 0
            sock.close()
            return not result
        except Exception:
            return True

    def _check_existing_service(self, port: int, display_name: str) -> bool:
        """检查端口上是否已有服务（端口被占用即认为有服务，不杀进程）"""
        if port == 0:
            return False
        # _is_port_free 返回 True=空闲，所以"有服务"=not 空闲
        return not self._is_port_free(port)

    def _kill_port_if_busy(self, port: int, display_name: str):
        """**跳过**：不再自动杀进程，避免误杀已有服务"""
        # 已禁用自动杀进程功能，由用户手动管理
        pass

    def _check_all_status(self):
        """定时巡检：端口探测放后台线程，避免 connect 超时把 GUI 卡死（按钮失灵）"""
        # 无端口服务（bot）只需看进程是否还活着
        for name, config in SERVICE_CONFIGS.items():
            if config.port > 0 or self._pending_stops.get(name, False):
                continue
            proc = self._processes.get(name)
            if proc and proc.state() != QProcess.ProcessState.Running:
                if self._states.get(name) == self.STATE_RUNNING:
                    self._update_state(name, self.STATE_STOPPED)
                    self._try_auto_restart(name, "已掉线")

        # 端口服务交给后台线程探测，结果通过信号回到 GUI 线程处理
        targets = {
            name: config for name, config in SERVICE_CONFIGS.items()
            if config.port > 0
            and not self._pending_stops.get(name, False)
            and name in self._processes
        }
        if not targets or self._probe_busy:
            return
        self._probe_busy = True

        def _probe():
            result = {n: not self._is_port_free(c.port) for n, c in targets.items()}
            self.status_probe_done.emit(result)

        threading.Thread(target=_probe, daemon=True).start()

    def _apply_status_probe(self, in_use: Dict[str, bool]):
        """GUI 线程：应用后台探测到的端口占用结果"""
        self._probe_busy = False
        for name, busy in in_use.items():
            config = SERVICE_CONFIGS.get(name)
            if config is None or self._pending_stops.get(name, False):
                continue
            current_state = self._states.get(name, self.STATE_STOPPED)
            if busy and current_state != self.STATE_RUNNING:
                self._update_state(name, self.STATE_RUNNING)
                self._retry_counts[name] = 0
            elif not busy and current_state == self.STATE_RUNNING:
                self._update_state(name, self.STATE_STOPPED)
                self._try_auto_restart(name, f"端口 {config.port} 失联")

    def _try_auto_restart(self, name: str, reason: str):
        """掉线自动重启（限制重试次数）"""
        config = SERVICE_CONFIGS[name]
        if self._retry_counts.get(name, 0) >= self._max_retries:
            self.log_received.emit(
                f"⚠️  {config.display_name} 重启次数已达上限 ({self._max_retries})，请手动检查")
            return
        self._retry_counts[name] = self._retry_counts.get(name, 0) + 1
        self.log_received.emit(
            f"🔄 {config.display_name} {reason}，自动重启 "
            f"({self._retry_counts[name]}/{self._max_retries})...")
        QTimer.singleShot(2000, lambda n=name: self._auto_restart(n))

    def _auto_restart(self, name: str):
        """自动重启指定服务（非阻塞方式）；重启前再次确认端口确实空闲"""
        if name not in self._processes:
            return
        config = SERVICE_CONFIGS[name]
        # 重启前二次确认：如果端口被占用，说明服务还活着，不杀
        if config.port > 0 and not self._is_port_free(config.port):
            self.log_received.emit(f"  ℹ️ {config.display_name} 端口仍在监听，跳过重启")
            self._update_state(name, self.STATE_RUNNING)
            self._retry_counts[name] = 0
            return
        self.log_received.emit(f"🔧 正在重新启动 {config.display_name}...")
        # 非阻塞停止：先标记，再异步停止
        self._pending_stops[name] = True
        process = self._processes.get(name)
        if process:
            process.terminate()
            # 延迟停止后重启
            QTimer.singleShot(1000, lambda n=name: self._restart_service(n))

    def _restart_service(self, name: str):
        """实际执行重启"""
        self._pending_stops[name] = False
        # 清理旧进程
        process = self._processes.get(name)
        if process:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
            self._processes.pop(name, None)

        # 清理端口
        config = SERVICE_CONFIGS[name]
        if config.port > 0:
            self._kill_port_if_busy(config.port, config.display_name)

        # 重新启动
        self.start_service(name)

    def _update_state(self, name: str, state: str):
        """更新服务状态"""
        old = self._states.get(name)
        if old == state:
            return
        self._states[name] = state
        try:
            self.status_changed.emit(name, state)
            config = SERVICE_CONFIGS[name]
            self.log_received.emit(f"[{config.display_name}] 状态: {state}")
        except RuntimeError:
            pass  # 信号源已删除，忽略

    def get_service_status(self, name: str) -> str:
        """获取服务状态"""
        return self._states.get(name, self.STATE_STOPPED)

    def get_all_status(self) -> Dict[str, str]:
        """获取所有服务状态"""
        return dict(self._states)

    def start_service(self, name: str) -> bool:
        """启动单个服务"""
        if name not in SERVICE_CONFIGS:
            return False

        config = SERVICE_CONFIGS[name]
        self.log_received.emit(f"▶️  正在启动 {config.display_name}...")
        self._update_state(name, self.STATE_STARTING)

        # 如果已在运行，强制清理旧进程
        if name in self._processes:
            old_proc = self._processes.pop(name)
            self._pending_stops[name] = False
            # 断开旧进程的所有信号，避免删除后触发
            try:
                old_proc.readyReadStandardOutput.disconnect()
                old_proc.readyReadStandardError.disconnect()
                old_proc.finished.disconnect()
                old_proc.errorOccurred.disconnect()
            except Exception:
                pass
            if old_proc.state() != QProcess.ProcessState.NotRunning:
                old_proc.kill()
            old_proc.deleteLater()

        # 端口预检查：如果端口被占用 → 说明已有服务，直接复用
        if config.port > 0:
            port_in_use = not self._is_port_free(config.port, 0.4)  # 取反：free=False 表示被占用
            if port_in_use:
                self._update_state(name, self.STATE_RUNNING)
                self._retry_counts[name] = 0
                self.log_received.emit(f"  ✅ 已存在可用的 {config.display_name} 实例，直接复用")
                return True
            # 端口空闲，正常启动

        # NapCat 注入只在 QQ 启动瞬间生效：
        # - 若当前 QQ 是"注入版 NapCat"（3000/6099 已监听）→ 放行，可继续复用/切号
        # - 若当前 QQ 是"裸 QQ"（非 NapCat 拉起）→ 仍放行：napimain.exe 会拉起
        #   第二个 QQNT 实例并注入（QQNT 支持多开），两个 QQ 实例可并存。
        #   用户通过 WebUI 切号即可，无需手动退出裸 QQ。
        #   注入失败时 napimain.exe 日志会显示（QProcess 已转发 stdout/stderr）。
        if name == "napcat" and self._qq_running() and not self._napcat_injected():
            self.log_received.emit(
                "ℹ️  检测到裸 QQ 在运行：将拉起第二个 QQNT 实例并注入 NapCat，"
                "两个 QQ 可并存。如需注入到裸 QQ，请完全退出后重启。")

        # 创建并启动进程
        process = self._build_process(name, config)
        self._processes[name] = process
        process.start()

        # 就绪检测改为 QTimer 异步轮询：原来这里同步 sleep 等端口，
        # GPT-SoVITS 启动要 50s，会把整个界面卡死（表现为按钮失灵）
        if config.port > 0:
            # NapCat 要等用户扫码登录后才监听 3000，给足 3 分钟
            wait = 180 if name == "napcat" else 60
            QTimer.singleShot(
                1000, lambda: self._schedule_ready_check(name, config, 0, wait))
        else:
            QTimer.singleShot(int(max(config.startup_wait, 0) * 1000),
                              lambda n=name: self._mark_ready(n))
        return True

    def _build_process(self, name: str, config) -> QProcess:
        """创建服务 QProcess（含环境变量与信号绑定）"""
        process = QProcess(self)
        process.setWorkingDirectory(config.cwd)
        process.setProgram(config.command)
        args = list(config.args)
        # NapCat 支持 -q <uin> 直接快速登录，省掉每次扫码；账号从 settings.json 读取
        if name == "napcat":
            uin = _napcat_quick_uin()
            if uin:
                args += ["-q", uin]
        process.setArguments(args)

        # 环境变量（强制 UTF-8 输出，避免 Windows GBK 乱码）
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("LANG", "en_US.UTF-8")
        # NapCat v4.18.41 注入链路所需环境变量（对齐 launcher-user.bat L5-L9）
        if name == "napcat":
            env.insert("NAPCAT_PATCH_PACKAGE", _NAPCAT_QQNT_JSON)
            env.insert("NAPCAT_LOAD_PATH", _NAPCAT_LOAD_JS)
            env.insert("NAPCAT_INJECT_PATH", _NAPCAT_HOOK_DLL)
            env.insert("NAPCAT_LAUNCHER_PATH", _NAPCAT_BOOT_EXE)
            env.insert("NAPCAT_MAIN_PATH", _NAPCAT_MAIN_MJS)
        process.setProcessEnvironment(env)

        process.readyReadStandardOutput.connect(
            lambda p=process, n=name: self._read_stdout(p, n))
        process.readyReadStandardError.connect(
            lambda p=process, n=name: self._read_stderr(p, n))
        process.finished.connect(
            lambda exit_code, exit_status, n=name: self._on_process_finished(n, exit_code))
        process.errorOccurred.connect(
            lambda err, n=name: self._on_process_error(n, err))
        return process

    def start_all(self, skip: List[str] = None):
        """并行启动所有服务（优化：不再串行等待）

        Args:
            skip: 要跳过的服务名列表（如 ["napcat", "bot"]）
        """
        skip = set(skip or [])
        self.log_received.emit("=" * 50)
        self.log_received.emit("🚀 开始启动所有服务...")

        # 先检查哪些服务已经在运行
        for name, config in SERVICE_CONFIGS.items():
            if config.port > 0:
                port_in_use = not self._is_port_free(config.port, 0.3)  # free=False 表示被占用
                if port_in_use:
                    self.log_received.emit(f"  ✅ {config.display_name} 已在运行")

        # 关键服务串行启动（避免端口检测冲突）
        self.log_received.emit("⚠️  有服务未启动，正在自动补齐...")

        # 1. NapCat + QQ 机器人（可选，取决于用户开关；bot 依赖 NapCat，随之启动）
        # 启动顺序与间隔保留原语义，但改成 QTimer 串联，不再 sleep 阻塞 UI
        steps = []
        if "napcat" not in skip:
            steps.append(("napcat", 3000))
            if "bot" not in skip:
                steps.append(("bot", 1000))

        # 2. 语音服务（非关键，异步就绪检测已经不会卡界面）
        if "gpt_sovits" not in skip:
            self.start_service("gpt_sovits")

        self._run_start_steps(steps, 0, skip)

    def _run_start_steps(self, steps: list, idx: int, skip: set):
        """按顺序启动服务，每步用 QTimer 延后，避免阻塞 GUI 线程"""
        if idx >= len(steps):
            self._watch_all_ready(skip, time.time() + 15)
            return
        name, delay = steps[idx]
        self.start_service(name)
        QTimer.singleShot(delay, lambda: self._run_start_steps(steps, idx + 1, skip))

    def _watch_all_ready(self, skip: set, deadline: float):
        """轮询关键服务是否就绪（原 15 秒同步等待的异步版）"""
        ok_napcat = "napcat" in skip or self._states.get("napcat") == self.STATE_RUNNING
        ok_bot = "bot" in skip or self._states.get("bot") == self.STATE_RUNNING
        if ok_napcat and ok_bot:
            self.log_received.emit("✅ 所有服务已就绪")
            self.log_received.emit("=" * 50)
            return
        if time.time() >= deadline:
            self.log_received.emit("⚠️  部分服务启动超时")
            self.log_received.emit("=" * 50)
            return
        QTimer.singleShot(1000, lambda: self._watch_all_ready(skip, deadline))

    def stop_service(self, name: str) -> bool:
        """停止单个服务（非阻塞方式）"""
        if name not in SERVICE_CONFIGS:
            return False
        config = SERVICE_CONFIGS[name]

        # NapCat 特殊处理：注入版 QQNT 不是 QProcess 直接拉起的（由
        # NapCatWinBootMain.exe 拉起并注入 napiloader.dll），QProcess.kill()
        # 只能杀 NapCatWinBootMain.exe，杀不到被注入的 QQNT。
        # 需额外调 WebUI 退出 / 提示用户手动退出 QQNT（裸 QQ 不受影响，可并存）。
        if name == "napcat":
            if name in self._processes:
                self.log_received.emit("⏹  正在停止 NapCat 注入器...")
                process = self._processes.pop(name, None)
                if process:
                    try:
                        process.terminate()
                        QTimer.singleShot(3000, lambda: self._force_kill_if_needed("napcat"))
                    except Exception:
                        pass
            # 提醒用户退出注入版 QQNT（裸 QQ 不受影响）
            self.log_received.emit(
                "⚠️  NapCat 注入版 QQNT 不会自动退出，请在注入版 QQ 窗口手动"
                "「退出登录」或关闭窗口。NapCatWinBootMain.exe 已被终止。"
                "（裸 QQ 不受影响，可继续使用）")
            self._update_state("napcat", self.STATE_STOPPED)
            self.service_stopped.emit("napcat")
            return True

        if name not in self._processes:
            # 外部启动/复用的服务不在本应用进程表里，不能替用户杀
            if config.port > 0 and not self._is_port_free(config.port):
                self.log_received.emit(
                    f"ℹ️ {config.display_name} 不是本应用启动的，无法从这里停止")
            return True

        process = self._processes[name]

        self.log_received.emit(f"⏹  正在停止 {config.display_name}...")
        self._pending_stops[name] = True

        # 连接 finished 信号来清理
        process.finished.connect(
            lambda exit_code, exit_status, n=name: self._on_service_stopped(n))

        # 尝试优雅终止
        process.terminate()

        # 3秒后如果还在运行，强制 kill
        QTimer.singleShot(3000, lambda n=name: self._force_kill_if_needed(n))

        return True

    def _force_kill_if_needed(self, name: str):
        """强制终止仍在运行的服务"""
        if name not in self._processes:
            return
        process = self._processes[name]
        if process.state() != QProcess.ProcessState.NotRunning:
            config = SERVICE_CONFIGS[name]
            self.log_received.emit(f"   {config.display_name} 3 秒内未退出，强制终止")
            process.kill()

    def _on_service_stopped(self, name: str):
        """服务停止完成"""
        try:
            if name not in self._processes:
                return
            config = SERVICE_CONFIGS[name]
            self._processes.pop(name, None)
            self._pending_stops[name] = False
            self._update_state(name, self.STATE_STOPPED)
            self.log_received.emit(f"✅ {config.display_name} 已停止")
            self.service_stopped.emit(name)
        except RuntimeError:
            pass  # 信号源已删除，忽略

    def stop_all(self):
        """停止所有服务（逆序，非阻塞）"""
        self.log_received.emit("=" * 50)
        self.log_received.emit("⏹  正在停止所有服务...")

        # 逆序停止，但不等待（Ollama 已下线）
        order = ["bot", "gpt_sovits", "napcat"]
        for name in order:
            if name in self._processes:
                self.stop_service(name)

        # 延迟发送 all_stopped 信号
        QTimer.singleShot(4000, self._check_all_stopped)

    def _check_all_stopped(self):
        """检查是否所有服务都已停止"""
        if not self._processes:
            self.log_received.emit("✅ 所有服务已停止")
            self.log_received.emit("=" * 50)
            self.all_stopped.emit()

    def is_any_running(self) -> bool:
        """是否有任何服务在运行"""
        return any(s == self.STATE_RUNNING for s in self._states.values())

    def napcat_switch_qq(self, uin: int) -> bool:
        """对外入口：通过 NapCat WebUI 在线切号到指定 QQ 号（无需重启 QQ）

        前提：NapCat 注入版 QQ 已运行且 WebUI（6099）在监听、QQ 处于未登录态。
        返回 True=调用成功，False=失败（裸 QQ / WebUI 未起 / 已登录等）。
        """
        if not self._napcat_injected():
            self.log_received.emit(
                "❌ 无法切号：NapCat 未注入运行（3000/6099 均未监听）。"
                "请先完全退出裸 QQ，再点启动由 NapCat 拉起并扫码登录。")
            return False
        return self._webui_login(uin)

    def _schedule_ready_check(self, name: str, config: ServiceConfig,
                              attempt: int = 0, timeout: int = 60):
        """异步轮询端口是否就绪（不阻塞 GUI 线程，原同步 sleep 版本会卡死界面）"""
        if config.port <= 0:
            return
        # 进程已被停止/崩溃移除 → 放弃检测
        if name not in self._processes:
            return
        # 端口已监听 → 就绪（本地端口超时给 0.2s，避免轮询卡界面）
        if not self._is_port_free(config.port, 0.2):
            self._mark_ready(name)
            return
        if timeout <= 0:
            # NapCat 超时不重试：重拉只会再开一个未注入的新 QQ 实例
            if name == "napcat":
                self.log_received.emit(
                    f"⚠️  {config.display_name} 端口 {config.port} 未就绪，"
                    "请在 QQ 窗口完成扫码登录")
                self._update_state(name, self.STATE_FAILED)
                self._napcat_qq_hint()
                return
            # 首次超时 → 重试一次
            if attempt < 1:
                self.log_received.emit(
                    f"🔄 {config.display_name} 启动重试 ({attempt + 1}/1)...")
                self._respawn(name, config, attempt)
            else:
                self.log_received.emit(
                    f"⚠️  {config.display_name} 端口 {config.port} 未能就绪 (已重试 1 次)")
                self._update_state(name, self.STATE_FAILED)
                if name == "napcat":
                    self._napcat_qq_hint()
            return
        QTimer.singleShot(
            1000,
            lambda: self._schedule_ready_check(name, config, attempt, timeout - 1))

    def _respawn(self, name: str, config: ServiceConfig, attempt: int):
        """杀掉未就绪的旧进程，2 秒后重新启动并再次轮询端口"""
        old = self._processes.pop(name, None)
        if old:
            try:
                old.readyReadStandardOutput.disconnect()
                old.readyReadStandardError.disconnect()
                old.finished.disconnect()
                old.errorOccurred.disconnect()
            except Exception:
                pass
            if old.state() != QProcess.ProcessState.NotRunning:
                old.kill()
                old.waitForFinished(3000)
            old.deleteLater()

        def _restart():
            # 期间被用户停止 / 已由其它路径启动 → 不再重启
            if self._pending_stops.get(name, False) or name in self._processes:
                return
            if config.port > 0 and not self._is_port_free(config.port):
                self._mark_ready(name)
                return
            process = self._build_process(name, config)
            self._processes[name] = process
            process.start()
            wait = 180 if name == "napcat" else 60
            self._schedule_ready_check(name, config, attempt + 1, timeout=wait)

        QTimer.singleShot(2000, _restart)

    def _mark_ready(self, name: str):
        """服务就绪：更新状态并记录日志"""
        config = SERVICE_CONFIGS[name]
        self._retry_counts[name] = 0
        self._update_state(name, self.STATE_RUNNING)
        if config.port > 0:
            self.log_received.emit(f"✅ {config.display_name} 就绪 (端口 {config.port})")
        else:
            self.log_received.emit(f"✅ {config.display_name} 已启动")

    def _qq_running(self) -> bool:
        """检查本机是否已有 QQ 进程在运行（QQNT 是多进程，多个 QQ.exe 都算"在跑"）"""
        try:
            import subprocess
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                capture_output=True, timeout=3, creationflags=flags,
                text=True, encoding="utf-8", errors="ignore")
            return "qq.exe" in (out.stdout or "").lower()
        except Exception:
            return False

    def _napcat_injected(self) -> bool:
        """判断当前 QQ 是否由 NapCat 注入器拉起（区别于裸启动的 QQNT）

        依据：NapCat 注入版 QQ 就绪后会监听 3000（OneBot API）与 6099（WebUI）。
        两者只要有一个在监听，就说明这是"注入版"而非"裸 QQ"。
        """
        return not self._is_port_free(3000, 0.3) or not self._is_port_free(6099, 0.3)

    def _webui_login(self, uin: int) -> bool:
        """通过 NapCat WebUI（6099）在线切号到指定 QQ 号

        v4.18.41 鉴权为两步：
          1) POST /api/auth/login {"hash": sha256(token + ".napcat")} → Credential
          2) POST /api/QQLogin/SetQuickLogin  Authorization: Bearer <Credential>
        返回 True=调用成功（是否真正登成功以 WebUI 日志为准），False=失败。
        """
        import hashlib, json, urllib.request
        token = ""
        try:
            cfg = os.path.join(_ROOT, "napcat", "config", "webui.json")
            with open(cfg, encoding="utf-8") as f:
                token = (json.load(f) or {}).get("token", "")
        except Exception:
            pass
        if not token:
            self.log_received.emit("  [NapCat] WebUI 切号失败：未读到 webui.json 的 token")
            return False
        base = "http://127.0.0.1:6099"
        try:
            h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
            req = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"hash": h}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode() or "{}")
            cred = (payload.get("data") or {}).get("Credential")
            if not cred:
                self.log_received.emit(f"  [NapCat] WebUI 切号失败：鉴权无 Credential {payload}")
                return False
            req2 = urllib.request.Request(
                base + "/api/QQLogin/SetQuickLogin",
                data=json.dumps({"uin": uin}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + cred})
            with urllib.request.urlopen(req2, timeout=8) as resp:
                data = json.loads(resp.read().decode() or "{}")
            ok = data.get("code") == 0
            self.log_received.emit(
                f"  [NapCat] WebUI 切号 {uin} -> {'成功' if ok else '失败: ' + str(data)}")
            return ok
        except Exception as e:
            self.log_received.emit(f"  [NapCat] WebUI 切号 {uin} 调用失败：{e}")
            return False

    def _napcat_qq_hint(self):
        """NapCat 未就绪时的提示：区分"裸 QQ"和"注入版 NapCat"两种情况"""
        if self._qq_running():
            if self._napcat_injected():
                self.log_received.emit(
                    "ℹ️ NapCat 注入版 QQ 已运行，3000 尚未监听：请在 QQ 窗口完成扫码登录；"
                    "登录后即可反复通过 WebUI 切号（无需重启 QQ）")
            else:
                self.log_received.emit(
                    "ℹ️ 检测到裸 QQ 在运行（非 NapCat 注入）：注入仅在 QQ 启动瞬间生效，"
                    "请完全退出 QQ（所有 QQ 进程）后重新点启动，由 NapCat 启动器拉起并注入")

    def _read_stdout(self, process: QProcess, name: str):
        """读取标准输出"""
        data = bytes(process.readAllStandardOutput())
        text = data.decode("utf-8", errors="ignore").strip()
        if text:
            config = SERVICE_CONFIGS[name]
            # 只输出关键信息（过滤掉重复的启动日志）
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # NapCat 启动器会刷几百行 Hook 调试日志，只保留有意义的行
                if name == "napcat" and not any(
                        k in line for k in ("[NapCat]", "[error]", "✓", "成功", "失败",
                                            "QQ版本", "WebUi", "正在启动", "二维码")):
                    continue
                self.log_received.emit(f"  [{config.display_name}] {line[:120]}")

    def _read_stderr(self, process: QProcess, name: str):
        """读取标准错误"""
        data = bytes(process.readAllStandardError())
        text = data.decode("utf-8", errors="ignore").strip()
        if text:
            config = SERVICE_CONFIGS[name]
            for line in text.split("\n"):
                line = line.strip()
                if line and ("warning" in line.lower() or "error" in line.lower() or "traceback" in line.lower()):
                    self.log_received.emit(f"  [{config.display_name}] ⚠️ {line[:120]}")

    def _on_process_finished(self, name: str, exit_code: int):
        """进程结束"""
        try:
            if name not in self._processes:
                return
            config = SERVICE_CONFIGS[name]
            self.log_received.emit(f"[{config.display_name}] 进程结束 (exit={exit_code})")
            self._processes.pop(name, None)
            self._pending_stops[name] = False  # 已结束，不能残留"正在停止"标记
            self._update_state(name, self.STATE_STOPPED)
            self.service_stopped.emit(name)
        except RuntimeError:
            pass  # 信号源已删除，忽略

    def _on_process_error(self, name: str, error: QProcess.ProcessError):
        """进程错误"""
        try:
            if name not in self._processes:
                return  # 已处理过，跳过重复信号
            config = SERVICE_CONFIGS[name]
            error_map = {
                QProcess.ProcessError.FailedToStart: "无法启动",
                QProcess.ProcessError.Crashed: "崩溃",
                QProcess.ProcessError.Timedout: "超时",
                QProcess.ProcessError.WriteError: "写入错误",
                QProcess.ProcessError.ReadError: "读取错误",
                QProcess.ProcessError.UnknownError: "未知错误",
            }
            error_msg = error_map.get(error, str(error))
            self.log_received.emit(f"[{config.display_name}] ❌ {error_msg}")
            self._processes.pop(name, None)  # 防止后续 finished 重复触发
            self._pending_stops[name] = False
            self._update_state(name, self.STATE_FAILED)
            self.service_failed.emit(name, error_msg)
        except RuntimeError:
            pass  # 信号源已删除，忽略

    def cleanup(self):
        """清理所有进程（程序退出时调用）"""
        self.stop_all()
