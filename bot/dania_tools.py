#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅的内置工具集（通过 @tool 自动注册）
- query_time: 时间/日期查询（本地，零网络）
- set_reminder: 定时提醒（threading.Timer，到点回调通知 UI）
- screen_context: 看屏幕关键词提示（截屏由 UI 主线程完成，这里只做拦截标记）

新增工具只需在本文件加一个 @tool 函数即可被自动路由。
"""
import re
import threading
from datetime import datetime

from tool_registry import tool

# 提醒到点后的回调（由 UI 层注入，参数: 提醒内容）
_reminder_callback = None
# 进行中的提醒列表（任务页展示用）
_active_reminders = []


def set_reminder_callback(callback):
    """UI 层注入提醒触发回调（线程安全：回调内部自行切线程）"""
    global _reminder_callback
    _reminder_callback = callback


@tool(
    name="query_time",
    description="查询当前时间、日期、星期",
    keywords=["几点", "时间", "日期", "几号", "多少号", "星期几", "礼拜几", "今天几月"],
)
def query_time(query: str) -> str:
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    ctx = (f"（系统信息：现在是{now.year}年{now.month}月{now.day}日 {weekdays[now.weekday()]} "
           f"{now.hour}点{now.minute}分。请用达妮娅的语气自然地说出来）")
    return ctx


# 匹配两种语序（数字支持阿拉伯与中文数字：一分钟 / 15分钟 / 半小时 / 二十分钟）：
#   A. "X分钟后/小时后提醒我……"  (数字在前，最常见)
#   B. "提醒我X分钟后/小时后……"  (数字在后)
_NUM = r'([\d一二两三四五六七八九十半]+)'
_UNIT = r'\s*(分钟|个小时|小时|分)\s*[之以]?[后过]?'
_REMINDER_PATTERNS = [
    _NUM + _UNIT + r'提醒我.{0,4}?(.+)?$',
    r'提醒我.{0,6}?' + _NUM + _UNIT + r'.{0,4}?(.+)?$',
]

# 中文数字映射
_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _parse_num(s: str):
    """'15'→15；'一/两/十/十五/二十/半'等中文数字→数值；解析失败返回 None"""
    if s.isdigit():
        return int(s)
    if s == "半":
        return 0.5
    if "十" in s:
        ten, _, one = s.partition("十")
        if (ten and ten not in _CN_DIGIT) or (one and one not in _CN_DIGIT):
            return None
        return (_CN_DIGIT.get(ten, 1) if ten else 1) * 10 + (_CN_DIGIT.get(one, 0) if one else 0)
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    return None


def _match_reminder(query: str):
    for pat in _REMINDER_PATTERNS:
        m = re.search(pat, query)
        if m:
            return m
    return None


@tool(
    name="set_reminder",
    description="定时提醒：X分钟后/小时后提醒做某事（两种语序、中文数字都支持）",
    patterns=_REMINDER_PATTERNS,
)
def set_reminder(query: str) -> str:
    m = _match_reminder(query)
    if not m:
        return None
    amount = _parse_num(m.group(1))
    if not amount:
        return None
    unit = m.group(2)
    content = (m.group(3) or "时间到啦").strip("。，, .？?！!")
    # 归一单位：「小时/个小时」统一为小时，「分/分钟」统一为分钟
    if unit in ("小时", "个小时"):
        seconds = amount * 3600
        if amount < 1:  # 半小时 → 显示 30分钟
            disp, unit_cn = int(amount * 60), "分钟"
        else:
            disp, unit_cn = int(amount), "小时"
    else:  # 分 / 分钟
        seconds = amount * 60
        disp, unit_cn = int(amount), "分钟"
    if not (1 <= seconds <= 86400):
        return None

    entry = {"content": content}
    _active_reminders.append(entry)

    def _fire():
        try:
            _active_reminders.remove(entry)
        except ValueError:
            pass
        if _reminder_callback:
            try:
                _reminder_callback(f"时间到啦！{disp}{unit_cn}前让你记得：{content}")
            except Exception as e:
                print(f"[工具] 提醒回调异常: {e}")

    timer = threading.Timer(seconds, _fire)
    timer.daemon = True
    timer.start()
    return f"（系统信息：已设好提醒，{disp}{unit_cn}后提醒用户「{content}」。请用达妮娅的语气答应下来）"


@tool(
    name="screen_context",
    description="用户提到看屏幕/网页内容时提示已截屏（实际截屏由UI层完成）",
    keywords=["屏幕", "网页", "页面上", "桌面上", "看看页面", "看下页面"],
)
def screen_context(query: str) -> str:
    # 截屏在 main.py 主线程完成（Qt 限制），这里只返回提示上下文
    return None  # 由 main.py 截屏流程接管，不在此注入


# ===================== 应用调用 =====================
import os
import shutil
import subprocess

# 本机已安装应用（来自开始菜单扫描）→ 启动命令 映射表
# 用「开始菜单/注册名」做启动命令，兼容第三方应用；系统应用用命令名/URL
_APP_MAP = {
    # ---- 第三方应用（本机实际安装，用注册名启动）----
    "qq": "start \"\" \"QQ\"",
    "qq音乐": "start \"\" \"QQ音乐\"",
    "网易云": "start \"\" \"网易云音乐\"",
    "网易云音乐": "start \"\" \"网易云音乐\"",
    "云音乐": "start \"\" \"网易云音乐\"",
    "微信": "start \"\" \"微信\"",
    "腾讯会议": "start \"\" \"腾讯会议\"",
    "steam": "start \"\" \"Steam\"",
    "steamdeck": "start \"\" \"Steam\"",
    "wework": "WeChat",
    "wegame": "start \"\" \"WeGame\"",
    "哔哩哔哩": "start \"\" \"哔哩哔哩直播姬\"",
    "b站": "start https://www.bilibili.com",
    "哔哩": "start https://www.bilibili.com",
    "夸克": "start \"\" \"夸克\"",
    "夸克浏览器": "start \"\" \"夸克\"",
    "quark": "start \"\" \"Quark\"",
    "我的世界": "start \"\" \"我的世界启动器\"",
    "mc": "start \"\" \"我的世界启动器\"",
    "鸣潮": "start \"\" \"鸣潮\"",
    "美图秀秀": "start \"\" \"美图秀秀\"",
    "必剪": "start \"\" \"必剪\"",
    "剪映": "start \"\" \"必剪\"",
    "鼠大侠": "start \"\" \"鼠大侠\"",
    "听歌识曲": "start \"\" \"听歌识曲\"",
    "百度网盘": "start \"\" \"百度网盘\"",
    "网盘": "start \"\" \"百度网盘\"",
    "ollama": "start \"\" \"Ollama\"",
    "trae": "start \"\" \"Trae CN\"",
    "warp": "start \"\" \"Cloudflare WARP\"",
    "uupc": "start \"\" \"UU加速器\"",
    "mumu": "start \"\" \"MuMu模拟器\"",
    "mumu模拟器": "start \"\" \"MuMu模拟器\"",
    "emulator": "start \"\" \"MuMu模拟器\"",
    "motrix": "start \"\" \"Motrix\"",
    "网易发烧游戏": "start \"\" \"网易发烧游戏\"",
    # ---- 办公 ----
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "ppt": "powerpnt",
    "office": "start \"\" \"Microsoft Office\"",
    "outlook": "outlook",
    "publisher": "mspub",
    # ---- 浏览器/网站 ----
    "浏览器": "msedge",
    "edge": "msedge",
    "谷歌": "chrome",
    "chrome": "chrome",
    "youtube": "start https://www.youtube.com",
    "谷歌搜索": "start https://www.google.com",
    "百度": "start https://www.baidu.com",
    "baidu": "start https://www.baidu.com",
    "淘宝": "start https://www.taobao.com",
    "京东": "start https://www.jd.com",
    "知乎": "start https://www.zhihu.com",
    "抖音": "start https://www.douyin.com",
    # ---- 系统应用（命令名/URL）----
    "视频": "wmplayer",
    "音乐": "wmplayer",
    "windows media player": "wmplayer",
    "画图": "mspaint",
    "paint": "mspaint",
    "记事本": "notepad",
    "记事": "notepad",
    "计算器": "calc",
    "资源管理器": "explorer",
    "文件管理": "explorer",
    "文件资源管理器": "explorer",
    "explorer": "explorer",
    "任务管理": "taskmgr",
    "任务管理器": "taskmgr",
    "控制面板": "control",
    "设置": "ms-settings:",
    "系统设置": "ms-settings:",
    "电脑设置": "ms-settings:",
    "终端": "cmd",
    "命令行": "cmd",
    "cmd": "cmd",
    "powershell": "powershell",
    "python": "python",
    "python3": "python",
    "idle": "pythonw -m idlelib",
}


@tool(
    name="open_app",
    description="打开电脑里的应用/网站/文件",
    keywords=["打开", "启动", "运行", "点开", "调出", "调用", "设置", "QQ", "qq"],
)
def open_app(query: str) -> str:
    """按关键词识别目标应用，调用 subprocess 启动。失败返回 None（LLM 走自然回复）。"""
    q = query.strip()

    # 「设置」单独出现（打开系统设置）——不走 target 提取
    if "设置" in q and "打开" not in q and "启动" not in q:
        try:
            subprocess.Popen("ms-settings:", shell=True, creationflags=0x00000008)
            print("[工具] 打开系统设置")
            return "（系统信息：已为用户打开「系统设置」。请用达妮娅的语气确认一下）"
        except Exception as e:
            print(f"[工具] open_app 设置失败: {e}")
            return None

    # 提取目标：「打开/启动/运行/点开/调出/调用 X」里的 X（只跳过分隔符，避免吞字）
    m = re.search(r'(?:打开|启动|运行|点开|调出|调用)[\s，,、]{0,3}([\u4e00-\u9fff\w\.\-]{1,30})', q)
    if not m:
        return None
    target = m.group(1).lower().strip()

    # 统一解析目标 → 真实启动目标（URL / 快捷方式 / exe / 系统命令）
    # 解析不到就如实返回 None，绝不盲目交给 start 假成功
    app = _APP_MAP.get(target)
    if app:
        if not app.startswith("start "):
            # 纯系统命令（notepad/calc 等）
            return _launch_and_verify(f'start "" {app}', target)
        name = re.sub(r'^start\s+""\s+', '', app).strip().strip('"')
        if name.startswith(("http://", "https://", "ms-", "shell:")):
            return _launch_and_verify(f'start "" "{name}"', target)
        lnk = _find_start_menu_shortcut(name)
        if lnk:
            return _launch_and_verify(f'start "" "{lnk}"', target)
        exe = _find_local_exe(name) or _app_paths_exe(name) or shutil.which(name)
        if exe:
            return _launch_and_verify(f'start "" "{exe}"', target)
        print(f"[工具] open_app 未找到应用: {name}")
        return None

    found = _find_local_exe(target)
    if found:
        return _launch_and_verify(f'start "" "{found}"', target)
    lnk = _find_start_menu_shortcut(target)
    if lnk:
        return _launch_and_verify(f'start "" "{lnk}"', target)
    if shutil.which(target) or _app_paths_exe(target):
        return _launch_and_verify(f'start "" "{target}"', target)
    print(f"[工具] open_app 未找到应用: {target}")
    return None


def _launch_and_verify(cmd: str, target: str):
    """启动应用；失败返回 None（不假成功）。前提：cmd 里的目标已经过确定性解析。"""
    try:
        proc = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        print(f"[工具] open_app 失败: {target} -> {e}")
        return None
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        rc = 0  # start 已派生 GUI 进程，cmd 未退出视为成功
    if rc != 0:
        print(f"[工具] open_app 启动失败: {target} -> {cmd} (exit={rc})")
        return None

    print(f"[工具] 启动应用: {target} -> {cmd}")
    # QQ / 网易云这类第三方应用，首次启动 Windows 会弹 UAC 确认框
    extra = ""
    if target in ("qq", "网易云", "网易云音乐", "云音乐", "微信"):
        extra = "（若系统弹出权限确认框请点允许）"
    return f"（系统信息：已为用户启动「{target}」。请用达妮娅的语气确认一下{extra}）"


def _app_paths_exe(name: str):
    """查注册表 App Paths（HKLM/HKCU），路径存在才返回；找不到返回 None。"""
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}.exe",
                    rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}"):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    path = winreg.QueryValue(k, None)
                    if path and os.path.exists(path):
                        return path
            except OSError:
                continue
    return None


def _find_local_exe(target: str):
    """按 target 探测本机已安装应用的 exe，返回绝对路径；找不到返回 None。"""
    exe = target + ".exe"
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        rf"C:\Program Files\{exe}",
        rf"C:\Program Files (x86)\{exe}",
        rf"{local_appdata}\Programs\{target}\{exe}" if local_appdata else None,
        rf"{local_appdata}\Apps\{target}\{exe}" if local_appdata else None,
        rf"{local_appdata}\{exe}" if local_appdata else None,
        exe,
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _find_start_menu_shortcut(target: str):
    """在开始菜单中查找名字包含 target 的快捷方式（.lnk），返回绝对路径；找不到返回 None。"""
    target = target.lower()
    roots = [
        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                     r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    for root in roots:
        if not root.strip() or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk") and target in f.lower():
                    return os.path.join(dirpath, f)
    return None


def fire_reminder_for_test(content: str):
    """测试用：立即触发提醒回调"""
    if _reminder_callback:
        _reminder_callback(content)
