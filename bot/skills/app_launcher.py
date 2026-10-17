#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：应用启动（app_launcher）
按关键词识别目标应用/网站/文件，subprocess 启动。
开始菜单 / 注册表 App Paths / PATH 三级探测 + _launch_and_verify 防假成功。
高危工具：harness tiny/small 档会先弹确认框。
"""
import os
import re
import shutil
import subprocess

from skills import skill
from tool_registry import tool

skill("app_launcher", "打开电脑里的应用/网站/文件", tools=["open_app"])

SKILLS = ["open_app"]
SKILL_DESCRIPTION = "打开电脑里的应用/网站/文件"

# 本机已安装应用（来自开始菜单扫描）→ 启动命令 映射表
_APP_MAP = {
    "qq": 'start "" "QQ"',
    "qq音乐": 'start "" "QQ音乐"',
    "网易云": 'start "" "网易云音乐"',
    "网易云音乐": 'start "" "网易云音乐"',
    "云音乐": 'start "" "网易云音乐"',
    "微信": 'start "" "微信"',
    "腾讯会议": 'start "" "腾讯会议"',
    "steam": 'start "" "Steam"',
    "steamdeck": 'start "" "Steam"',
    "wework": "WeChat",
    "wegame": 'start "" "WeGame"',
    "哔哩哔哩": 'start "" "哔哩哔哩直播姬"',
    "b站": "start https://www.bilibili.com",
    "哔哩": "start https://www.bilibili.com",
    "夸克": 'start "" "夸克"',
    "夸克浏览器": 'start "" "夸克"',
    "quark": 'start "" "Quark"',
    "我的世界": 'start "" "我的世界启动器"',
    "mc": 'start "" "我的世界启动器"',
    "鸣潮": 'start "" "鸣潮"',
    "美图秀秀": 'start "" "美图秀秀"',
    "必剪": 'start "" "必剪"',
    "剪映": 'start "" "必剪"',
    "鼠大侠": 'start "" "鼠大侠"',
    "听歌识曲": 'start "" "听歌识曲"',
    "百度网盘": 'start "" "百度网盘"',
    "网盘": 'start "" "百度网盘"',
    "ollama": 'start "" "Ollama"',
    "trae": 'start "" "Trae CN"',
    "warp": 'start "" "Cloudflare WARP"',
    "uupc": 'start "" "UU加速器"',
    "mumu": 'start "" "MuMu模拟器"',
    "mumu模拟器": 'start "" "MuMu模拟器"',
    "emulator": 'start "" "MuMu模拟器"',
    "motrix": 'start "" "Motrix"',
    "网易发烧游戏": 'start "" "网易发烧游戏"',
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "ppt": "powerpnt",
    "office": 'start "" "Microsoft Office"',
    "outlook": "outlook",
    "publisher": "mspub",
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
    """按关键词识别目标应用，subprocess 启动。失败返回 None（LLM 走自然回复）。"""
    q = query.strip()

    if "设置" in q and "打开" not in q and "启动" not in q:
        try:
            subprocess.Popen("ms-settings:", shell=True, creationflags=0x00000008)
            print("[工具] 打开系统设置")
            return "（系统信息：已为用户打开「系统设置」。请用达妮娅的语气确认一下）"
        except Exception as e:
            print(f"[工具] open_app 设置失败: {e}")
            return None

    m = re.search(r'(?:打开|启动|运行|点开|调出|调用)[\s，,、]{0,3}([\u4e00-\u9fff\w\.\-]{1,30})', q)
    if not m:
        return None
    target = m.group(1).lower().strip()

    app = _APP_MAP.get(target)
    if app:
        if not app.startswith("start "):
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
        rc = 0
    if rc != 0:
        print(f"[工具] open_app 启动失败: {target} -> {cmd} (exit={rc})")
        return None

    print(f"[工具] 启动应用: {target} -> {cmd}")
    extra = ""
    if target in ("qq", "网易云", "网易云音乐", "云音乐", "微信"):
        extra = "（若系统弹出权限确认框请点允许）"
    return f"（系统信息：已为用户启动「{target}」。请用达妮娅的语气确认一下{extra}）"


def _app_paths_exe(name: str):
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
