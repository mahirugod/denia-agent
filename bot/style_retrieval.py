"""台词语料的词法检索：按当前这句话挑最相关的几组，替代固定随机抽取。

为什么不用向量模型：语料只有几十组短对白，字 1-2 gram + TF-IDF 已经够
（"心烦"能召回含"烦"的句子，专名靠关键词而非语义）。不引入 jieba/模型，
零下载零依赖。接口保持稳定（build_index / retrieve），将来若换成向量，
只改 _score 内部，调用方不用动。
"""

import math
import os
import random
import re

# 情绪/状态词：命中的组额外加权。这类词在短句里信息量最大，
# 纯 TF-IDF 容易被"今天/什么/自己"这类高频词稀释。
_EMO = ("累", "困", "烦", "开心", "高兴", "难过", "无聊", "饿", "冷", "热",
        "怕", "生气", "委屈", "睡不着", "加班", "上班", "下班", "放学",
        "考试", "下雨", "生病", "疼", "想", "走", "烦人", "好累")

_CLEAN_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")

# 同义归并：用户很少直接说"累"，更多说"提不起劲/没精神/有点丧"。词法检索
# 抓不到这些改写（实测 6 条里 5 条召回完全无关），所以在查询侧把它们映射到
# 语料里真实出现过的锚点词，让 TF-IDF 能对上。锚点必须存在于语料中，
# 否则 idf 为 0，加了等于没加。
_SYN_GROUPS = (
    (("提不起劲", "没精神", "没力气", "心累", "疲惫", "丧", "不想动", "好累"), ("累",)),
    (("犯困", "好困", "困了", "想睡"), ("睡", "哈欠")),
    (("烦躁", "郁闷", "心里堵", "火大", "烦死", "烦人"), ("烦", "生气")),
    (("难受", "想哭", "低落", "伤心", "难过", "emo"), ("难过", "心情")),
    (("高兴", "快乐", "愉快", "心情好", "开心"), ("开心", "笑")),
    (("没事干", "好闲", "没意思", "无聊"), ("闲",)),
    (("害怕", "吓人", "恐怖", "不敢"), ("怕",)),
    (("失眠", "睡不着", "熬夜"), ("睡",)),
    (("不想上班", "不想学习", "不想努力"), ("不想", "努力")),
)


def expand_query(q: str) -> str:
    """把同义说法映射到语料锚点，每个锚点重复两次提高权重。"""
    extra = []
    for phrases, anchors in _SYN_GROUPS:
        if any(p in q for p in phrases):
            extra.extend(list(anchors) * 2)
    return q + ("" if not extra else "".join(extra))

# 分数到这个量级，说明这一组几乎就是"用户这句话 + 她的原话回答"。直接给出去
# 会被整句照抄（实测"今天好累啊"→原样输出"那就别动了。"加一整段原文），
# 所以这类组不进样本：问法一样还要抄，等于没学会说话。
_COPY_GUARD = 0.5


def _tokens(text: str) -> list:
    """切字 1-2 gram（中文短句上比词粒度更稳，不需要分词器）。"""
    s = _CLEAN_RE.sub("", text or "")
    if not s:
        return []
    toks = list(s)
    toks.extend(s[i:i + 2] for i in range(len(s) - 1))
    return toks


def _load_groups(path: str) -> list:
    """按空行切组：一组 = 一轮完整对白，切开会丢掉"别人怎么问"。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return []
    groups = []
    for chunk in re.split(r"\n\s*\n", raw):
        lines = [ln.rstrip() for ln in chunk.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        if lines:
            groups.append(_sanitize("\n".join(lines)))
    return groups


# 参考语录里的称呼统一换掉：模型会照着样本叫人，实测出现过"漂泊者同学"。
# 她的自称"达妮娅"保留（台词里有"叫我达妮娅"这种句子，删了会读不通）。
_SPEAKER_RE = re.compile(r"^(\S{2,6})\s*[：:]")
_NAMES = re.compile(
    "漂泊者同学|漂泊者|西格莉卡|娜波摩|艾莉亚|琳奈|娜斯塔霞|阿里曼|绯雪|阿布|西西")


def _sanitize(group: str) -> str:
    """把对白里的其他人的称呼改成"你/她"；她自己的名字保留不换。"""
    out = []
    for ln in group.splitlines():
        m = _SPEAKER_RE.match(ln)
        if m and m.group(1) not in ("达妮娅",):
            ln = "对方：" + _strip_names(ln[m.end():])
        else:
            ln = _strip_names(ln)
        out.append(ln)
    t = "\n".join(out)
    t = re.sub(r"她她", "她", t)
    t = re.sub(r"^(对方：)你，", r"\1", t, flags=re.M)  # "你，她找你了吗" → "她找你了吗"
    t = re.sub(r"[，、]\s*([。！？])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return "\n".join(ln.rstrip() for ln in t.splitlines())


def _strip_names(text: str) -> str:
    text = text.replace("漂泊者同学", "你").replace("漂泊者", "你")
    return _NAMES.sub("她", text)


def build_index(path: str, dense: bool = False) -> dict:
    """构建倒排索引（进程内一次，几十组规模几毫秒）。

    dense=True 时同时挂上向量矩阵（idx["emb"]）。后端没就绪就拿不到，
    调用方照常用词法，不会因为等模型而卡住。
    """
    groups = _load_groups(path)
    if not groups:
        return {"groups": [], "idf": {}, "vecs": []}
    df = {}
    for g in groups:
        for t in set(_tokens(g)):
            df[t] = df.get(t, 0) + 1
    n = len(groups)
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    vecs = [_vector(_tokens(g), idf) for g in groups]
    idx = {"groups": groups, "idf": idf, "vecs": vecs}
    if dense:
        try:
            from dense_retrieval import corpus_embedding
            emb, name = corpus_embedding(path, groups)
            if emb is not None:
                idx["emb"] = emb
                idx["backend"] = name
        except Exception as e:
            print(f"[向量] 索引挂载失败（用词法）: {e}")
    return idx


def _vector(tokens: list, idf: dict) -> dict:
    tf = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    vec = {t: (1 + math.log(c)) * idf.get(t, 0.0) for t, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def retrieve(query: str, idx: dict, cap: int = 820, k: int = 10,
             stats: dict = None) -> str:
    """返回最相关的若干组文本（凑到约 cap 字）；全不相关时退回随机抽。

    stats 传入字典时回填诊断信息：top（最高分）、fallback（是否退回随机），
    供上层统计"检索到底有没有命中"。
    """
    groups = idx.get("groups") or []
    if not groups:
        return ""
    q = (query or "").strip()
    if not q:
        if stats is not None:
            stats["fallback"] = True
        return _fill(random.sample(groups, len(groups)), cap)

    qvec = _vector(_tokens(expand_query(q)), idx["idf"])
    lex = []
    for i, g in enumerate(groups):
        gv = idx["vecs"][i]
        s = sum(w * gv.get(t, 0.0) for t, w in qvec.items())
        # 情绪词命中：同一条里出现就加一点，弥补短句信息稀疏
        s += 0.25 * sum(1 for w in _EMO if w in q and w in g)
        lex.append(s)

    # 向量通道：后端就绪时才用，0.4 词法 + 0.6 语义（词法管专名和精确词，
    # 语义管"提不起劲"这类改写）。两路各做 min-max，避免量纲相差过大。
    dense = None
    emb = idx.get("emb")
    if emb is not None:
        try:
            from dense_retrieval import embed as _embed
            _dq = _embed([q])
            if _dq is not None:
                dense = emb.dot(_dq[0])
        except Exception:
            dense = None

    if dense is not None:
        # 直接用原始分数融合（不做 min-max：归一化会把 top 永远拉到 1，
        # 兜底阈值就失去意义，也看不出"这轮到底有多像"）
        fused = [0.6 * float(d) + 0.4 * lex[i] for i, d in enumerate(dense)]
        order = sorted(range(len(groups)), key=lambda i: fused[i], reverse=True)
        if stats is not None:
            stats["top"] = round(fused[order[0]], 2)
            stats["top_dense"] = round(float(max(dense)), 2)
            stats["dense"] = True
        # 余量小说明"没有特别像的组"（37 组时余弦都很接近），仍用当前最优，
        # 但记 weak 让上层统计"检索到底有多不确定"
        _margin = float(max(dense)) - float(sum(dense) / len(dense))
        if _margin < 0.10 and stats is not None:
            stats["weak"] = True
        # 近重复只挡"长组"：短句（"在吗/在。"）照抄也无妨，长段落才会变成复读
        hits = [i for i in order[:k]
                if not (lex[i] >= _COPY_GUARD and len(groups[i]) > 60)]
        if not hits:
            if stats is not None:
                stats["fallback"] = True
            return _fill(random.sample(groups, len(groups)), cap)
        _rest = [i for i in order if i not in hits]
        random.shuffle(_rest)
        return _fill([groups[i] for i in hits] + [groups[i] for i in _rest], cap)

    scored = [(lex[i], i) for i in range(len(groups))]
    scored.sort(reverse=True)
    if stats is not None:
        stats["top"] = round(scored[0][0], 2)
        stats["dense"] = False
    # 差太远（纯寒暄/和语料无关）就别硬凑相关组，整体随机，保持长尾覆盖
    if scored[0][0] < 0.12:
        if stats is not None:
            stats["fallback"] = True
        return _fill(random.sample(groups, len(groups)), cap)
    hits = [i for s, i in scored[:k]
            if s > 0 and not (s >= _COPY_GUARD and len(groups[i]) > 60)]
    if not hits:
        # 全被"照着问"的近重复挡掉了 → 随机，别硬凑
        if stats is not None:
            stats["fallback"] = True
        return _fill(random.sample(groups, len(groups)), cap)
    # 相关的排前面，剩余额度用随机组补满：既贴题，又保留语料的覆盖面
    _rest = [i for _, i in scored if i not in hits]
    random.shuffle(_rest)
    return _fill([groups[i] for i in hits] + [groups[i] for i in _rest], cap)


def _fill(parts: list, cap: int) -> str:
    """按顺序装入，装不下的跳过（而不是直接截断在半句上）。"""
    out, total = [], 0
    for g in parts:
        if total + len(g) + 2 > cap:
            continue
        out.append(g)
        total += len(g) + 2
    if not out and parts:
        return parts[0][:cap]
    return "\n\n".join(out).strip()
