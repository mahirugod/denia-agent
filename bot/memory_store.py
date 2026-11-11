#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长期记忆（一期）：情节记忆 + 会话摘要 + 检索 + 遗忘打分。

设计约束：
- **不自己调模型**。抽取/摘要的时机由调用方控制（会话空闲或结束时），
  这里只提供提示词构造与解析，避免把额外的请求塞进回复路径（429 压力）。
- **只存用户说过的话**。抽取提示词里明确禁止模型加工成"她记得的事"，
  防止出现编造经历那类问题。
- 检索复用 dense_retrieval（fastembed）；模型没就绪时退回字二元组重合度。
"""
import json
import os
import re
import threading
import time
import hashlib

try:
    import dense_retrieval
except Exception:  # 向量后端不可用时也要能跑
    dense_retrieval = None

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
_EPISODES = os.path.join(_DIR, "episodes.jsonl")
_SUMMARY_DIR = os.path.join(_DIR, "summaries")
_VEC_PATH = os.path.join(_DIR, "vectors.npy")
_VEC_META = os.path.join(_DIR, "vectors_meta.json")
_ARCHIVE = os.path.join(_DIR, "episodes_archive.jsonl")
_build_lock = threading.Lock()
_building = {"on": False}
# 情节文件与向量缓存都会被多线程碰（回复线程 + 空闲抽取 + 退出钩子）。
# Windows 上 os.replace 撞上未关闭的读句柄会抛 PermissionError，所以统一串行化。
_io_lock = threading.RLock()

# 遗忘曲线：score = 重要度 * exp(-Δt / S)，S 随命中次数增长（间隔重复）
_BASE_STABLE_DAYS = 7.0


def _ensure_dir():
    os.makedirs(_SUMMARY_DIR, exist_ok=True)


# ── 情节记忆 ──
def _ep_id(text: str, ts: float) -> str:
    return hashlib.sha1(f"{ts}|{text}".encode("utf-8")).hexdigest()[:12]


def add_episode(text: str, kind: str = "event", mood: str = "",
                importance: int = 3, ts: float = None) -> dict:
    """追加一条情节。importance 1-5，决定遗忘速度。"""
    text = (text or "").strip()
    if not text:
        return {}
    _ensure_dir()
    ts = ts if ts is not None else time.time()
    ep = {
        "id": _ep_id(text, ts),
        "ts": round(ts, 3),
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
        "kind": kind,               # event / promise / preference / mood
        "mood": mood,
        "importance": max(1, min(5, int(importance))),
        "text": text,
        "hits": 0,
        "last_hit": 0,
    }
    with _io_lock:
        with open(_EPISODES, "a", encoding="utf-8") as f:
            f.write(json.dumps(ep, ensure_ascii=False) + "\n")
    return ep


def load_episodes() -> list:
    if not os.path.exists(_EPISODES):
        return []
    out = []
    try:
        with _io_lock:
            with open(_EPISODES, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
    except Exception:
        pass
    return out


def _save_episodes(eps: list):
    _ensure_dir()
    tmp = _EPISODES + ".tmp"
    with _io_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            for ep in eps:
                f.write(json.dumps(ep, ensure_ascii=False) + "\n")
        os.replace(tmp, _EPISODES)


def save_episodes(eps: list):
    """对外暴露的整表写回（整理模块 / 手工修复用）。"""
    _save_episodes(eps)


def active_episodes(eps: list = None) -> list:
    """参与召回的情节：合并过的、已过时的都不再出现。"""
    eps = load_episodes() if eps is None else eps
    return [e for e in eps if not e.get("superseded") and not e.get("merged_into")]


def score(ep: dict, now: float = None) -> float:
    """记忆强度：重要度 + 相对时间衰减 + 命中加成。

    稳定期 S = 7 天 × 重要度 × (1 + 命中次数)：越重要衰减越慢，
    被反复回忆的还会进一步延长，对应"间隔重复"。
    """
    now = now or time.time()
    days = max(0.0, (now - float(ep.get("ts") or 0)) / 86400.0)
    imp = float(ep.get("importance") or 3)
    stable = _BASE_STABLE_DAYS * imp * (1.0 + float(ep.get("hits") or 0))
    return imp * pow(2.718281828, -days / stable)


# ── 会话摘要 ──
def add_summary(key: str, line: str, replace: bool = False):
    """把一条摘要追加到 data 层（key = 会话标识，如群号/私聊 QQ）。"""
    line = (line or "").strip()
    if not key or not line:
        return
    _ensure_dir()
    path = os.path.join(_SUMMARY_DIR, f"{_safe(key)}.md")
    mode = "w" if replace else "a"
    with _io_lock:
        with open(path, mode, encoding="utf-8") as f:
            if replace:
                f.write(line + "\n")
            else:
                f.write(f"- [{time.strftime('%Y-%m-%d')}] {line}\n")


def read_summary(key: str, max_chars: int = 600) -> str:
    path = os.path.join(_SUMMARY_DIR, f"{_safe(key)}.md")
    try:
        with _io_lock:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    out, used = [], 0
    for ln in reversed(text.splitlines()):
        if used + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(reversed(out))


def _safe(key: str) -> str:
    return re.sub(r"[^\w\-]+", "_", str(key))[:40] or "default"


# ── 检索 ──
def _grams(text: str) -> set:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", text or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} | set(s)


def _lexical_scores(query: str, eps: list) -> list:
    q = _grams(query)
    if not q:
        return [0.0] * len(eps)
    return [len(q & _grams(ep["text"])) / len(q) for ep in eps]


# ── 情绪一致的记忆更容易被想起 ──
# 只做关键词判断：代价为零，且对已经入库的旧情节同样生效（不需要重新抽取）。
_NEG_WORDS = ("累", "难过", "烦", "哭", "失眠", "加班", "生气", "失望", "难受",
              "压力", "焦虑", "委屈", "生病", "疼", "吵架", "崩溃", "孤独", "想哭")
_POS_WORDS = ("开心", "高兴", "喜欢", "约好", "一起", "顺利", "惊喜", "满足",
              "期待", "甜蜜", "笑", "通过", "成功", "放假", "奖励")
# 否定形式要先识别并"遮住"，否则"不喜欢"会被当成"喜欢"
_NEG_PHRASES = ("不喜欢", "不想要", "不想", "不开心", "不高兴", "不顺利",
                "不期待", "不愿意", "没成功", "不满足", "没通过")


def valence(text: str) -> int:
    """-1 消极 / 0 中性 / 1 积极。

    "加班的时候很开心"这种正负词同时出现的句子，用**靠后的那个**定调：
    中文小句通常把结论/情绪放在后面。否定短语（"不喜欢"）先遮掉再判。
    """
    t = text or ""
    masked = t
    neg_at = -1
    for w in _NEG_PHRASES:
        i = t.rfind(w)
        if i >= 0:
            neg_at = max(neg_at, i)
            masked = masked.replace(w, "\x00" * len(w))  # 等长替换，位置不变
    neg_at = max(neg_at, max((masked.rfind(w) for w in _NEG_WORDS), default=-1))
    pos_at = max((masked.rfind(w) for w in _POS_WORDS), default=-1)
    if neg_at == pos_at:
        return 0
    return -1 if neg_at > pos_at else 1


def _mood_boost(ep: dict, mood: float) -> float:
    """心情差时更容易想起难过的事，反之亦然；反向情绪的记忆会被压一点。"""
    if not mood:
        return 1.0
    cur = -1 if mood <= -3 else (1 if mood >= 3 else 0)
    if not cur:
        return 1.0
    ep_val = valence(ep.get("text", "")) or valence(ep.get("mood", ""))
    if not ep_val:
        return 1.0
    return 1.20 if ep_val == cur else 0.85


def _vector_matrix(eps: list):
    """情节向量矩阵（按内容哈希缓存）。缓存未命中时后台重建，本轮先用词法。"""
    if dense_retrieval is None:
        return None
    if not dense_retrieval.ready():
        dense_retrieval.warmup_async()
        return None
    digest = hashlib.sha1(
        (dense_retrieval.backend() + "|" + "\x00".join(e["text"] for e in eps)).encode("utf-8")
    ).hexdigest()[:16]
    try:
        with _io_lock:
            with open(_VEC_META, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("digest") == digest:
                import numpy as np
                return np.load(_VEC_PATH)
    except Exception:
        pass
    # 缓存过期：别在回复路径上编码全部情节（几百条会卡一秒以上），丢后台
    if not _building["on"]:
        with _build_lock:
            if not _building["on"]:
                _building["on"] = True
                threading.Thread(target=_build_vectors, args=(list(eps), digest),
                                 daemon=True).start()
    return None


def _build_vectors(eps: list, digest: str):
    try:
        m = dense_retrieval.embed([e["text"] for e in eps])
        if m is None:
            return
        import numpy as np
        with _io_lock:
            np.save(_VEC_PATH, m)
            with open(_VEC_META, "w", encoding="utf-8") as f:
                json.dump({"digest": digest, "count": len(eps)}, f)
    except Exception as e:
        print(f"[记忆] 向量索引构建失败（继续用词法）: {str(e)[:60]}")
    finally:
        _building["on"] = False


def recall(query: str, k: int = 4, mood: float = 0, stats: dict = None) -> list:
    """按当前这句话召回相关情节；命中会累加 hits（影响后续遗忘速度）。

    mood 是当前心情分数（-10~10）：情绪一致的旧事更容易被想起，
    反向情绪的记忆会被压一点。0 表示中性，不做加权。
    """
    with _io_lock:  # 读-改-写要串行，否则并发召回会互相覆盖 hits
        all_eps = load_episodes()
        eps = active_episodes(all_eps)
        if not eps:
            return []
        lex = _lexical_scores(query, eps)
        vec_scores = None
        m = _vector_matrix(eps)
        qv = dense_retrieval.embed([query]) if (m is not None and dense_retrieval) else None
        if m is not None and qv is not None:
            vec_scores = (m @ qv[0]).tolist()
        if vec_scores:
            mixed = [0.6 * v + 0.4 * l for v, l in zip(vec_scores, lex)]
        else:
            mixed = lex
        if mood:
            boosts = [_mood_boost(ep, mood) for ep in eps]
            mixed = [s * b for s, b in zip(mixed, boosts)]
        else:
            boosts = [1.0] * len(eps)
        order = sorted(range(len(eps)), key=lambda i: mixed[i], reverse=True)[:k]
        out = []
        for i in order:
            if mixed[i] <= 0:
                continue
            ep = dict(eps[i])
            ep["_score"] = round(mixed[i], 3)
            out.append(ep)
            target = eps[i]
            target["hits"] = int(target.get("hits") or 0) + 1
            target["last_hit"] = round(time.time(), 3)
        if out:
            _save_episodes(all_eps)
        if isinstance(stats, dict):
            stats["count"] = len(out)
            stats["boosted"] = sum(1 for i in order[:len(out)] if boosts[i] > 1.0)
            stats["suppressed"] = sum(1 for i in order[:len(out)] if boosts[i] < 1.0)
        return out


def forget_scan(min_score: float = 0.6) -> int:
    """把强度低于阈值的记忆移进归档（不删除，可手动恢复）。"""
    with _io_lock:
        eps = load_episodes()
        now = time.time()
        keep, drop = [], []
        for ep in eps:
            (keep if score(ep, now) >= min_score else drop).append(ep)
        if not drop:
            return 0
        _ensure_dir()
        with open(_ARCHIVE, "a", encoding="utf-8") as f:
            for ep in drop:
                f.write(json.dumps(ep, ensure_ascii=False) + "\n")
        _save_episodes(keep)
        return len(drop)


# ── 抽取提示词（模型调用交给调用方） ──
_EXTRACT_PROMPT = """从下面这段对话里提取"值得长期记住"的内容，只提取用户亲口说过的事。

规则：
- 只记用户说过的：他/她自述的经历、约定、喜好、明确的情绪转折。
- 不要写任何"达妮娅记得/她想到了"这类第三人称叙述，直接写事实本身。
- 不要推测，不要补全，不要编造。原文里没有的绝不添加。
- 琐碎寒暄（你好/在吗/晚安）不要记。
- 每条一行，格式：类型|重要度|内容
  类型只能是 event（发生过的事）、promise（约定）、preference（喜好）、mood（当下状态）
  重要度 1-5，越影响关系越重要
- 没有值得记的就输出空。

对话：
%s

只输出这些行，不要解释。"""


def extraction_prompt(messages: list) -> str:
    lines = []
    for m in messages or []:
        role = "用户" if m.get("role") == "user" else "对方"
        lines.append(f"{role}：{str(m.get('content') or '')[:600]}")
    return _EXTRACT_PROMPT % "\n".join(lines[-24:])


_EXTRACT_LINE = re.compile(r"^(event|promise|preference|mood)\s*\|\s*(\d)\s*\|\s*(.+)$")


def parse_extraction(text: str) -> list:
    """解析抽取结果，返回可直接喂给 add_episode 的 dict 列表。"""
    out = []
    for line in (text or "").splitlines():
        m = _EXTRACT_LINE.match(line.strip())
        if not m:
            continue
        kind, imp, body = m.group(1), int(m.group(2)), m.group(3).strip()
        if body:
            out.append({"kind": kind, "importance": imp, "text": body})
    return out


# ── 自思考（空闲时冒出来的想法，给主动搭话当话头用） ──
# 和情节分开存：想法是第一人称的（"我在想他今天加班到那么晚"），
# 混进情节里会被 recall 当成"用户说过的事"注入，反而变成编造经历。
_THOUGHTS = os.path.join(_DIR, "thoughts.jsonl")
_THOUGHT_MAX = 40


def _load_thoughts() -> list:
    if not os.path.exists(_THOUGHTS):
        return []
    out = []
    try:
        with _io_lock:
            with open(_THOUGHTS, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
    except Exception:
        pass
    return out


def _save_thoughts(items: list):
    _ensure_dir()
    tmp = _THOUGHTS + ".tmp"
    with _io_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            for t in items[-_THOUGHT_MAX:]:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        os.replace(tmp, _THOUGHTS)


def add_thought(text: str, mood: str = "") -> dict:
    """记下一条空闲时的想法。"""
    text = (text or "").strip()
    if not text:
        return {}
    ts = time.time()
    item = {"ts": round(ts, 3), "text": text[:80], "mood": mood, "used": False}
    # load+改+save 必须同一个临界区：锁外写盘会覆盖掉并发 take 的 used 标记
    with _io_lock:
        items = _load_thoughts()
        items.append(item)
        _save_thoughts(items)
    return item


def take_thought(max_age_secs: float = 6 * 3600) -> str:
    """取一条还没用过、且不太旧的想法（取完标记已用，避免反复当开场白）。"""
    now = time.time()
    # 选中的想法和 used 标记要在同一个锁里落盘，否则并发 add 会把它覆盖回未用
    with _io_lock:
        items = _load_thoughts()
        for it in reversed(items):
            if it.get("used"):
                continue
            if now - float(it.get("ts") or 0) > max_age_secs:
                return ""
            it["used"] = True
            _save_thoughts(items)
            return it.get("text", "")
    return ""


_THOUGHT_PROMPT = """你是达妮娅。下面是你们刚刚聊完的一段对话，现在没人说话了，房间很安静。
写一句你此刻心里冒出来的想法。

规则：
- 第一人称"我"，一句，不超过 30 字。
- 具体：想他刚说的事、想他现在的样子、想接下来要做的事都行。
- 不要问候，不要提议，不要复述对话，不要加引号或任何前缀。
- 不出现"用户""对方""漂泊者"这类词。
- 真的没什么可想的就输出空。

对话：
%s"""


def thought_prompt(messages: list) -> str:
    lines = []
    for m in messages or []:
        role = "用户" if m.get("role") == "user" else "我"
        lines.append(f"{role}：{str(m.get('content') or '')[:400]}")
    return _THOUGHT_PROMPT % "\n".join(lines[-12:])


_THOUGHT_STRIP = re.compile(r"^(?:想法|自思考|思考|达妮娅)\s*[:：]\s*")


def parse_thought(text: str) -> str:
    """把模型输出收拾成一句干净的想法（多行取第一句，超长截断）。"""
    for line in (text or "").splitlines():
        line = _THOUGHT_STRIP.sub("", line.strip()).strip('"\'“”「」')
        if line:
            return line[:60]
    return ""


if __name__ == "__main__":
    for t, k, imp in [("用户说他今天加班到十一点，很累", "event", 3),
                      ("用户约好周末一起看那部新番", "promise", 4),
                      ("用户不喜欢吃香菜", "preference", 4)]:
        add_episode(t, k, importance=imp)
    for q in ["还记得我昨天说加班吗", "周末看番", "晚饭吃什么"]:
        hits = recall(q, k=3)
        print(q, "->", [(h["text"][:18], h["_score"]) for h in hits] or "无")
    print("清理:", forget_scan(min_score=0.5), "条")
