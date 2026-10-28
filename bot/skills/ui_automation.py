#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：界面自动化（ui_automation）
- ui_inspect  列出窗口里的可操作控件（按钮/输入框/菜单…），供模型规划步骤
- ui_click    点击指定窗口里的控件
- ui_type     向指定窗口的输入框写入文字（支持中文）
- ui_hotkey   发送快捷键（ctrl+s / alt+tab 等）
- ui_window   激活 / 最小化 / 最大化 / 还原 / 关闭窗口

基于 Windows UI Automation（pywinauto uia 后端）。
说明：只能操作与达妮娅同权限级别的窗口；管理员权限的程序需要提权后才能操作。
危险操作（关机/支付/卸载/格式化等）一律拒绝，交给用户自己确认。
"""
import re
import time
import ctypes

from skills import skill
from tool_registry import tool

skill("ui_automation", "操作真实窗口界面（点击/输入/快捷键/窗口控制）",
      tools=["ui_inspect", "ui_click", "ui_type", "ui_hotkey", "ui_window"])

SKILLS = ["ui_inspect", "ui_click", "ui_type", "ui_hotkey", "ui_window"]
SKILL_DESCRIPTION = "操作真实窗口界面（点击/输入/快捷键/窗口控制）"

# 可点击/可操作的控件类型
_ACTION_TYPES = (
    "Button", "Edit", "MenuItem", "ListItem", "TabItem", "Hyperlink",
    "CheckBox", "RadioButton", "ComboBox", "TreeItem", "DataItem", "Slider",
)

# 绝不代劳的危险动作（命中即拒绝）
_DANGER_WORDS = (
    "关机", "重启", "注销", "格式化", "卸载", "支付", "付款", "转账",
    "清空回收站", "删除全部", "恢复出厂",
)

_WINDOW_ACTIONS = {
    "激活": "focus", "切换": "focus", "显示": "focus", "前台": "focus",
    "最小化": "minimize", "最大化": "maximize", "还原": "restore",
    "关闭": "close", "关掉": "close",
}


def _desktop():
    # 工具在工作线程里执行：每个线程都要初始化 COM，否则 UIA 调用会失败
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _foreground_handle() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _visible_windows():
    """返回可见的顶层窗口列表 [(title, wrapper)]"""
    out = []
    try:
        for w in _desktop().windows():
            try:
                if not w.is_visible():
                    continue
                title = (w.window_text() or "").strip()
                if title:
                    out.append((title, w))
            except Exception:
                continue
    except Exception as e:
        print(f"[界面] 枚举窗口失败: {e}")
    return out


def _find_window(keyword: str):
    """按标题找窗口：精确 > 包含；无关键词时用前台窗口"""
    kw = _norm(keyword)
    wins = _visible_windows()
    if not kw:
        hwnd = _foreground_handle()
        for _t, w in wins:
            try:
                if w.handle == hwnd:
                    return w
            except Exception:
                continue
        return wins[0][1] if wins else None

    exact = [(t, w) for t, w in wins if _norm(t) == kw]
    if exact:
        return exact[0][1]
    partial = [(t, w) for t, w in wins if kw in _norm(t)]
    if not partial:
        return None
    # 越短越接近目标；前台窗口优先
    hwnd = _foreground_handle()
    partial.sort(key=lambda x: (x[1].handle != hwnd, len(x[0])))
    return partial[0][1]


def _window_title(win) -> str:
    try:
        return (win.window_text() or "").strip()
    except Exception:
        return ""


def _label(ctrl) -> str:
    try:
        name = (ctrl.element_info.name or "").strip()
    except Exception:
        name = ""
    if not name:
        try:
            name = (ctrl.window_text() or "").strip()
        except Exception:
            name = ""
    return name


def _aid(ctrl) -> str:
    try:
        return (ctrl.element_info.automation_id or "").strip()
    except Exception:
        return ""


def _controls(win, per_type: int = 30):
    """按类型逐个取控件，避免一次性遍历整棵大树"""
    seen = set()
    for t in _ACTION_TYPES:
        try:
            items = win.descendants(control_type=t)
        except Exception:
            continue
        for c in items[:per_type]:
            try:
                key = (c.element_info.control_type, _label(c), _aid(c))
                if key in seen:
                    continue
                seen.add(key)
                if not c.is_visible() or not c.is_enabled():
                    continue
                yield c
            except Exception:
                continue


def _find_control(win, target: str):
    """在窗口里按 名称/automation_id 找最匹配的控件"""
    t = _norm(target)
    if not t:
        return None
    best, best_score = None, 0
    for c in _controls(win):
        name = _norm(_label(c))
        aid = _norm(_aid(c))
        if not name and not aid:
            continue
        score = 0
        if name == t or aid == t:
            score = 100
        elif name.startswith(t) or aid.startswith(t):
            score = 80 - abs(len(name) - len(t))
        elif t in name or t in aid:
            score = 60 - abs(len(name) - len(t))
        elif name and name in t:
            score = 50 - abs(len(t) - len(name))
        if score > best_score:
            best, best_score = c, score
    return best


def _activate(win) -> bool:
    try:
        win.set_focus()
        time.sleep(0.15)
        return True
    except Exception:
        try:
            win.restore()
            win.set_focus()
            time.sleep(0.15)
            return True
        except Exception as e:
            print(f"[界面] 激活窗口失败: {e}")
            return False


def _danger(text: str) -> str:
    for w in _DANGER_WORDS:
        if w in (text or ""):
            return w
    return ""


def _clean_target(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^[的在里中]+", "", s)
    s = re.sub(r"(按钮|菜单|选项|标签|链接|图标|按键|项目|栏)$", "", s).strip()
    s = s.strip("「」\"“”'‘’ 《》<>")
    return s.strip()


_VERB_RE = r"(?:点击|单击|点一下|按一下|按下|点开|打开|输入|填写|打上|键入|打字)"
_CLICK_VERB_RE = r"(?:点击|单击|点一下|按一下|按下|点开|打开)"


def _window_split(q: str):
    """拆出 (窗口名, 剩余文本)。

    支持：
      在 记事本 里 输入：xxx
      点击记事本里的 保存
      记事本里输入：xxx
    """
    q = re.sub(r"^(?:帮我|请|麻烦|你)\s*", "", (q or "").strip())
    # ① 句首的「在 X 里」
    m = re.match(r"^在\s*(.+?)\s*(?:里|中|里面)", q)
    if m:
        return m.group(1).strip(" 「」\"“”'"), q[m.end():]
    # ② 「点击 X 里」（输入类动词不参与，避免把“输入框”拆成窗口）
    m = re.match(rf"^{_CLICK_VERB_RE}\s*(.+?)\s*(?:里|中|里面)", q)
    if m:
        return m.group(1).strip(" 「」\"“”'"), q[m.end():]
    # ③ 「X 里输入：xxx」（窗口名在前，动词在后）
    m = re.match(r"^(.{1,20}?)\s*(?:里|中|里面)", q)
    if m and re.search(_VERB_RE, q):
        return m.group(1).strip(" 「」\"“”'"), q[m.end():]
    return "", q


def _split_window_target(q: str):
    """从「在 记事本 里 点击 保存按钮」里拆出 (窗口, 目标)"""
    window, rest = _window_split(q)
    rest = re.sub(r"^(?:帮我|请|麻烦|你)?(?:去)?", "", rest.strip())
    rest = re.sub(rf"^{_VERB_RE}\s*", "", rest)
    rest = re.sub(r"^(?:的|中的|里的)\s*", "", rest)
    return window, _clean_target(rest)


# ── 工具实现 ────────────────────────────────────────────────

@tool(
    name="ui_inspect",
    description="查看某个窗口里有哪些可操作的控件（按钮/输入框/菜单/标签），用于确定该点哪里",
    keywords=["控件", "窗口里有什么", "有什么按钮", "界面元素", "看看窗口"],
)
def ui_inspect(query: str) -> str:
    q = (query or "").strip()
    # 优先匹配真实存在的窗口标题，避免把标题里的「的」当成语法词删掉
    kw = ""
    for t, _w in _visible_windows():
        if t and (t in q or _norm(t)[:10] in _norm(q)):
            kw = t
            break
    if not kw:
        kw = re.sub(r"(看看|查看|列出|什么|有哪些|窗口|界面|控件|元素|里|的|"
                    r"帮我|请|一下|呢|吗|？|\?|，|,|。)", "", q).strip()
    if not kw:
        wins = _visible_windows()
        if not wins:
            return "（系统信息：当前没有可见窗口）"
        lines = [f"- {t}" for t, _w in wins[:15]]
        return "（系统信息：当前可见窗口如下，需要看哪个我可以继续查）\n" + "\n".join(lines)

    win = _find_window(kw)
    if win is None:
        return f"（系统信息：没找到标题含「{kw}」的窗口）"
    if not _activate(win):
        return f"（系统信息：窗口「{_window_title(win)}」无法激活）"

    rows = []
    for c in _controls(win):
        try:
            ct = c.element_info.control_type or "Control"
            name = _label(c)[:40]
            aid = _aid(c)
            if not name and aid:
                name = f"#{aid}"
            r = c.rectangle()
            rows.append(f"- {ct}「{name}」({r.left},{r.top})")
            if len(rows) >= 40:
                break
        except Exception:
            continue
    if not rows:
        return (f"（系统信息：窗口「{_window_title(win)}」里没有读到标准控件，"
                f"可能是不支持 UI Automation 的程序）")
    return (f"（系统信息：窗口「{_window_title(win)}」的控件如下）\n"
            + "\n".join(rows))


@tool(
    name="ui_click",
    description="点击某个窗口里的按钮/菜单/选项，如「点击记事本里的保存」",
    keywords=["点击", "单击", "点一下", "按一下", "点开"],
)
def ui_click(query: str) -> str:
    q = (query or "").strip()
    window_kw, target = _split_window_target(q)
    if not target:
        return "（系统信息：没听清要点击哪个控件，请说明窗口和控件名）"
    bad = _danger(target)
    if bad:
        return f"（系统信息：涉及「{bad}」的操作需要你自己确认，我不代劳）"

    win = _find_window(window_kw)
    if win is None:
        return (f"（系统信息：没找到窗口「{window_kw}」）" if window_kw
                else "（系统信息：没找到当前前台窗口）")
    if not _activate(win):
        return f"（系统信息：窗口「{_window_title(win)}」无法激活）"

    ctrl = _find_control(win, target)
    if ctrl is None:
        return (f"（系统信息：在「{_window_title(win)}」里没找到「{target}」，"
                f"可以先用 ui_inspect 看看有哪些控件）")
    name = _label(ctrl) or target
    try:
        ctrl.invoke()
    except Exception:
        try:
            ctrl.click_input()
        except Exception as e:
            return f"（系统信息：点击「{name}」失败：{e}）"
    time.sleep(0.2)
    return (f"（系统信息：已在「{_window_title(win)}」点击「{name}」。"
            f"请用达妮娅的语气回应，不要提技术细节）")


def _field(win):
    """取窗口里第一个可输入的编辑框"""
    try:
        edits = win.descendants(control_type="Edit")
    except Exception:
        return None
    for e in edits:
        try:
            if e.is_visible() and e.is_enabled():
                return e
        except Exception:
            continue
    return None


def _paste(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception as e:
        print(f"[界面] 写剪贴板失败: {e}")
        return False
    try:
        from pywinauto.keyboard import send_keys
        send_keys("^v")
        return True
    except Exception as e:
        print(f"[界面] 粘贴失败: {e}")
        return False


@tool(
    name="ui_type",
    description="往某个窗口的输入框里打字（支持中文），如「在记事本里输入：你好」",
    keywords=["输入框", "打字", "填写", "键入"],
)
def ui_type(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "（系统信息：没听清要输入什么）"

    m = re.search(r"[:：]", q)
    if m:
        content = q[m.end():].strip()
    else:
        m2 = re.search(r"(?:输入|填写|打上|键入|打字)\s*(.+)", q)
        content = m2.group(1).strip() if m2 else q
    content = content.strip(" 「」\"“”'")
    if not content:
        return "（系统信息：要输入的内容是空的）"

    mw = _window_split(q)
    window_kw = mw[0]
    # 「在输入框里输入」中的“输入框”是控件不是窗口名
    if re.search(r"(输入框|搜索框|文本框|编辑框)$", window_kw):
        window_kw = ""
    win = _find_window(window_kw)
    if win is None:
        return (f"（系统信息：没找到窗口「{window_kw}」）" if window_kw
                else "（系统信息：没找到当前前台窗口）")
    if not _activate(win):
        return f"（系统信息：窗口「{_window_title(win)}」无法激活）"

    field = _field(win)
    if field is not None:
        try:
            field.set_focus()
        except Exception:
            pass
        try:
            field.set_edit_text(content)
            return (f"（系统信息：已在「{_window_title(win)}」的输入框写入内容。"
                    f"请用达妮娅的语气回应）")
        except Exception:
            try:
                field.click_input()
            except Exception:
                pass
    if not _paste(content):
        return "（系统信息：写输入框失败，剪贴板或焦点不可用）"
    time.sleep(0.15)
    return (f"（系统信息：已把文字粘贴到「{_window_title(win)}」。"
            f"请用达妮娅的语气回应）")


def _hotkey_combo(text: str):
    """'ctrl+shift+s' → '^+s'；不支持的键返回 None"""
    t = (text or "").lower().replace("＋", "+").replace("加", "+")
    parts = [p.strip() for p in re.split(r"[+\s]+", t) if p.strip()]
    if not parts:
        return None
    mods = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}
    combo = ""
    for p in parts[:-1]:
        if p not in mods:
            return None
        combo += mods[p]
    key = parts[-1]
    key_map = {"enter": "{ENTER}", "esc": "{ESC}", "tab": "{TAB}", "space": " ",
               "backspace": "{BACKSPACE}", "delete": "{DELETE}", "up": "{UP}",
               "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
               "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}",
               "pagedown": "{PGDN}", "f1": "{F1}", "f2": "{F2}", "f3": "{F3}",
               "f4": "{F4}", "f5": "{F5}", "f6": "{F6}", "f7": "{F7}",
               "f8": "{F8}", "f9": "{F9}", "f10": "{F10}", "f11": "{F11}",
               "f12": "{F12}"}
    return combo + key_map.get(key, key)


@tool(
    name="ui_hotkey",
    description="发送快捷键组合，如「按 ctrl+s 保存」「alt+tab 切换窗口」",
    keywords=["快捷键", "组合键", "按下 ctrl", "按 ctrl", "按 alt"],
)
def ui_hotkey(query: str) -> str:
    q = (query or "").strip()
    if "win" in q.lower():
        return "（系统信息：Win 组合键会影响系统，我不代按，请你自己操作）"
    m = re.search(r"(?:按下|按|发送|敲)\s*([a-zA-Z0-9+\s]+(?:\+\s*[a-zA-Z0-9]+)*)", q)
    raw = m.group(1).strip() if m else q
    combo = _hotkey_combo(raw)
    if combo is None:
        return f"（系统信息：快捷键「{raw}」暂时不支持，目前支持 ctrl/alt/shift + 字母或功能键）"
    try:
        from pywinauto.keyboard import send_keys
        send_keys(combo)
    except Exception as e:
        return f"（系统信息：发送快捷键失败：{e}）"
    return f"（系统信息：已发送快捷键 {raw}。请用达妮娅的语气回应）"


@tool(
    name="ui_window",
    description="激活/切换/最小化/最大化/还原/关闭窗口，如「最小化记事本」「切换到浏览器」",
    keywords=["最小化", "最大化", "还原窗口", "切换窗口", "激活窗口", "关闭窗口"],
)
def ui_window(query: str) -> str:
    q = (query or "").strip()
    action = None
    for k, v in _WINDOW_ACTIONS.items():
        if k in q:
            action = v
            break
    if action is None:
        return "（系统信息：要我对窗口做什么呢？可以激活、最小化、最大化、还原或关闭）"
    if action == "close" and _danger(q):
        return f"（系统信息：涉及「{_danger(q)}」的窗口我不关闭，请你自己确认）"

    rest = q
    for k in list(_WINDOW_ACTIONS) + ["窗口", "一下", "帮我", "请", "把", "去", "到"]:
        rest = rest.replace(k, "")
    kw = rest.strip(" 「」\"“”'，,。！!？?")
    kw = re.sub(r"^[的]+|[的]+$", "", kw).strip()
    win = _find_window(kw)
    if win is None:
        return (f"（系统信息：没找到窗口「{kw}」）" if kw
                else "（系统信息：没找到当前前台窗口）")
    title = _window_title(win)
    try:
        if action == "focus":
            ok = _activate(win)
        elif action == "minimize":
            win.minimize()
            ok = True
        elif action == "maximize":
            win.maximize()
            ok = True
        elif action == "restore":
            win.restore()
            ok = True
        else:
            win.close()
            ok = True
    except Exception as e:
        return f"（系统信息：操作窗口「{title}」失败：{e}）"
    if not ok:
        return f"（系统信息：窗口「{title}」无法激活）"
    time.sleep(0.2)
    names = {"focus": "已切到", "minimize": "已最小化", "maximize": "已最大化",
             "restore": "已还原", "close": "已关闭"}
    return (f"（系统信息：{names[action]}「{title}」。请用达妮娅的语气回应，"
            f"不要提技术细节）")
