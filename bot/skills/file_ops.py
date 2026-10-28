#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：文件操作（file_ops）
- list_dir          看文件夹里有什么
- read_file         读文本文件
- write_file        新建/追加文本文件
- organize_folder   把文件夹里的文件按类型归类（同盘内移动，不删除、不跨盘）

安全边界：所有操作只能落在白名单根（家目录 / 桌面 / 下载 / 文档）内。
ToolGuard 也登记了这几个工具做同一套路径校验（双层保险）。
"""
import os
import re
import shutil
import time

from skills import skill
from tool_registry import tool

skill("file_ops", "读写文件、查看目录、整理文件夹", tools=[
    "list_dir", "read_file", "write_file", "organize_folder",
])

SKILLS = ["list_dir", "read_file", "write_file", "organize_folder"]
SKILL_DESCRIPTION = "读写文件、查看目录、整理文件夹"

# 中文别名 → 家目录下的真实子目录（空串 = 家目录本身）
_ALIASES = {
    "下载文件夹": "Downloads",
    "下载目录": "Downloads",
    "下载": "Downloads",
    "桌面": "Desktop",
    "文档": "Documents",
    "文档文件夹": "Documents",
    "图片": "Pictures",
    "图片文件夹": "Pictures",
    "音乐": "Music",
    "视频": "Videos",
    "家目录": "",
    "主目录": "",
    "用户目录": "",
}

_HOME = os.path.expanduser("~")

# 文件类型归类表
_CATEGORIES = [
    ("图片", {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif", ".tif",
              ".tiff", ".heic", ".ico", ".svg"}),
    ("文档", {".doc", ".docx", ".pdf", ".txt", ".md", ".xls", ".xlsx", ".ppt",
              ".pptx", ".csv", ".rtf", ".odt", ".epub", ".wps"}),
    ("视频", {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".mpg", ".mpeg", ".ts"}),
    ("音频", {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".ape"}),
    ("压缩包", {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}),
    ("安装包", {".exe", ".msi", ".msix", ".apk", ".appx", ".bat", ".cmd"}),
]


# ── 路径解析（ToolGuard 也调用 resolve_any）──────────────────────────

def _explicit_path(text: str):
    """从文本里抽显式路径（C:\\xxx / D:/xxx / ~/xxx）"""
    if not text:
        return None
    m = re.search(r"([A-Za-z]:[\\/][^\s，。！？,、:：\"'<>|*?]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"(~[\\/][^\s，。！？,、:：\"'<>|*?]+)", text)
    if m:
        return os.path.expanduser(m.group(1))
    return None


def resolve_dir(text: str):
    """把「下载文件夹/桌面/文档…」解析成真实目录路径（解析不到返回 None）"""
    if not text:
        return None
    explicit = _explicit_path(text)
    if explicit and os.path.isdir(explicit):
        return os.path.abspath(explicit)
    # 长别名优先，避免「下载」抢了「下载文件夹」
    for alias in sorted(_ALIASES, key=len, reverse=True):
        if alias in text:
            sub = _ALIASES[alias]
            p = os.path.join(_HOME, sub) if sub else _HOME
            if os.path.isdir(p):
                return p
    return None


def resolve_any(text: str):
    """解析文本里出现的任意路径（目录优先，失败则显式路径）。
    供 ToolGuard 做白名单校验；解析不到返回 None（此时放行，由工具自身再判）。"""
    d = resolve_dir(text)
    if d:
        return d
    p = _explicit_path(text)
    return p or None


# 凭据/私有目录：即使落在白名单根内也一律拒绝
# .ssh/.aws 里是私钥，.local/.config 里是登录 token，AppData 里是各客户端凭据
_DENY_SEGMENTS = {
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".local", ".config",
    ".cache", ".trae-cn", ".git", "appdata",
}
# 项目自身的配置文件里有 API Key / cookie，也不让文件工具碰
_DENY_NAMES = {"settings.json", "settings.local.json"}


def is_sensitive_path(path: str) -> bool:
    """路径里含隐藏目录/文件、凭据目录或配置文件即为敏感"""
    real = os.path.realpath(os.path.expanduser(path or ""))
    for part in re.split(r"[\\/]+", real):
        low = part.lower()
        if not low or low in (".", ".."):
            continue
        if low in _DENY_SEGMENTS or low in _DENY_NAMES:
            return True
        # 以点开头的隐藏文件/目录（.env / .gitignore / .ssh …）一律不碰
        if low.startswith("."):
            return True
    return False


def _is_writable(path: str) -> bool:
    """实路径必须落在白名单根内，且不能是隐藏/凭据目录"""
    if is_sensitive_path(path):
        return False
    real = os.path.realpath(os.path.expanduser(path))
    roots = [_HOME, os.getcwd(),
             os.path.expanduser("~/Desktop"),
             os.path.expanduser("~/Downloads"),
             os.path.expanduser("~/Documents")]
    for root in roots:
        if not root:
            continue
        root_real = os.path.realpath(root).rstrip("\\/")
        # 按目录边界比较，避免 "C:\Users\user2" 被当成在 "C:\Users\user" 之内
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    return False


def _find_file(text: str):
    """在显式路径 / 别名目录 / 桌面·下载·文档 里找一个已存在的文件"""
    explicit = _explicit_path(text)
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)
    name_m = re.search(r"([^\s，。！？,、:：/\\]+\.(?:txt|md|log|json|csv|ini|py|js|html?|docx?|xlsx?|pdf|pptx?|rtf))",
                       text or "", re.IGNORECASE)
    name = name_m.group(1) if name_m else None
    dirs = []
    d = resolve_dir(text)
    if d:
        dirs.append(d)
    for sub in ("Desktop", "Downloads", "Documents", ""):
        p = os.path.join(_HOME, sub) if sub else _HOME
        if p not in dirs:
            dirs.append(p)
    if not name:
        # 没有扩展名时，把整句里最后一个"像文件名"的片段当名字
        tail = re.sub(r"^.*?(?:读|看看|打开|读取)", "", text or "").strip(" 的了呢啊吧吗：:")
        name = tail[:40] or None
    if not name:
        return None
    for p in dirs:
        cand = os.path.join(p, name)
        if os.path.isfile(cand):
            return cand
    return None


def _default_new_file(text: str):
    """为 write_file 解析目标文件：显式路径 > 「写进/记到 X」里的 X > 桌面默认笔记"""
    explicit = _explicit_path(text)
    if explicit:
        return explicit
    m = re.search(r"(?:写进|写入|写到|记到|记进|存到|保存到|新建|创建)[\s：:]*"
                  r"([^\s，。！？,、:：]+)", text or "")
    if m:
        name = m.group(1).strip()
        if not os.path.splitext(name)[1]:
            name += ".txt"
        d = resolve_dir(text) or os.path.join(_HOME, "Desktop")
        return os.path.join(d, name)
    return os.path.join(_HOME, "Desktop", "达妮娅笔记.txt")


# ── 工具实现 ──────────────────────────────────────────────────────

@tool(
    name="list_dir",
    description="查看文件夹里有什么（桌面/下载/文档，或指定路径）",
    keywords=["看看文件夹", "文件夹里有什么", "有什么文件", "哪些文件", "列目录", "看看目录"],
)
def list_dir(query: str) -> str:
    d = resolve_dir(query)
    if not d:
        return "（我没找到这个文件夹呢，说清楚点，比如「下载文件夹」「桌面」？）"
    if not _is_writable(d):
        return "（这个位置我不能动哦）"
    try:
        entries = os.listdir(d)
    except Exception as e:
        return f"（读不了这个文件夹：{e}）"
    if not entries:
        return f"（{d} 是空的呢）"
    lines, dirs_n = [], 0
    for name in sorted(entries)[:40]:
        full = os.path.join(d, name)
        try:
            st = os.stat(full)
        except Exception:
            continue
        if os.path.isdir(full):
            dirs_n += 1
            lines.append(f"[目录] {name}")
        else:
            lines.append(f"{name}  {st.st_size / 1024:.1f}KB  "
                         f"{time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))}")
    more = "" if len(entries) <= 40 else f"\n…还有 {len(entries) - 40} 项"
    return f"📁 {d}（共 {len(entries)} 项，含 {dirs_n} 个目录）：\n" + "\n".join(lines) + more


@tool(
    name="read_file",
    description="读一个文本文件的内容（桌面/下载/文档里的文件，或指定路径）",
    keywords=["读文件", "读一下文件", "看看文件内容", "打开文件看看"],
)
def read_file(query: str) -> str:
    p = _find_file(query)
    if not p:
        return "（没找到这个文件呢，告诉我文件名，或者放到桌面/下载/文档里更方便）"
    if not _is_writable(p):
        return "（这个位置我不能动哦）"
    try:
        size = os.path.getsize(p)
        if size > 2 * 1024 * 1024:
            return f"（这个文件有点大（{size/1024/1024:.1f}MB），我读不动呢）"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(8000)
    except Exception as e:
        return f"（读不了这个文件：{e}）"
    cut = "" if size <= len(content.encode("utf-8")) * 1.02 else "\n…（内容太长，只读了前面一段）"
    return f"📄 {os.path.basename(p)} 的内容：\n{content}{cut}"


@tool(
    name="write_file",
    description="新建或追加一个文本文件（默认写到桌面）",
    keywords=["写文件", "记下来", "写进文件", "帮我记", "记到文件里",
              # 明确要落盘的说法（配合 tool_registry 的 write_file 护栏使用）
              "保存到", "存到", "写到", "存成", "存为"],
)
def write_file(query: str) -> str:
    text = (query or "").strip()
    # 内容：取「把 … 写进/记到 …」之间的部分，或冒号后的部分
    content = ""
    m = re.search(r"把?[\s]*[\"“]?(.+?)[\"”]?[\s]*"
                  r"(?:写进|写入|写到|记到|记进|存到|保存到|新建|创建)", text)
    if m:
        content = m.group(1)
    else:
        m2 = re.search(r"[:：]\s*(.+)$", text)
        content = m2.group(1) if m2 else ""
    content = re.sub(r"^(帮我|给我|麻烦|请|你)[\s]*", "", content).strip()
    if not content:
        return "（要写什么内容呢？说「把周末计划写进 待办.txt」这样我就能记下来）"

    path = _default_new_file(text)
    if not _is_writable(path):
        return "（这个位置我不能动哦，放桌面/下载/文档里吧）"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        exists = os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if exists:
                f.write("\n")
            f.write(content)
    except Exception as e:
        return f"（写不进去：{e}）"
    verb = "追加到" if exists else "新建并写入"
    return f"（系统信息：已把内容{verb}「{os.path.basename(path)}」（位置 {path}）。请用达妮娅的语气确认）"


@tool(
    name="organize_folder",
    description="把文件夹里的文件按类型归类到子文件夹（图片/文档/视频/音频/压缩包/安装包/其他）",
    keywords=["整理文件夹", "整理一下", "归类", "收拾一下文件夹", "整理下载"],
)
def organize_folder(query: str) -> str:
    d = resolve_dir(query) or os.path.join(_HOME, "Downloads")
    if not os.path.isdir(d):
        return "（没找到这个文件夹呢）"
    if not _is_writable(d):
        return "（这个位置我不能动哦）"

    moved = {}
    skipped = 0
    try:
        for name in os.listdir(d):
            full = os.path.join(d, name)
            if not os.path.isfile(full):
                continue
            ext = os.path.splitext(name)[1].lower()
            cat = "其他"
            for cname, exts in _CATEGORIES:
                if ext in exts:
                    cat = cname
                    break
            sub = os.path.join(d, cat)
            target = os.path.join(sub, name)
            try:
                os.makedirs(sub, exist_ok=True)
                if os.path.exists(target):
                    skipped += 1
                    continue
                shutil.move(full, target)
                moved[cat] = moved.get(cat, 0) + 1
            except Exception as e:
                print(f"[工具] organize_folder 移动失败 {name}: {e}")
                skipped += 1
    except Exception as e:
        return f"（整理失败：{e}）"

    if not moved:
        return f"（{d} 里没有可以整理的文件呢{'（有 %d 个重名被跳过）' % skipped if skipped else ''}）"
    detail = "、".join(f"{k} {v} 个" for k, v in sorted(moved.items(), key=lambda x: -x[1]))
    tail = f"，另有 {skipped} 个重名跳过" if skipped else ""
    return (f"（系统信息：已整理「{d}」，共归类 {sum(moved.values())} 个文件——{detail}{tail}。"
            f"没有删除任何文件。请用达妮娅的语气汇报结果）")
