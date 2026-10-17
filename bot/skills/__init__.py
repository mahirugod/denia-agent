#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达妮娅技能包（Skills）—— 把工具按"域"拆成自包含模块，统一发现/导入/路由。

每个技能包是一个 Python 文件（本包内），内部用 `@skill` 声明技能，
可选导出 `register` 钩子注入额外工具。加载器：
  1. 扫描本目录 *.py（跳过 _ 前缀与 __init__）
  2. import 触发 @skill 注册（自动挂到 tool_registry 的 _TOOLS）
  3. 收集 _SKILL_META（元信息，供 harness 工具清单/UI 展示）
  4. 调用模块的 register()（若存在），支持自定义注册逻辑

对外 API（保持与旧 dania_tools 兼容，供 main.py / chat_controller.py 使用）：
  - load_skills()  导入全部技能包
  - get_skill_meta()  取技能元信息
  - set_reminder_callback / get_active_reminders / fire_reminder_for_test  提醒转发
"""
import importlib
import json
import os
import sys

_SKILLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REGISTERED = []  # 已加载的技能包名（去重）
_SKIPPED = []    # 被跳过的技能包名（disabled / harness 不够 / 依赖缺失）
_META = {}  # 技能包名 -> {"name","description","tools":[...]}
_reminder_callback = None
_DISABLED_FILE = os.path.join(_SKILLS_DIR, "disabled.json")

# harness 档位排序（tiny < small < standard）
_HARNESS_RANK = {"tiny": 0, "small": 1, "standard": 2}


def _load_yaml(path: str) -> dict:
    """读 skill.yaml（仅支持简单 key: value，无嵌套解析）。
    值以 [ 开头视为列表（逗号分隔），其余按字符串。
    """
    if not os.path.exists(path):
        return {}
    data = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                val = val.strip()
                key = key.strip()
                # 空列表 [] 或 "[]" → 空
                if val == "[]" or val == "''" or val == '""' or val == "":
                    data[key] = ""
                # 列表：[a, b] 或 ["a", "b"]
                elif val.startswith("[") and val.endswith("]"):
                    inner = val[1:-1]
                    data[key] = ",".join(
                        x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()
                    )
                else:
                    data[key] = val.strip('"').strip("'")
    return data


def _load_disabled() -> dict:
    """读 disabled.json，返回 {技能名: bool}。"""
    if not os.path.exists(_DISABLED_FILE):
        return {}
    try:
        with open(_DISABLED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_skill_yaml(pkg_name: str) -> dict:
    """读某技能的 skill.yaml，返回 dict（可能含 name/description/version/depends/min_harness/tools）。"""
    yaml_path = os.path.join(_SKILLS_DIR, pkg_name, "skill.yaml")
    if os.path.exists(yaml_path):
        return _load_yaml(yaml_path)
    # 兼容：扁平结构下 skill.yaml 和 .py 同目录
    yaml_path_flat = os.path.join(_SKILLS_DIR, f"{pkg_name}.skill.yaml")
    if os.path.exists(yaml_path_flat):
        return _load_yaml(yaml_path_flat)
    return {}


def skill(pkg_name: str, description: str, tools: list = None):
    """
    技能包内声明一条技能域（供模型慢路径/工具清单识别）。
    用法：在技能包模块顶部调 `skill("weather", "查询实时天气")`。
    pkg_name 必须与技能包模块名一致（如 weather.py 用 "weather"），
    这样 _META 以模块名为唯一 key，不会重复。
    """
    global _META
    entry = _META.setdefault(pkg_name, {"name": pkg_name, "description": description, "tools": []})
    if tools:
        for t in tools:
            if t not in entry["tools"]:
                entry["tools"].append(t)
    return entry


def _register_meta(pkg_name: str, tools: list, description: str = ""):
    global _META
    _META[pkg_name] = {
        "name": pkg_name,
        "description": description,
        "tools": tools,
    }


def get_skill_meta() -> dict:
    return {k: dict(v) for k, v in _META.items()}


def load_skills() -> list:
    """扫描并导入本目录下所有技能包，返回已注册的技能包名列表。
    支持 skill.yaml 门控：disabled / min_harness / depends 检查。
    """
    global _REGISTERED, _SKIPPED
    # 确保包路径在 sys.path（直接执行时）
    if _SKILLS_DIR not in sys.path:
        sys.path.insert(0, _SKILLS_DIR)

    disabled_map = _load_disabled()
    current_harness = _get_current_harness()

    for fn in sorted(os.listdir(_SKILLS_DIR)):
        if not fn.endswith(".py") or fn.startswith("_") or fn == "__init__.py":
            continue
        pkg = fn[:-3]
        if pkg in _REGISTERED:
            continue

        # 读 skill.yaml 门控
        meta = _load_skill_yaml(pkg)
        skill_name = meta.get("name", pkg)

        # ① disabled 检查
        if disabled_map.get(skill_name, False):
            _SKIPPED.append(pkg)
            print(f"[技能包] 跳过 {pkg}（disabled）")
            continue

        # ② min_harness 检查
        min_h = meta.get("min_harness", "").strip()
        if min_h and min_h in _HARNESS_RANK:
            if _HARNESS_RANK.get(current_harness, 2) < _HARNESS_RANK[min_h]:
                _SKIPPED.append(pkg)
                print(f"[技能包] 跳过 {pkg}（harness {current_harness} < min_harness {min_h}）")
                continue

        # ③ depends 检查（被依赖技能必须已加载）
        dep_raw = meta.get("depends", "")
        if dep_raw:
            deps = [d.strip() for d in dep_raw.split(",") if d.strip()]
            missing = [d for d in deps if d not in _REGISTERED and d != pkg]
            if missing:
                _SKIPPED.append(pkg)
                print(f"[技能包] 跳过 {pkg}（缺少依赖 {missing}）")
                continue

        try:
            mod = importlib.import_module(pkg)
            _REGISTERED.append(pkg)
            # 元信息：skill() 已用模块名登记 _META，这里只补全未声明的
            if pkg not in _META:
                desc = getattr(mod, "SKILL_DESCRIPTION", "")
                tools_here = getattr(mod, "SKILLS", None) or []
                _register_meta(pkg, tools_here, desc)
            # skill.yaml 补全版本信息
            version = meta.get("version", "").strip()
            if version:
                _META.setdefault(pkg, {})["version"] = version
            # 可选钩子：自定义注册逻辑
            if hasattr(mod, "register"):
                mod.register()
            tools_here = getattr(mod, "SKILLS", None) or []
            print(f"[技能包] 已加载 {pkg} v{version or '?'}（工具 {tools_here or '(由模块声明)'}）")
        except Exception as e:
            print(f"[技能包] 加载 {pkg} 失败: {e}")
    return list(_REGISTERED)


def _get_current_harness() -> str:
    """获取当前 harness 档位（读 settings.json 或默认 standard）。"""
    try:
        settings_path = os.path.join(os.path.dirname(_SKILLS_DIR), "settings.json")
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("harness", "standard")
    except Exception:
        pass
    return "standard"


# ── 提醒 API 转发（保持 dania_tools 旧接口）──────────────────────────
def set_reminder_callback(callback):
    global _reminder_callback
    _reminder_callback = callback
    try:
        from importlib import import_module
        m = import_module("reminder")
        if hasattr(m, "_set_callback"):
            m._set_callback(callback)
    except Exception:
        pass


def get_active_reminders() -> list:
    try:
        from importlib import import_module
        m = import_module("reminder")
        return m.get_active_reminders()
    except Exception:
        return []


def fire_reminder_for_test(content: str):
    try:
        from importlib import import_module
        m = import_module("reminder")
        m.fire_reminder_for_test(content)
    except Exception:
        pass


# 首次导入即自动加载全部技能包
load_skills()
