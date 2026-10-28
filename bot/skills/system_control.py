#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：系统调节（system_control）
- set_volume      调音量（静音 / 加 / 减 / 调到百分之多少）
- set_brightness  调屏幕亮度（笔记本可用；台式机/驱动不支持时友好失败）

零第三方依赖兜底：音量走 Win32 媒体键（keybd_event），亮度走 PowerShell WMI。
装了 pycaw / screen_brightness_control 时自动用精确值。
"""
import ctypes
import re
import subprocess

from skills import skill
from tool_registry import tool

skill("system_control", "调节音量与屏幕亮度", tools=["set_volume", "set_brightness"])

SKILLS = ["set_volume", "set_brightness"]
SKILL_DESCRIPTION = "调节音量与屏幕亮度"

_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_KEYEVENTF_KEYUP = 0x0002

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _press(vk: int, times: int = 1):
    """发一次媒体键（按下+抬起），times 次"""
    u = ctypes.windll.user32
    for _ in range(max(1, times)):
        u.keybd_event(vk, 0, 0, 0)
        u.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def _num(text: str, default=None):
    """从文本里取数字（支持阿拉伯与中文数字）"""
    m = re.search(r"(\d{1,3})", text)
    if m:
        return int(m.group(1))
    for k, v in _CN_NUM.items():
        if k in text:
            return v
    return default


def _set_volume_pycaw(percent: int):
    """有 pycaw 时设绝对值；不可用返回 False"""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return False
    try:
        device = AudioUtilities.GetSpeakers()
        vol = device.EndpointVolume
        vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, percent / 100.0)), None)
        return True
    except Exception as e:
        print(f"[工具] pycaw 设音量失败: {e}")
        return False


@tool(
    name="set_volume",
    description="调节系统音量：静音/取消静音、音量加/减、调到指定百分比",
    keywords=["音量", "静音", "声音大点", "声音小点", "调大声音", "调小声音"],
)
def set_volume(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "（你想怎么调音量呢？说「静音」或「音量调到30」）"

    # 静音 / 取消静音
    if "静音" in q or "mute" in q.lower():
        _press(_VK_VOLUME_MUTE)
        if "取消" in q or "恢复" in q or "关掉静音" in q or "解除" in q:
            return "（系统信息：已切换静音开关（之前若是静音则已恢复）。请用达妮娅的语气回应）"
        return "（系统信息：已为用户静音（媒体键切换）。请用达妮娅的语气回应）"

    # 调到百分比
    m_pct = re.search(r"(?:调|设|变)?[\s]*[到至为成]?\s*(\d{1,3})\s*%|"
                      r"音量[\s]*(?:调|设|变)?[\s]*[到至为成][\s]*(\d{1,3})", q)
    if m_pct:
        pct = int(m_pct.group(1) or m_pct.group(2))
        pct = max(0, min(100, pct))
        if _set_volume_pycaw(pct):
            return f"（系统信息：已把音量设为 {pct}%。请用达妮娅的语气回应）"
        # 无 pycaw：先降到最低（约 0），再按 2% 一档往上走到目标
        _press(_VK_VOLUME_DOWN, 60)
        _press(_VK_VOLUME_UP, max(0, round(pct / 2)))
        return (f"（系统信息：音量已大致调到 {pct}%（没有精确控制库，是按键步进的近似值）。"
                f"请用达妮娅的语气回应，不要提技术细节）")

    # 加 / 减
    step = _num(q.replace("音量", ""), None)
    times = max(1, min(int((step or 10) / 2), 50))  # 媒体键每档约 2%
    if any(k in q for k in ("加", "大", "高", "升", "up", "调高")):
        _press(_VK_VOLUME_UP, times)
        return "（系统信息：已把音量调大了一些。请用达妮娅的语气回应）"
    if any(k in q for k in ("减", "小", "低", "降", "down", "调低")):
        _press(_VK_VOLUME_DOWN, times)
        return "（系统信息：已把音量调小了一些。请用达妮娅的语气回应）"
    return "（你想怎么调音量呢？说「静音」「音量加到20」「音量调到30%」都行）"


def _ps(cmd: str):
    """跑一段 PowerShell，返回 stdout（失败返回 None）"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return r.stdout.strip()
    except Exception as e:
        print(f"[工具] PowerShell 调用失败: {e}")
        return None


def _get_brightness():
    out = _ps("(Get-CimInstance -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness")
    if out and out.isdigit():
        return int(out)
    return None


def _set_brightness_wmi(percent: int) -> bool:
    percent = max(0, min(100, percent))
    sbc_ok = False
    try:
        import screen_brightness_control as sbc
        sbc.set_brightness(percent)
        sbc_ok = True
    except Exception:
        sbc_ok = False
    if sbc_ok:
        return True
    out = _ps("(Get-CimInstance -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
              f").WmiSetBrightness(1,{percent})")
    return out is not None


@tool(
    name="set_brightness",
    description="调节屏幕亮度（调到指定百分比或加减）",
    keywords=["亮度", "屏幕太亮", "屏幕太暗", "调亮", "调暗"],
)
def set_brightness(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "（你想怎么调亮度呢？说「亮度调到50」）"

    m = re.search(r"(\d{1,3})\s*%?", q)
    pct = int(m.group(1)) if m else _num(q, None)
    if pct is not None:
        target = max(0, min(100, pct))
        if _set_brightness_wmi(target):
            return f"（系统信息：已把屏幕亮度设为 {target}%。请用达妮娅的语气回应）"
        return "（系统信息：这台机器我调不了亮度（显卡/驱动不支持），请用达妮娅的语气说一声抱歉）"

    cur = _get_brightness()
    if cur is None:
        return "（系统信息：这台机器我读不到亮度（台式机/外接屏通常不支持），请用达妮娅的语气说一声）"
    delta = 10 if any(k in q for k in ("亮", "高", "加", "升")) else -10
    target = max(0, min(100, cur + delta))
    if _set_brightness_wmi(target):
        return f"（系统信息：亮度已从 {cur}% 调到 {target}%。请用达妮娅的语气回应）"
    return "（系统信息：这台机器我调不了亮度，请用达妮娅的语气说一声抱歉）"
