# -*- coding: utf-8 -*-
"""
长期记忆整理（二期）：合并 / 冲突消解 / 摘要压缩 / 升级为事实。

设计约束：
- **本地优先**。合并、冲突消解、摘要压缩都不发请求，即使网关 429 也能跑。
- **只改状态，不改内容**。被合并/过时的条目标 `merged_into` / `superseded`，
  原文永远保留，召回时跳过而不是删除。
- **升级为事实**需要模型判断，这里只提供候选、提示词和解析，
  调用方（会话空闲时）再决定要不要发那一次请求。

字段说明（都可选、向后兼容）：
  merged_into  被合并进哪条（存 keeper 的 id）
  superseded   被更新的信息取代（True）
  superseded_by 取代它的那条 id
  promoted     已写进 facts.md（True）
"""
import json
import os
import re
import shutil
import time

import dense_retrieval
import memory_store as ms

_DEDUP_COS = 0.85        # 合并阈值（向量余弦）
_CONFLICT_JAC = 0.45     # 冲突阈值（字二元组重合度）
_SUMMARY_MAX_LINES = 12  # 摘要超过这个行数就压缩
_SUMMARY_KEEP_LINES = 8  # 压缩后保留最近几行


# ── 相似度 ──
def _gram_jac(a: str, b: str) -> float:
    ga, gb = ms._grams(a), ms._grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _sim_matrix(texts: list):
    """优先向量余弦；模型未就绪返回 None，调用方回退字面重合。"""
    if dense_retrieval is None:
        return None
    if not dense_retrieval.ready():
        dense_retrieval.warmup_async()  # 让下一次整理能用上向量
        return None
    m = dense_retrieval.embed(texts)
    if m is None:
        return None
    try:
        import numpy as np
        norm = np.linalg.norm(m, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        m = m / norm
        return (m @ m.T).tolist()
    except Exception:
        return None


def _ep_key(ep: dict) -> str:
    return ep.get("id") or f"{ep.get('ts')}|{ep.get('text')}"


# ── ① 合并近似情节 ──
def dedupe(min_cos: float = _DEDUP_COS) -> int:
    """同类型、同情绪方向、近乎重复的情节合并到一条，hits 累加。"""
    all_eps = ms.load_episodes()
    act = ms.active_episodes(all_eps)
    if len(act) < 2:
        return 0
    sims = _sim_matrix([e.get("text", "") for e in act])
    order = sorted(range(len(act)), key=lambda i: (
        -float(act[i].get("importance") or 3), -float(act[i].get("ts") or 0)))
    keepers = []
    merged = 0
    for i in order:
        ep = act[i]
        for j in keepers:
            a, b = act[i], act[j]
            if (a.get("kind") or "event") != (b.get("kind") or "event"):
                continue
            va, vb = ms.valence(a.get("text", "")), ms.valence(b.get("text", ""))
            if va and vb and va != vb:
                continue  # 只有"明确相反"才留给冲突处理；中性不挡
            if sims is not None:
                sim_ij = sims[i][j]
            else:
                sim_ij = _gram_jac(a.get("text", ""), b.get("text", ""))
            if sim_ij < min_cos:
                continue
            # 合并进 keeper：hits 相加，重要度取大
            b["hits"] = int(b.get("hits") or 0) + int(a.get("hits") or 0) + 1
            b["importance"] = max(int(b.get("importance") or 3), int(a.get("importance") or 3))
            if float(a.get("ts") or 0) > float(b.get("ts") or 0):
                b["ts"] = a.get("ts")
                b["date"] = a.get("date")
            a["merged_into"] = _ep_key(b)
            merged += 1
            break
        else:
            keepers.append(i)
    if merged:
        ms.save_episodes(all_eps)
    return merged


# ── ④ 冲突消解 ──
def resolve_conflicts(min_jac: float = _CONFLICT_JAC) -> int:
    """同一件事的正/反陈述：保留更新的，旧的标 superseded（不删）。"""
    all_eps = ms.load_episodes()
    act = ms.active_episodes(all_eps)
    if len(act) < 2:
        return 0
    changed = 0
    for i in range(len(act)):
        for j in range(i + 1, len(act)):
            a, b = act[i], act[j]
            va, vb = ms.valence(a.get("text", "")), ms.valence(b.get("text", ""))
            if not va or not vb or va == vb:
                continue
            if _gram_jac(a.get("text", ""), b.get("text", "")) < min_jac:
                continue
            older, newer = (a, b) if float(a.get("ts") or 0) < float(b.get("ts") or 0) else (b, a)
            if older.get("superseded"):
                continue
            older["superseded"] = True
            older["superseded_by"] = _ep_key(newer)
            changed += 1
    if changed:
        ms.save_episodes(all_eps)
    return changed


# ── ② 摘要压缩 ──
def rewrite_summaries(max_lines: int = _SUMMARY_MAX_LINES,
                      keep_lines: int = _SUMMARY_KEEP_LINES) -> int:
    """超长摘要：原文整份归档，只留最近几行，避免越滚越像流水账。

    这里只做本地压缩。需要"压成 3 条要点"时用 compress_prompt() 交给模型，
    由调用方在空闲时执行（本函数不发起请求）。
    """
    out_dir = os.path.join(ms._SUMMARY_DIR, "archive")
    n = 0
    for fn in sorted(os.listdir(ms._SUMMARY_DIR)) if os.path.isdir(ms._SUMMARY_DIR) else []:
        if not fn.endswith(".md"):
            continue
        path = os.path.join(ms._SUMMARY_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) <= max_lines:
            continue
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, os.path.join(out_dir, f"{os.path.splitext(fn)[0]}_{stamp}.md"))
        keep = lines[-keep_lines:]
        with open(path, "w", encoding="utf-8") as f:
            f.write("（更早的对话已归档）\n" + "\n".join(keep) + "\n")
        n += 1
    return n


def compress_prompt(lines: list) -> str:
    """需要模型压缩摘要时用（调用方决定时机）。"""
    return ("把下面的对话摘要压成不超过 3 条要点，每条不超过 25 字。"
            "只保留还会影响以后聊天的事实（谁、什么事、什么偏好、什么约定）。"
            "不要评论，不要编造，不要写成句子以外的格式。直接输出三条，每行一条。\n\n"
            + "\n".join(lines))


def parse_compress(text: str) -> list:
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("-·• ").strip()
        if ln and len(ln) <= 60:
            out.append(ln)
        if len(out) >= 3:
            break
    return out


# ── ③ 升级为事实（候选是本地算的，写入由调用方触发） ──
def promotion_candidates(min_cos: float = 0.8, min_count: int = 2) -> list:
    """同类偏好/约定被记了至少两次 → 值得写成一条长期事实。

    向量没就绪时字面重合会偏严，所以回退阈值放宽到 0.6。
    """
    all_eps = ms.load_episodes()
    act = [e for e in ms.active_episodes(all_eps)
           if (e.get("kind") or "") in ("preference", "promise")
           and not e.get("promoted")]
    if len(act) < min_count:
        return []
    sims = _sim_matrix([e.get("text", "") for e in act])
    thr = min_cos if sims is not None else 0.6
    used, cands = set(), []
    for i in range(len(act)):
        if i in used:
            continue
        group = [i]
        for j in range(i + 1, len(act)):
            if j in used or act[i].get("kind") != act[j].get("kind"):
                continue
            # 向量对否定几乎不敏感（"喜欢"和"不喜欢"余弦 0.93），相反方向必须靠词面挡掉
            va, vb = ms.valence(act[i].get("text", "")), ms.valence(act[j].get("text", ""))
            if va and vb and va != vb:
                continue
            sim_ij = sims[i][j] if sims is not None else _gram_jac(
                act[i].get("text", ""), act[j].get("text", ""))
            if sim_ij >= thr:
                group.append(j)
        if len(group) >= min_count:
            used.update(group)
            rep = max(group, key=lambda x: float(act[x].get("ts") or 0))
            cands.append({
                "text": act[rep].get("text", ""),
                "count": len(group),
                "ids": [_ep_key(act[x]) for x in group],
            })
    return cands


def promotion_prompt(cands: list) -> str:
    lines = [f"{i + 1}. {c['text']}" for i, c in enumerate(cands)]
    return ("下面几组是同一件事被反复提到的记录。把每组压成一条长期记住的事实，"
            "只保留会一直有用的信息（喜好、约定、习惯），用“用户…”开头，"
            "每条一行，不要编号以外的格式，不要编造，不要合并成一句总述。\n\n"
            + "\n".join(lines))


def apply_promotions(items: list, source_ids: list = None) -> int:
    """把定稿的事实写进 facts.md，并把来源情节标 promoted。"""
    try:
        from memory_facts import add_fact, read_facts
    except Exception:
        return 0
    written = 0
    for it in items or []:
        text = (it.get("text") if isinstance(it, dict) else str(it)) or ""
        text = text.strip().lstrip("-·• ").strip()
        if not text or text in read_facts(99999):
            continue  # 已经有同一条了，别重复写
        if add_fact(text):
            written += 1
    ids = set(source_ids or [])
    if ids:
        all_eps = ms.load_episodes()
        changed = False
        for ep in all_eps:
            if _ep_key(ep) in ids and not ep.get("promoted"):
                ep["promoted"] = True
                changed = True
        if changed:
            ms.save_episodes(all_eps)
    return written


# ── 统一入口 ──
def run(dedup: bool = True, conflicts: bool = True, summaries: bool = True) -> dict:
    """跑一遍本地整理（不发请求）。返回统计，可直接打日志。"""
    st = {"merged": 0, "superseded": 0, "summaries": 0, "promotions": 0}
    try:
        st["merged"] = dedupe() if dedup else 0
        st["superseded"] = resolve_conflicts() if conflicts else 0
        st["summaries"] = rewrite_summaries() if summaries else 0
        st["promotions"] = len(promotion_candidates())
    except Exception as e:
        print(f"[整理] 失败（忽略）: {str(e)[:80]}")
    if any(st[k] for k in ("merged", "superseded", "summaries")):
        print(f"[整理] 合并 {st['merged']} 条 / 冲突 {st['superseded']} 条 / "
              f"摘要压缩 {st['summaries']} 个 / 待升级 {st['promotions']} 组")
    return st


# ── ③ 升级为事实：显式触发（CLI / 定时任务），不在对话链路里 ──
def _gateway_config() -> dict:
    """读 settings.json 里的网关配置（与 denia_llm 用同一份）"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        key = cfg.get("cloud_api_key") or ""
        url = cfg.get("cloud_base_url") or ""
        if not key or not url:
            return {}
        return {"api_key": key, "base_url": url,
                "model": cfg.get("model_name") or "agnes-3.0-flash"}
    except Exception:
        return {}


def _ask_gateway(prompt: str, timeout: float = 15.0) -> str:
    """单发一次，不重试；限流/超时/任何异常都静默返回空串。

    升级事实不是关键路径，失败就等下次触发，绝不给网关添堵。
    """
    cfg = _gateway_config()
    if not cfg:
        print("[升级] 缺少网关配置，跳过")
        return ""
    import requests
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 300,
        "stream": False,
    }
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}"},
                          json=payload, timeout=timeout)
        if r.status_code == 429:
            print("[升级] 网关限流，本次跳过")
            return ""
        r.raise_for_status()
        data = r.json()
        return ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
    except Exception as e:
        print(f"[升级] 请求失败，本次跳过: {str(e)[:60]}")
        return ""


def parse_promotion(text: str) -> list:
    """解析模型输出：每行一条“用户…”的长期事实，最多 6 条。"""
    items = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"^\s*((?:[-·•]|\d+[.)、])\s*)+", "", ln).strip()
        if not ln or len(ln) > 60:
            continue
        if not ln.startswith("用户"):
            continue  # 只要“用户…”开头的陈述，其余是解释/拒绝
        items.append({"text": ln})
    return items[:6]


def promote(dry_run: bool = False, timeout: float = 15.0) -> dict:
    """把重复出现的偏好/约定压成长期事实写进 facts.md。

    dry_run=True 只列候选，不发请求也不写盘。
    """
    cands = promotion_candidates()
    st = {"candidates": len(cands), "written": 0, "skipped": ""}
    if not cands:
        return st
    if dry_run:
        for c in cands:
            print(f"[候选] ×{c['count']} {c['text']}")
        return st
    text = _ask_gateway(promotion_prompt(cands), timeout=timeout)
    if not text:
        st["skipped"] = "网关未返回"
        return st
    items = parse_promotion(text)
    if not items:
        st["skipped"] = "模型未给出可用事实"
        return st
    ids = [i for c in cands for i in c["ids"]]
    st["written"] = apply_promotions(items, ids)
    for it in items:
        print(f"[升级] {it['text']}")
    return st


if __name__ == "__main__":
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="长期记忆整理（本地优先）")
    ap.add_argument("--promote", action="store_true",
                    help="把重复的偏好/约定升级成长期事实（发一次请求）")
    ap.add_argument("--dry-run", action="store_true", help="只列候选，不发请求不写盘")
    ap.add_argument("--local-only", action="store_true", help="只跑本地整理")
    ap.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数")
    a = ap.parse_args()

    st = run()
    if a.local_only:
        print(json.dumps(st, ensure_ascii=False))
    elif a.dry_run:
        print(json.dumps(promote(dry_run=True), ensure_ascii=False))
    elif a.promote:
        print(json.dumps(promote(timeout=a.timeout), ensure_ascii=False))
    else:
        # 默认：本地整理 + 列候选（不发请求）
        print(json.dumps(promote(dry_run=True), ensure_ascii=False))
