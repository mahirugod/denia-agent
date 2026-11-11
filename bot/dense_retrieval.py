"""语料的向量后端：fastembed(onnxruntime) 优先，退 model2vec(纯 numpy)，再退词法。

两条都在本机实测过（bge-small-zh 512 维、potion-multilingual 256 维），
模型走 hf-mirror 下载、缓存在项目内 bot/data/models（不写 C 盘）。

加载是有成本的（fastembed 冷启约 9 秒、model2vec 约 30 秒），所以：
- warmup_async() 在后台线程预加载，不阻塞启动也不拖慢第一条消息
- embed() 在模型还没就绪时返回 None，调用方自动退回词法检索
- 语料向量按 (路径, mtime, 后端) 缓存成 .npy，重启不用重算

可用 settings.json 的 style_dense 控制：auto（默认，按顺序探测）/ off /
fastembed / model2vec。
"""

import hashlib
import json
import os
import threading

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BOT_DIR, "data")
_MODELS_DIR = os.path.join(_DATA_DIR, "models")
_EMB_DIR = os.path.join(_DATA_DIR, "style_dense")
os.environ.setdefault("HF_HOME", os.path.join(_MODELS_DIR, "hf"))

_FASTEMBED_MODEL = "BAAI/bge-small-zh-v1.5"
_MODEL2VEC_MODEL = "minishlab/potion-multilingual-128M"

_lock = threading.Lock()
_state = {"backend": None, "model": None, "loading": False, "tried": False}
np = None


def _numpy():
    global np
    if np is None:
        import numpy
        np = numpy
    return np


def _setting() -> str:
    try:
        with open(os.path.join(_BOT_DIR, "..", "settings.json"), encoding="utf-8") as f:
            return str(json.load(f).get("style_dense", "auto")).strip().lower()
    except Exception:
        return "auto"


def _load(force: str = ""):
    """按顺序加载后端；失败就换下一个。整个过程只跑一次。"""
    mode = force or _setting()
    if mode == "off":
        _state.update(backend=None, tried=True)
        return
    candidates = [mode] if mode in ("fastembed", "model2vec") else ["fastembed", "model2vec"]
    for name in candidates:
        try:
            if name == "fastembed":
                from fastembed import TextEmbedding
                _state["model"] = TextEmbedding(model_name=_FASTEMBED_MODEL,
                                                cache_dir=_MODELS_DIR)
            else:
                from model2vec import StaticModel
                _state["model"] = StaticModel.from_pretrained(_MODEL2VEC_MODEL)
            _state["backend"] = name
            print(f"[向量] 后端就绪: {name}")
            return
        except Exception as e:
            print(f"[向量] {name} 不可用: {type(e).__name__}: {str(e)[:80]}")
    _state.update(backend=None, model=None)
    print("[向量] 两个后端都不可用，退回词法检索")


def _ensure_loaded():
    if _state["tried"]:
        return
    with _lock:
        if _state["tried"]:
            return
        _state["loading"] = True
        _state["tried"] = True
    try:
        _load()
    finally:
        _state["loading"] = False


def warmup_async():
    """后台预加载，别拖慢第一条消息。"""
    if _state["tried"] or _state["loading"]:
        return
    threading.Thread(target=_ensure_loaded, daemon=True).start()


def ready() -> bool:
    return _state["backend"] is not None and _state["model"] is not None


def backend() -> str:
    return _state["backend"] or ""


def embed(texts):
    """返回 L2 归一化后的 float32 矩阵；模型没就绪时返回 None（不阻塞）。"""
    if not texts:
        return None
    if not _state["tried"]:
        warmup_async()
    if not ready():
        return None
    try:
        m = _state["model"]
        if _state["backend"] == "fastembed":
            v = _numpy().vstack([_numpy().asarray(x, dtype="float32") for x in m.embed(texts)])
        else:
            v = _numpy().asarray(m.encode(texts), dtype="float32")
        n = _numpy().linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1.0
        return (v / n).astype("float32")
    except Exception as e:
        print(f"[向量] 编码失败，本轮退回词法: {e}")
        return None


def corpus_embedding(path: str, groups: list):
    """语料向量：按 (内容, 后端) 缓存。返回 (矩阵, 后端名) 或 (None, "")。

    用内容哈希而不是 mtime：语料的清洗/改写规则一变，文本就变了，缓存必须失效。
    """
    if not groups:
        return None, ""
    if not ready():
        return None, ""
    key = _state["backend"] + "|" + "\x00".join(groups)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    os.makedirs(_EMB_DIR, exist_ok=True)
    cache = os.path.join(_EMB_DIR, f"{digest}.npy")
    try:
        if os.path.exists(cache):
            return _numpy().load(cache), _state["backend"]
    except Exception:
        pass
    v = embed(groups)
    if v is None:
        return None, ""
    try:
        _numpy().save(cache, v)
    except Exception as e:
        print(f"[向量] 缓存写入失败: {e}")
    return v, _state["backend"]
