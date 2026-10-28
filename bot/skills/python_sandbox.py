#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：python_sandbox
给达妮娅一个受限的 Python 执行工具（跑脚本、算数据、验证代码）。

不是 OS 级隔离，而是"软沙箱"：
- 独立解释器 `-I -B`，最小环境变量，不继承 PYTHONPATH
- 工作目录限制在 bot/ 之内，临时脚本放在 bot/sandbox/
- 超时强杀（默认 20s，最多 60s），输出截断
- 禁止 os.system / os.popen / shutil.rmtree / ctypes / socket / winreg 等危险调用
"""
import os
import re
import subprocess
import sys
import time
import uuid

from skills import skill
from tool_registry import tool

skill("python_sandbox", "受限执行 Python 代码/脚本", tools=["run_python"])

SKILLS = ["run_python"]
SKILL_DESCRIPTION = "受限执行 Python 代码/脚本"

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX_DIR = os.path.join(BOT_DIR, "sandbox")

_DENY = re.compile(
    r"(os\.(system|popen|startfile)\s*\(|shutil\.rmtree\s*\(|"
    r"^\s*(?:import|from)\s+(ctypes|socket|winreg|_winapi)\b|"
    r"__import__\s*\(\s*[\"'](?:ctypes|socket|winreg))",
    re.M)

_MAX_CODE = 20000
_MAX_OUT = 4000


def _within_bot(path: str) -> str:
    real = os.path.realpath(os.path.abspath(path))
    base = os.path.realpath(BOT_DIR)
    if real != base and not real.startswith(base + os.sep):
        return ""
    return real


@tool(
    name="run_python",
    description=(
        "在受限环境里执行一段 Python 代码，或运行现成的 .py 脚本（可带参数），返回 stdout/stderr。"
        "用于计算、验证代码、跑 Skill Pack 里的脚本（例如 cangjie.py）。"
        "只能用 bot 目录内的文件；默认 20 秒超时，最多 60 秒；不能执行系统命令或删除目录。"
    ),
    keywords=["跑一下代码", "执行python", "运行脚本", "跑脚本"],
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码（与 script 二选一）"},
            "script": {"type": "string", "description": "要运行的 .py 路径，相对 bot/，如 skill_packs/cangjie-skill/scripts/cangjie.py"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "传给脚本的参数"},
            "cwd": {"type": "string", "description": "工作目录，相对 bot/，默认 sandbox/"},
            "timeout": {"type": "integer", "description": "超时秒数，5~60，默认 20"},
        },
    },
)
def run_python(code: str = "", script: str = "", args=None, cwd: str = "", timeout: int = 20) -> str:
    """受限执行 Python"""
    if not code and not script:
        return "（要给我代码或脚本路径）"
    if code and script:
        return "（code 和 script 只能给一个）"

    try:
        timeout = max(5, min(60, int(timeout or 20)))
    except Exception:
        timeout = 20

    os.makedirs(SANDBOX_DIR, exist_ok=True)
    workdir = _within_bot(os.path.join(BOT_DIR, cwd)) if cwd else SANDBOX_DIR
    if not workdir or not os.path.isdir(workdir):
        return "（工作目录只能是 bot/ 里已有的目录）"

    tmp = ""
    if code:
        if len(code) > _MAX_CODE:
            return "（代码太长，精简一下）"
        hit = _DENY.search(code)
        if hit:
            return f"（这段代码里有不允许的调用：{hit.group(1) or hit.group(0)}）"
        tmp = os.path.join(SANDBOX_DIR, f"_run_{uuid.uuid4().hex[:8]}.py")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return f"（写临时脚本失败：{e}）"
        target = [sys.executable, "-I", "-B", "-u", tmp]
    else:
        path = _within_bot(os.path.join(BOT_DIR, script))
        if not path or not os.path.isfile(path):
            return "（脚本路径不对，只能是 bot/ 里已有的 .py）"
        target = [sys.executable, "-I", "-B", "-u", path]

    if args:
        target += [str(a) for a in args]

    env = {
        "SYSTEMROOT": os.environ.get("SystemRoot", r"C:\Windows"),
        "PATH": os.path.dirname(sys.executable),
        "PYTHONIOENCODING": "utf-8",
        "TEMP": SANDBOX_DIR,
        "TMP": SANDBOX_DIR,
    }

    print(f"[sandbox] 执行 {os.path.basename(target[4] if code else path)} "
          f"cwd={os.path.relpath(workdir, BOT_DIR)} timeout={timeout}s")
    started = time.time()
    try:
        proc = subprocess.run(
            target, cwd=workdir, env=env, timeout=timeout,
            capture_output=True, creationflags=0x08000000,
        )
    except subprocess.TimeoutExpired:
        return f"（跑超过 {timeout} 秒，已经掐掉了）"
    except Exception as e:
        return f"（执行失败：{e}）"
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass

    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    cost = time.time() - started
    parts = [f"exit={proc.returncode} time={cost:.1f}s"]
    if out.strip():
        parts.append("stdout:\n" + out[-_MAX_OUT:])
    if err.strip():
        parts.append("stderr:\n" + err[-_MAX_OUT:])
    if not out.strip() and not err.strip():
        parts.append("（没有输出）")
    return "\n".join(parts)
