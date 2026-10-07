#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程管理器 - 统一启停 Ollama / NapCat / GPT-SoVITS / 机器人
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


# 服务配置
class ServiceConfig:
    def __init__(self, name: str, display_name: str, command: str,
                 args: List[str], cwd: str, port: int = 0,
                 startup_wait: int = 5, critical: bool = True):
        self.name = name              # 内部名: ollama / napcat / gpt_sovits / bot
        self.display_name = display_name  # 显示名
        self.command = command
        self.args = args
        self.cwd = cwd
        self.port = port              # 0 = 不检查端口
        self.startup_wait = startup_wait  # 启动后等待秒数
        self.critical = critical      # 是否关键服务（失败会报警）


# 各服务配置
SERVICE_CONFIGS = {
    "ollama": ServiceConfig(
        name="ollama",
        display_name="Ollama",
        command=r"D:\Ollama\ollama.exe",
        args=["serve"],
        cwd=r"D:\Ollama",
        port=11434,
        startup_wait=5,
        critical=True,
    ),
    "napcat": ServiceConfig(
        name="napcat",
        display_name="NapCat",
        command="cmd",
        args=["/c", "start", "/min", "/b", "start.bat"],  # 后台静默启动
        cwd=os.path.join(_ROOT, "napcat"),
        port=3000,
        startup_wait=10,
        critical=True,
    ),
    "gpt_sovits": ServiceConfig(
        name="gpt_sovits",
        display_name="GPT-SoVITS 语音",
        command=os.path.join(_ROOT, "GPT-SoVITS", "runtime", "python.exe"),
        args=["-I", "api_v2.py"],
        cwd=os.path.join(_ROOT, "GPT-SoVITS"),
        port=9880,
        startup_wait=25,  # CPU 模式加载模型约 23 秒，留余量
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

    def _is_port_free(self, port: int) -> bool:
        """检查端口是否空闲（True=空闲，False=被占用）

        用 connect_ex + 宽松超时，避免 Ollama/GPT-SoVITS 偶尔慢响应导致误判"空闲"
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
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
        """检查所有服务的端口是否存活，掉线时自动重试（优化：降低频率）"""
        # 跳过正在停止的服务
        for name, config in SERVICE_CONFIGS.items():
            if self._pending_stops.get(name, False):
                continue

            # 只管理自己启动的进程
            proc = self._processes.get(name)
            if not proc:
                # 没有进程，说明是复用的外部服务，不做状态检测
                continue

            if config.port == 0:
                # 无端口服务，检查进程是否还在
                if proc.state() != QProcess.ProcessState.Running:
                    if self._states.get(name) == self.STATE_RUNNING:
                        self._update_state(name, self.STATE_STOPPED)
                        # 进程掉线，自动重启（限重试次数）
                        if self._retry_counts.get(name, 0) < self._max_retries:
                            self._retry_counts[name] = self._retry_counts.get(name, 0) + 1
                            self.log_received.emit(f"🔄 {config.display_name} 已掉线，自动重启 ({self._retry_counts[name]}/{self._max_retries})...")
                            QTimer.singleShot(2000, lambda n=name: self._auto_restart(n))
                continue

            # 检查端口是否被占用（_is_port_free 返回 True=空闲/False=被占用）
            port_is_free = self._is_port_free(config.port)
            port_in_use = not port_is_free
            current_state = self._states.get(name, self.STATE_STOPPED)

            if port_in_use and current_state == self.STATE_RUNNING:
                # 端口被占用且状态已是 running → 无需操作
                pass
            elif port_in_use and current_state != self.STATE_RUNNING:
                # 端口被占用但状态不是 running → 更新为 running
                self._update_state(name, self.STATE_RUNNING)
                self._retry_counts[name] = 0
            elif not port_in_use and current_state == self.STATE_RUNNING:
                # 端口空闲了 → 服务掉线
                self._update_state(name, self.STATE_STOPPED)
                # 自动重启（限重试次数）
                if self._retry_counts.get(name, 0) < self._max_retries:
                    self._retry_counts[name] = self._retry_counts.get(name, 0) + 1
                    self.log_received.emit(f"🔄 {config.display_name} 端口 {config.port} 失联，自动重启 ({self._retry_counts[name]}/{self._max_retries})...")
                    QTimer.singleShot(2000, lambda n=name: self._auto_restart(n))
                else:
                    self.log_received.emit(f"⚠️  {config.display_name} 重启次数已达上限 ({self._max_retries})，请手动检查")

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
            port_in_use = not self._is_port_free(config.port)  # 取反：free=False 表示被占用
            if port_in_use:
                self._update_state(name, self.STATE_RUNNING)
                self._retry_counts[name] = 0
                self.log_received.emit(f"  ✅ 已存在可用的 {config.display_name} 实例，直接复用")
                return True
            # 端口空闲，正常启动

        # 创建 QProcess
        process = QProcess(self)
        process.setWorkingDirectory(config.cwd)
        process.setProgram(config.command)
        process.setArguments(config.args)

        # 环境变量（强制 UTF-8 输出，避免 Windows GBK 乱码）
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("LANG", "en_US.UTF-8")
        process.setProcessEnvironment(env)

        # 连接信号
        process.readyReadStandardOutput.connect(
            lambda p=process, n=name: self._read_stdout(p, n))
        process.readyReadStandardError.connect(
            lambda p=process, n=name: self._read_stderr(p, n))
        process.finished.connect(
            lambda exit_code, exit_status, n=name: self._on_process_finished(n, exit_code))
        process.errorOccurred.connect(
            lambda err, n=name: self._on_process_error(n, err))

        # 启动
        process.start()
        self._processes[name] = process

        # 等待服务就绪（支持自动重试）
        if config.port > 0:
            self._wait_for_port(name, config)
        else:
            time.sleep(config.startup_wait)
            self._update_state(name, self.STATE_RUNNING)
            self._retry_counts[name] = 0  # 启动成功，重置重试计数

        return True

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
                port_in_use = not self._is_port_free(config.port)  # free=False 表示被占用
                if port_in_use:
                    self.log_received.emit(f"  ✅ {config.display_name} 已在运行")

        # 关键服务串行启动（避免端口检测冲突）
        self.log_received.emit("⚠️  有服务未启动，正在自动补齐...")

        # 1. 先启动 Ollama（必须有）
        self.start_service("ollama")
        time.sleep(2)  # 确保端口稳定

        # 2. NapCat + QQ 机器人（可选，取决于用户开关；bot 依赖 NapCat，随之启动）
        if "napcat" not in skip:
            self.start_service("napcat")
            time.sleep(3)  # 等待 NapCat WebSocket 就绪
            if "bot" not in skip:
                self.start_service("bot")
                time.sleep(1)

        # 3. 语音服务后台启动（非关键）
        if "gpt_sovits" not in skip:
            threading.Thread(target=self.start_service, args=("gpt_sovits",), daemon=True).start()

        # 等待关键服务就绪
        start = time.time()
        while time.time() - start < 15:
            ok_ollama = self._states.get("ollama") == self.STATE_RUNNING
            ok_napcat = "napcat" in skip or self._states.get("napcat") == self.STATE_RUNNING
            ok_bot = "bot" in skip or self._states.get("bot") == self.STATE_RUNNING
            if ok_ollama and ok_napcat and ok_bot:
                self.log_received.emit("✅ 所有服务已就绪")
                self.log_received.emit("=" * 50)
                return
            time.sleep(1)

        self.log_received.emit("⚠️  部分服务启动超时")
        self.log_received.emit("=" * 50)

    def stop_service(self, name: str) -> bool:
        """停止单个服务（非阻塞方式）"""
        if name not in self._processes:
            return True

        config = SERVICE_CONFIGS[name]
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
            self.log_received.emit(f"   {config.display_name} 未响应，强制终止")
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

        # 逆序停止，但不等待
        order = ["bot", "gpt_sovits", "napcat", "ollama"]
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

    def _wait_for_port(self, name: str, config: ServiceConfig, timeout: int = 60):
        """等待端口就绪，超时后自动重试（最多重试 1 次）"""
        max_startup_retries = 1
        startup_attempt = 0

        while startup_attempt <= max_startup_retries:
            if startup_attempt > 0:
                self.log_received.emit(f"🔄 {config.display_name} 启动重试 ({startup_attempt}/{max_startup_retries})...")
                time.sleep(2)
                # 杀掉旧进程
                if name in self._processes:
                    self._processes[name].kill()
                    self._processes[name].waitForFinished(3000)
                # 重试前清理端口
                if config.port > 0:
                    self._kill_port_if_busy(config.port, config.display_name)
                    time.sleep(1)
                # 重新启动进程
                process = QProcess(self)
                process.setWorkingDirectory(config.cwd)
                process.setProgram(config.command)
                process.setArguments(config.args)
                env = QProcessEnvironment.systemEnvironment()
                env.insert("PYTHONIOENCODING", "utf-8")
                env.insert("PYTHONUTF8", "1")
                env.insert("LANG", "en_US.UTF-8")
                process.setProcessEnvironment(env)
                process.readyReadStandardOutput.connect(
                    lambda p=process, n=name: self._read_stdout(p, n))
                process.readyReadStandardError.connect(
                    lambda p=process, n=name: self._read_stderr(p, n))
                process.finished.connect(
                    lambda exit_code, exit_status, n=name: self._on_process_finished(n, exit_code))
                process.errorOccurred.connect(
                    lambda err, n=name: self._on_process_error(n, err))
                process.start()
                self._processes[name] = process

            start = time.time()
            while time.time() - start < timeout:
                if self._states.get(name) == self.STATE_STOPPED:
                    break
                try:
                    # socket 检测端口连通性
                    import socket as sock
                    s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
                    s.settimeout(2)
                    s.connect(("127.0.0.1", config.port))
                    s.close()
                    self._update_state(name, self.STATE_RUNNING)
                    self._retry_counts[name] = 0
                    self.log_received.emit(f"✅ {config.display_name} 就绪 (端口 {config.port})")
                    return
                except Exception:
                    time.sleep(1)

            startup_attempt += 1

        self.log_received.emit(f"⚠️  {config.display_name} 端口 {config.port} 未能就绪 (已重试 {max_startup_retries} 次)")
        self._update_state(name, self.STATE_FAILED)

    def _read_stdout(self, process: QProcess, name: str):
        """读取标准输出"""
        data = bytes(process.readAllStandardOutput())
        text = data.decode("utf-8", errors="ignore").strip()
        if text:
            config = SERVICE_CONFIGS[name]
            # 只输出关键信息（过滤掉重复的启动日志）
            for line in text.split("\n"):
                line = line.strip()
                if line:
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
            self._update_state(name, self.STATE_FAILED)
            self.service_failed.emit(name, error_msg)
        except RuntimeError:
            pass  # 信号源已删除，忽略

    def cleanup(self):
        """清理所有进程（程序退出时调用）"""
        self.stop_all()
