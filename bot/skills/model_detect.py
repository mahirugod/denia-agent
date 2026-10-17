#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：本地模型检测（model_detect）
扫描项目 models/ 文件夹，列出可用本地模型。
支持两种布局（Ollama 重定向库 / 通用 local 子目录），兼容 Ollama 但解绑 Ollama。
达妮娅可在对话中报出可用模型清单，供用户切换。
"""
import os

from skills import skill
from tool_registry import tool

skill("model_detect", "检测本地 models 文件夹中有哪些模型可用", tools=["model_detect"])

SKILLS = ["model_detect"]
SKILL_DESCRIPTION = "检测本地 models 文件夹中有哪些模型可用"

OLLAMA_PORT = 11434
# 本地模型库目录（process_manager / main.py 通过 OLLAMA_MODELS 环境变量指向项目内 models/）
_MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models")


def _detect_models_dir() -> str:
    """模型库目录：优先项目内 models/，其次 OLLAMA_MODELS 环境变量。
    注意：机器上可能残留指向其它路径的全局 OLLAMA_MODELS，
    但终端实际用的是项目内 models/，所以项目目录优先。"""
    if os.path.isdir(_MODELS_DIR):
        return _MODELS_DIR
    env_dir = os.environ.get("OLLAMA_MODELS", "").strip()
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    return _MODELS_DIR


def _list_models_from_folder(models_dir: str) -> list:
    """扫描 models/ 文件夹，支持两种布局：
      1. Ollama 重定向库: {dir}/manifests/registry.ollama.ai/library/{model}/{tag}
      2. 通用 local 子目录: {dir}/local/{model_name}（Ollama 兼容或任意后端）
    返回模型名列表（如 ["qwen2.5:3b"]）。"""
    names = set()
    # 布局 1：Ollama 重定向库
    p1 = os.path.join(models_dir, "manifests", "registry.ollama.ai", "library")
    if os.path.isdir(p1):
        for model in os.listdir(p1):
            mpath = os.path.join(p1, model)
            if not os.path.isdir(mpath):
                continue
            tags = [t for t in os.listdir(mpath) if os.path.isdir(os.path.join(mpath, t))]
            if tags:
                for t in sorted(tags):
                    names.add(f"{model}:{t}")
            else:
                manifest_files = [f for f in os.listdir(mpath) if os.path.isfile(os.path.join(mpath, f))]
                if manifest_files:
                    for f in sorted(manifest_files):
                        if ":" in f:
                            names.add(f)
                        elif f != model:
                            names.add(f"{model}:{f}")
                        else:
                            names.add(f"{model}:latest")
                else:
                    names.add(model)
    # 布局 2：通用 local 子目录
    p2 = os.path.join(models_dir, "local")
    if os.path.isdir(p2):
        for item in sorted(os.listdir(p2)):
            ipath = os.path.join(p2, item)
            if os.path.isdir(ipath):
                # local/<model_name>/ 目录
                names.add(item)
            elif os.path.isfile(ipath) and item not in ("README", "LICENSE"):
                # local/<model_name>.gguf 等文件，取文件名（去扩展名）
                base = os.path.splitext(item)[0]
                if base and not base.startswith("."):
                    names.add(base)
    return sorted(names)


def detect_local_models(models_dir: str = "") -> list:
    """
    只扫描本地 models/ 文件夹，不依赖任何模型服务（Ollama 或别的）。
    用户需把模型放进 models/manifests/registry.ollama.ai/library/{model}/{tag}
    或 models/local/{model_name}。
    返回模型名列表（如 ["qwen2.5:3b"]）。
    """
    return _list_models_from_folder(models_dir or _detect_models_dir())


# 兼容旧名（main.py / 设置页还在 import detect_ollama_models）
def detect_ollama_models(models_dir: str = "") -> list:
    return detect_local_models(models_dir)


@tool(
    name="model_detect",
    description="检测本地 models 文件夹中有哪些模型可用",
    keywords=["模型", "模型列表", "有哪些模型", "检测模型", "本地模型", "可用模型"],
)
def model_detect(query: str) -> str:
    """返回一段「可用模型清单」文本，供达妮娅以人设转述。"""
    names = detect_local_models()
    models_dir = _detect_models_dir()
    if not names:
        return (
            f"（模型检测：没检测到本地模型。models 文件夹 {models_dir} 不存在或为空。"
            "请用达妮娅的语气告诉用户：本地暂时没有可用模型，"
            "可以把模型放进 models/ 文件夹（Ollama 布局或 local 子目录），"
            "或者直接用云端模型（在设置页填云端 Key/URL/模型名）。）"
        )
    model_list = "、".join(names)
    return (
        f"（模型检测：本地 models 文件夹共 {len(names)} 个模型：{model_list}。"
        "请用达妮娅的语气告诉用户可用模型，并说明切换方法：在设置页选模型，"
        "或告诉达妮娅要切哪个。本地模型需要对应的推理后端（如 Ollama 服务）在跑才能切换。）"
    )


def register():
    """加载钩子：启动自检（后台静默跑，打印检测结果）。"""
    import threading
    threading.Thread(target=_startup_check, daemon=True).start()


def _startup_check():
    import time
    time.sleep(1.5)  # 等 process_manager 启动
    try:
        names = detect_local_models()
        if names:
            print(f"[模型检测] 本地 models 可用模型: {'、'.join(names)}（目录 {_detect_models_dir()}）")
        else:
            print(f"[模型检测] 未检测到本地模型（目录 {_detect_models_dir()}）")
    except Exception as e:
        print(f"[模型检测] 启动自检失败: {e}")
