#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：skills_loader
让达妮娅按需读取本地 Skill Pack（仓颉 / Claude Code 的 SKILL.md 形式）。

不预先往 prompt 里塞全文：先给索引（名称+描述），需要时再用 load_skill 取步骤。
"""
from skills import skill
from tool_registry import tool
import skill_loader

skill("skills_loader", "读取本地 Skill Pack", tools=["list_skills", "load_skill", "read_skill_file"])

SKILLS = ["list_skills", "load_skill", "read_skill_file"]
SKILL_DESCRIPTION = "读取本地 Skill Pack"


@tool(
    name="list_skills",
    description=(
        "列出达妮娅本地安装的 Skill Pack（方法库）：每项有名称和一句话说明。"
        "被问到「你会什么技能」「有没有做 XX 的方法」或者要按某套方法做事时先调用它。"
    ),
    keywords=[],
    parameters={"type": "object", "properties": {}},
)
def list_skills() -> str:
    """列出本地技能包"""
    return skill_loader.list_skills()


@tool(
    name="load_skill",
    description=(
        "读取某个 Skill Pack 的完整内容（触发条件、步骤、边界、示例）。"
        "读到后按里面的步骤调用已有工具执行；不要只把内容念给用户。"
    ),
    keywords=[],
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名或目录名（可先用 list_skills 查）",
            },
        },
        "required": ["name"],
    },
)
def load_skill(name: str = "") -> str:
    """读取技能包全文"""
    return skill_loader.load_skill(name)


@tool(
    name="read_skill_file",
    description=(
        "读取某个 Skill Pack 里的附属文件（例如 methodology/00-overview.md、templates/xxx.md）。"
        "SKILL.md 里写到「详见 xxx」时用它把那部分读出来，不要凭空猜测。"
    ),
    keywords=[],
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名或目录名"},
            "path": {"type": "string", "description": "包内相对路径，如 methodology/00-overview.md"},
        },
        "required": ["name", "path"],
    },
)
def read_skill_file(name: str = "", path: str = "") -> str:
    """读取技能包附属文件"""
    return skill_loader.read_skill_file(name, path)
