#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill Pack 加载器（兼容仓颉 / Claude Code 的 SKILL.md 形式）

目录：bot/skill_packs/<包名>/SKILL.md
- 支持 YAML frontmatter 的 name / description
- 没有 frontmatter 时退回第一个一级标题作名字
达妮娅通过 list_skills / load_skill 两个工具按需读取，不预占上下文。
"""
import os
import re

PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill_packs")


def _parse(path: str):
    """返回 (name, description, body)"""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    body = raw
    name = desc = ""
    m = re.match(r"(?s)^---\s*\n(.*?)\n---\s*\n?(.*)$", raw)
    if m:
        meta, body = m.group(1), m.group(2)
        for line in meta.splitlines():
            k, _, v = line.partition(":")
            if k.strip().lower() == "name":
                name = v.strip().strip("\"'")
            elif k.strip().lower() == "description":
                desc = v.strip().strip("\"'")
    if not name:
        h = re.search(r"(?m)^#\s+(.+)$", body)
        name = h.group(1).strip() if h else os.path.basename(os.path.dirname(path))
    if not desc:
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("---"):
                desc = s[:80]
                break
    return name, desc, body.strip()


def _packs() -> list:
    if not os.path.isdir(PACKS_DIR):
        return []
    out = []
    for d in sorted(os.listdir(PACKS_DIR)):
        path = os.path.join(PACKS_DIR, d, "SKILL.md")
        if os.path.isfile(path):
            try:
                name, desc, body = _parse(path)
            except Exception:
                continue
            out.append((name, desc, d, body))
    return out


def index_text() -> str:
    """给 system prompt 的技能索引（名称 + 一句话描述）"""
    rows = []
    for name, desc, _, _ in _packs():
        d = re.sub(r"\s+", " ", desc).strip()
        if len(d) > 90:
            d = d[:90] + "…"
        rows.append(f"- {name}：{d}")
    return "\n".join(rows)


def list_skills() -> str:
    packs = _packs()
    if not packs:
        return "（还没装 Skill Pack。把 SKILL.md 放进 bot/skill_packs/<包名>/ 即可）"
    rows = []
    for name, desc, key, _ in packs:
        d = re.sub(r"\s+", " ", desc).strip()
        if len(d) > 160:
            d = d[:160] + "…"
        rows.append(f"- {name}：{d}（键名 {key}）")
    return "\n".join(rows)


def _find(key: str):
    key = (key or "").strip()
    if not key:
        return None
    for pname, desc, pkey, body in _packs():
        if key == pname or key == pkey or key.lower() in (pname.lower(), pkey.lower()):
            return pname, pkey, body
    return None


def load_skill(name: str, max_chars: int = 20000) -> str:
    """按名字/目录名取 SKILL.md 全文"""
    hit = _find(name)
    if not hit:
        return f"（没找到「{name}」这个技能，先调 list_skills 看看有哪些）"
    _, _, body = hit
    if len(body) > max_chars:
        return body[:max_chars] + "\n\n（后面还很长，先按这一部分做）"
    return body


def read_skill_file(name: str, path: str, max_chars: int = 12000) -> str:
    """读技能包内的附属文件（methodology/、templates/、scripts/ 等）"""
    hit = _find(name)
    if not hit:
        return f"（没找到「{name}」这个技能）"
    _, pkey, _ = hit
    base = os.path.realpath(os.path.join(PACKS_DIR, pkey))
    target = os.path.realpath(os.path.join(base, path or ""))
    if not (target == base or target.startswith(base + os.sep)):
        return "（不能读技能包以外的文件）"
    if not os.path.isfile(target):
        return f"（包里没有 {path}）"
    if not target.lower().endswith((".md", ".txt", ".yaml", ".yml", ".json", ".py")):
        return "（只支持读文本/脚本文件）"
    if os.path.getsize(target) > 512 * 1024:
        return "（文件太大，先读其中一部分吧）"
    try:
        with open(target, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return "（读文件失败）"
    return text[:max_chars] + ("\n\n（后面还很长）" if len(text) > max_chars else "")
