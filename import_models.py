#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型导入脚本：把已下载的模型搬进本项目 `models/` 目录
用法:
  python import_models.py                 # 自动检测 Ollama 默认模型库，导入全部模型
  python import_models.py qwen2.5:3b      # 只导入指定模型（可传多个）
  python import_models.py --from D:\\ollama_models   # 从指定目录导入
  python import_models.py --layout local --from <目录>  # 通用布局：导入到 models/local/<model_name>/（供非 Ollama 推理后端使用）
原理:
  - Ollama 模型库分两层: manifest(注册文件) + blobs(模型权重)
  - 本脚本把这两层都复制进 项目/models/，并保留 Ollama 内部布局
    (manifests/registry.ollama.ai/library/<model>/<tag> + blobs/sha256)
  - 导入完成后 process_manager 启动 Ollama 时会自动指向 项目/models/
  - --layout local 模式：把源目录整体拷到 models/local/<model_name>/，
    供非 Ollama 的兼容推理后端（LM Studio / vLLM / llama.cpp 等）配合
    settings.json 的 model_name + llm_url 使用
"""
import os
import sys
import shutil

_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_MODELS = os.path.join(_ROOT, "models")


def _ollama_default_dir() -> str:
    """Ollama 默认模型库位置（Windows: C:\\Users\\<user>\\.ollama\\models）。"""
    return os.path.join(os.path.expanduser("~"), ".ollama", "models")


def _find_source_dir(cli_arg: str = "") -> str:
    """确定源模型库目录。优先级: CLI 参数 > OLLAMA_MODELS 环境变量 > Ollama 默认位置。"""
    if cli_arg:
        if not os.path.isdir(cli_arg):
            sys.exit(f"源目录不存在: {cli_arg}")
        return cli_arg
    env_dir = os.environ.get("OLLAMA_MODELS", "").strip()
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    default = _ollama_default_dir()
    if os.path.isdir(default):
        return default
    sys.exit(
        "未找到 Ollama 模型库。\n"
        "请确认 Ollama 已安装并下载过模型，或用 --from 指定模型库目录:\n"
        "  python import_models.py --from <模型库目录>"
    )


def _list_models(source: str) -> list:
    """扫描源库，列出 [(模型名, tag)]。两种布局都支持：
      - 默认全局库:   {source}/registry.ollama.ai/library/<model>/<tag>(目录或 manifest 文件)
      - 重定向库:     {source}/manifests/registry.ollama.ai/library/<model>/<tag>(目录或 manifest 文件)
    """
    layouts = [
        os.path.join(source, "registry.ollama.ai", "library"),
        os.path.join(source, "manifests", "registry.ollama.ai", "library"),
    ]
    lib = next((p for p in layouts if os.path.isdir(p)), None)
    if lib is None:
        return []
    models = []
    for model in os.listdir(lib):
        mpath = os.path.join(lib, model)
        if not os.path.isdir(mpath):
            continue
        # tag 子目录（完整下载的模型，blobs 已就绪）
        tags = [t for t in os.listdir(mpath) if os.path.isdir(os.path.join(mpath, t))]
        # manifest 文件（{model}/<tag>.oml 或同名文件）
        if not tags:
            mf = [f for f in os.listdir(mpath) if os.path.isfile(os.path.join(mpath, f))]
            for f in mf:
                # 文件名形如 "qwen2.5" / "3b" / "qwen2.5:3b"
                if ":" in f:
                    mm, tt = f.split(":", 1)
                    models.append((mm, tt))
                elif f == model:
                    models.append((model, "latest"))
                else:
                    # 如 qwen2.5/3b 这种 manifest 文件：model=qwen2.5, tag=3b
                    models.append((model, f))
            if not mf:
                # 空目录（不完整的库）
                models.append((model, ""))
            continue
        for t in tags:
            models.append((model, t))
    return models


def _import_model(source: str, model: str, tag: str, dest: str) -> bool:
    """把单个模型（manifest + 其引用的 blobs）复制进项目 models/ 目录。
    步骤: 读 manifest JSON -> 收集 layer digests -> 复制 manifest + 对应 blobs。
    目标统一用「重定向库」布局: dest/manifests/registry.ollama.ai/library/<model>/<tag>
    + dest/blobs/sha256/<digest 去掉前缀>
    """
    tag_part = tag if tag else "latest"
    # 源 manifest 位置（两种布局）
    m_rel = os.path.join("registry.ollama.ai", "library", model, tag_part)
    m_src = None
    for prefix in ("", os.path.join("manifests")):
        cand = os.path.join(source, prefix, m_rel)
        if os.path.exists(cand):
            m_src = cand
            break
    if m_src is None:
        print(f"  ⚠ {model}:{tag_part} manifest 未找到，跳过")
        return False

    m_dst = os.path.join(dest, "manifests", m_rel)
    os.makedirs(os.path.dirname(m_dst), exist_ok=True)
    if os.path.isdir(m_src):
        shutil.copytree(m_src, m_dst, dirs_exist_ok=True)
    else:
        shutil.copy2(m_src, m_dst)
    print(f"  ✓ manifest: {model}:{tag_part}")

    # 解析 manifest，找出实际用到的 blobs（只有 manifest 文件才有 JSON）
    import json
    blobs_src = None
    for prefix in ("", os.path.join("manifests")):
        cand = os.path.join(source, prefix, "blobs")
        if os.path.isdir(cand):
            blobs_src = cand
            break
    if blobs_src is None:
        print(f"  ⚠ 源库无 blobs 目录，无法完成 {model}:{tag_part}")
        return False

    digests = []
    if os.path.isfile(m_src):
        try:
            mf = json.load(open(m_src, encoding="utf-8"))
            for layer in mf.get("layers", []):
                d = layer.get("digest", "")
                if d.startswith("sha256:"):
                    digests.append(d[7:])
        except Exception as e:
            print(f"  ⚠ 解析 manifest 失败({e})，全量复制 blobs")
    elif os.path.isdir(m_src):
        # tag 子目录布局下 manifest 在子目录内（.oml / manifest 文件）
        for f in os.listdir(m_src):
            fp = os.path.join(m_src, f)
            if os.path.isfile(fp):
                try:
                    mf = json.load(open(fp, encoding="utf-8"))
                    for layer in mf.get("layers", []):
                        d = layer.get("digest", "")
                        if d.startswith("sha256:"):
                            digests.append(d[7:])
                    break
                except Exception:
                    continue

    copied = 0
    # 两种源布局: {prefix}/blobs/sha256/<hex>  或  {prefix}/blobs/sha256-<hex>（平铺）
    def _find_blob(source_blobs_root, digest):
        for layout in (os.path.join(source_blobs_root, "sha256", digest),
                       os.path.join(source_blobs_root, f"sha256-{digest}")):
            if os.path.exists(layout):
                return layout
        return None

    if digests:
        for d in digests:
            spath = _find_blob(blobs_src, d)
            dpath = os.path.join(dest, "blobs", f"sha256-{d}")
            if spath is None:
                print(f"  ⚠ 缺少 blob: sha256-{d[:16]}...")
                continue
            os.makedirs(os.path.dirname(dpath), exist_ok=True)
            if not os.path.exists(dpath):
                shutil.copy2(spath, dpath)
            copied += 1
        print(f"  ✓ blobs: 复制 {copied} 个")
    else:
        # manifest 解析不了就全量复制（保底）
        all_blobs = os.path.join(blobs_src, "sha256")
        if not os.path.isdir(all_blobs):
            # 平铺布局：blobs/ 直接放 sha256-xxx 文件
            all_blobs = blobs_src
        for b in os.listdir(all_blobs) if os.path.isdir(all_blobs) else []:
            spath = os.path.join(all_blobs, b)
            # 统一成 平铺 命名: sha256-<hex>
            if b.startswith("sha256-"):
                dpath = os.path.join(dest, "blobs", b)
            else:
                dpath = os.path.join(dest, "blobs", f"sha256-{b}")
            os.makedirs(os.path.dirname(dpath), exist_ok=True)
            if not os.path.exists(dpath):
                shutil.copy2(spath, dpath)
            copied += 1
        print(f"  ✓ blobs: 全量复制 {copied} 个")
    return True


def _import_local_layout(source: str, model: str) -> bool:
    """通用布局：把源目录（或源目录里指定模型子目录）整体拷到 models/local/<model>/，
    供非 Ollama 兼容推理后端使用。"""
    dest_root = os.path.join(_PROJECT_MODELS, "local")
    dest = os.path.join(dest_root, model)
    os.makedirs(dest, exist_ok=True)
    copied = 0
    for entry in os.listdir(source):
        spath = os.path.join(source, entry)
        dpath = os.path.join(dest, entry)
        if os.path.isdir(spath):
            shutil.copytree(spath, dpath, dirs_exist_ok=True)
        else:
            shutil.copy2(spath, dpath)
        copied += 1
    print(f"  ✓ 已复制 {copied} 项到 {dest}")
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="导入模型到项目 models/ 目录（Ollama 布局或通用 local/ 布局）")
    parser.add_argument("models", nargs="*", help="要导入的模型名（如 qwen2.5:3b），不传则导入全部")
    parser.add_argument("--from", dest="src_dir", default="", help="指定源模型库目录")
    parser.add_argument("--layout", dest="layout", default="ollama", choices=["ollama", "local"],
                        help="导入布局：ollama（默认，重定向库布局）或 local（通用，供非 Ollama 推理后端使用）")
    args = parser.parse_args()

    if args.layout == "local":
        # 通用布局：直接整体复制源目录到 models/local/<model>/
        source = args.src_dir or _find_source_dir("")
        model = args.models[0] if args.models else os.path.basename(os.path.normpath(source))
        print(f"源目录: {source}")
        print(f"目标: models/local/{model}/")
        _import_local_layout(source, model)
        print(f"\n✅ 导入完成（local 布局）。把 settings.json 的 model_name 设为 \"{model}\"，"
              f"llm_url 指向你的推理后端即可使用。")
        return

    source = _find_source_dir(args.src_dir)
    print(f"源模型库: {source}")
    all_models = _list_models(source)
    if not all_models:
        sys.exit("源模型库中没有可导入的模型。先运行 `ollama pull <模型>` 再导入。")

    wanted = set(args.models) if args.models else None
    todo = [(m, t) for (m, t) in all_models if (not wanted or f"{m}:{t}" in wanted or m in wanted)]

    if not todo:
        print("指定的模型不在源库中，未导入任何模型。")
        print(f"源库可用: {', '.join(m + (':' + t if t else '') for m, t in all_models)}")
        return

    print(f"目标目录: {_PROJECT_MODELS}")
    print(f"待导入: {', '.join(m + (':' + t if t else '') for m, t in todo)}")
    for m, t in todo:
        _import_model(source, m, t, _PROJECT_MODELS)
    print(f"\n✅ 导入完成。重启达妮娅终端后 Ollama 会从 {_PROJECT_MODELS} 加载。")
    print("   说「有哪些模型」可让达妮娅列出可用模型。")


if __name__ == "__main__":
    main()
