#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：剪贴板（clipboard）
- read_clipboard    读回剪贴板里的文字（供转述/总结）
- write_clipboard   把指定文本写进剪贴板（供用户粘贴）

用 Win32 ctypes 直接操作剪贴板，不依赖 Qt，因此在工作线程里也安全。
"""
import ctypes
import ctypes.wintypes as wt
import re
import time

from skills import skill
from tool_registry import tool

skill("clipboard", "读写系统剪贴板", tools=["read_clipboard", "write_clipboard"])

SKILLS = ["read_clipboard", "write_clipboard"]
SKILL_DESCRIPTION = "读写系统剪贴板"

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.OpenClipboard.argtypes = [wt.HWND]
_user32.OpenClipboard.restype = wt.BOOL
_user32.CloseClipboard.argtypes = []
_user32.GetClipboardData.argtypes = [wt.UINT]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.EmptyClipboard.argtypes = []
_kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]


def _open(retries: int = 8) -> bool:
    """剪贴板可能被别的程序占着，重试几次"""
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.08)
    return False


def _read_text():
    if not _open():
        return None
    try:
        h = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return ""
        p = _kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.c_wchar_p(p).value or ""
        finally:
            _kernel32.GlobalUnlock(h)
    except Exception as e:
        print(f"[工具] 读剪贴板失败: {e}")
        return None
    finally:
        _user32.CloseClipboard()


def _write_text(text: str) -> bool:
    size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
    h = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
    if not h:
        return False
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, ctypes.create_unicode_buffer(text), size)
    _kernel32.GlobalUnlock(h)
    if not _open():
        _kernel32.GlobalFree(h)
        return False
    try:
        _user32.EmptyClipboard()
        if not _user32.SetClipboardData(_CF_UNICODETEXT, h):
            _kernel32.GlobalFree(h)
            return False
        # 成功：内存所有权已交给系统，不能再 GlobalFree
        return True
    except Exception as e:
        print(f"[工具] 写剪贴板失败: {e}")
        return False
    finally:
        _user32.CloseClipboard()


@tool(
    name="read_clipboard",
    description="读取用户剪贴板里的文字",
    keywords=["剪贴板", "复制了什么", "粘贴板", "我复制的内容"],
)
def read_clipboard(query: str) -> str:
    text = _read_text()
    if text is None:
        return "（剪贴板被别的程序占着，读不到呢，缓一下再试？）"
    if not text.strip():
        return "（剪贴板里现在没有文字呢）"
    clipped = text.strip()[:4000]
    cut = "…（太长，只取了前面一段）" if len(text.strip()) > 4000 else ""
    return (f"（剪贴板内容如下，请用达妮娅的语气转述/总结，不要直接说“剪贴板”三个字）：\n"
            f"{clipped}{cut}")


@tool(
    name="write_clipboard",
    description="把指定的文字写进用户剪贴板，方便用户粘贴",
    keywords=["复制到剪贴板", "帮我复制", "复制这段", "放进剪贴板"],
)
def write_clipboard(query: str) -> str:
    text = (query or "").strip()
    m = re.search(r"[：:]\s*(.+)$", text)
    if m:
        text = m.group(1).strip()
    else:
        text = re.sub(r"^(帮我|给我|麻烦|请)?\s*把?|"
                      r"(复制到剪贴板|复制进剪贴板|复制这段|放进剪贴板|复制一下)$",
                      "", text).strip()
    if not text:
        return "（要复制什么内容呢？告诉我文本，或者说「把我刚才说的话复制一下」）"
    if not _write_text(text):
        return "（剪贴板被别的程序占着，写不进去呢）"
    preview = text if len(text) <= 30 else text[:30] + "…"
    return f"（系统信息：已把「{preview}」放进剪贴板了。请用达妮娅的语气确认）"
