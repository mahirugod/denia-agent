#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电脑状态采集 - 基于 psutil（CPU/内存/磁盘/网络/电池/系统信息）

QTimer 定时采集，system_ready 信号广播 JSON；latest() 供界面打开时立即取缓存。
CPU 百分比用非阻塞模式（interval=None），首轮为 0，从第二轮起有值。
"""
import os
import socket
import time

import psutil
from PySide6.QtCore import QObject, Signal, QTimer

_POLL_MS = 3000  # 3 秒采集一次


def _gb(n: float) -> float:
    return round(n / (1024 ** 3), 1)


def _uptime_text(seconds: float) -> str:
    m = int(seconds // 60)
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return "%d天%d小时%d分" % (d, h, m)
    if h:
        return "%d小时%d分" % (h, m)
    return "%d分钟" % m


class SystemStatusWorker(QObject):
    """定时采集电脑状态，结果经 system_ready 信号广播"""

    system_ready = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._latest = None
        self._last_io = None       # (time, sent, recv)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._poll()
        self._timer.start(_POLL_MS)

    def latest(self):
        return self._latest

    def poll(self):
        """手动触发一次采集（也由定时器调用）"""
        self._poll()

    def _poll(self):
        try:
            info = self._collect()
            self._latest = info
            self.system_ready.emit(info)
        except Exception as e:
            print("[状态] 采集失败: %s" % e)

    def _collect(self) -> dict:
        now = time.time()

        # CPU
        cpu_percent = psutil.cpu_percent(interval=None)
        cores = psutil.cpu_percent(interval=None, percpu=True)
        freq = psutil.cpu_freq()
        load = os.getloadavg() if hasattr(os, "getloadavg") else None

        # 内存
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # 磁盘（所有固定分区）
        disks = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            disks.append({
                "mount": part.mountpoint,
                "used": _gb(u.used),
                "total": _gb(u.total),
                "percent": u.percent,
            })

        # 网络速率（与上次采样差值）
        io = psutil.net_io_counters()
        net = {"up_kb": 0.0, "down_kb": 0.0, "total_sent": _gb(io.bytes_sent),
               "total_recv": _gb(io.bytes_recv)}
        if self._last_io:
            dt = now - self._last_io[0]
            if dt > 0:
                net["up_kb"] = round((io.bytes_sent - self._last_io[1]) / dt / 1024, 1)
                net["down_kb"] = round((io.bytes_recv - self._last_io[2]) / dt / 1024, 1)
        self._last_io = (now, io.bytes_sent, io.bytes_recv)

        # 电池（台式机无）
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        battery = None
        if bat is not None:
            battery = {"percent": bat.percent, "plugged": bool(bat.power_plugged),
                       "secsleft": bat.secsleft if bat.secsleft and bat.secsleft > 0 else None}

        # 系统信息
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = ""
        return {
            "cpu": {
                "percent": cpu_percent,
                "cores": cores,
                "freq_mhz": round(freq.current) if freq else None,
                "count": psutil.cpu_count(logical=True),
            },
            "memory": {
                "used": _gb(vm.used), "total": _gb(vm.total),
                "percent": vm.percent,
                "swap_used": _gb(swap.used), "swap_total": _gb(swap.total),
                "swap_percent": swap.percent,
            },
            "disks": disks,
            "net": net,
            "battery": battery,
            "processes": len(psutil.pids()),
            "uptime": _uptime_text(now - psutil.boot_time()),
            "ts": int(now),
        }
