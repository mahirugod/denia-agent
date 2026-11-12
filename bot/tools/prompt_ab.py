"""语气 A/B 评测：同一批真实对话组，分别用 base / new 两种 prompt 拼装顺序跑一遍。

用法：
    python bot/tools/prompt_ab.py --dry-run            # 只准备输入并检查分组，不调用模型
    python bot/tools/prompt_ab.py --limit 10 --turns 3 # 两套 prompt 各跑 10 组、每组 3 轮

产物：
    bot/data/eval_inputs.json
    bot/data/eval/<variant>.jsonl
    bot/data/eval/report.md        对照表 + 自动指标

注意：
- 每组 3-5 轮：单轮测不出"情绪是否延续""摘要是否被正确注入"，也测不出跨轮指代。
- 每次跑批约消耗 2×（组数×轮数）次云端请求，先用 --limit 2 试跑确认配置可用。
- 心情文件会被备份/还原，避免先跑的变体把心情带偏后跑的变体。
- 语气档位每轮随机，属于评测噪声；样本量太小不要下结论。
"""
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(BOT_DIR)
sys.path.insert(0, BOT_DIR)
DATA_DIR = os.path.join(BOT_DIR, "data")
EVAL_DIR = os.path.join(DATA_DIR, "eval")
INPUTS_PATH = os.path.join(DATA_DIR, "eval_inputs.json")
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_runner.py")
MOOD_PATH = os.path.join(DATA_DIR, "mood.json")

_TONE_PARTICLE_RE = re.compile(r"[嘛呢哦吧呀啦耶]")
_PAUSE_RE = re.compile(r"(……|\.\.\.|。。)")

# 测跨轮指代：这些词单独出现时，答案必须回到上一轮的对象上
_PRONOUN_RE = re.compile(r"(?:它|这个|那个|这件事|那件事|上面说的|刚才那个)")
# 高频虚词组成的二元组不参与重合判断，否则任何两句话都会"重合"
_STOP_GRAMS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "现在", "就是", "还是",
    "可以", "一下", "哈哈", "不是", "没有", "真的", "知道", "觉得", "因为", "所以",
    "但是", "然后", "这样", "那样", "一个", "有点", "好像", "已经", "时候",
    "今天", "明天", "昨天", "早上", "晚上", "刚才", "一点", "起来", "出来",
}


def collect_dialogues(limit, turns):
    """从真实对话历史里取用户消息，按时间顺序每 turns 条组成一段会话。

    单句组丢掉：不足两轮就测不出跨轮指代和上下文延续。
    """
    path = os.path.join(DATA_DIR, "chat_history.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    history = raw.get("history", []) if isinstance(raw, dict) else raw
    seen, picked = set(), []
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        text = (msg.get("content") or "").strip()
        if not (6 <= len(text) <= 60):
            continue
        if text in seen:
            continue
        seen.add(text)
        picked.append(text)
    picked.reverse()
    groups = []
    for i in range(0, len(picked), max(2, turns)):
        chunk = picked[i:i + max(2, turns)]
        if len(chunk) < 2:
            continue
        groups.append({"id": f"d{len(groups) + 1:03d}", "turns": chunk})
        if len(groups) >= limit:
            break
    return groups


def backup_mood():
    if os.path.exists(MOOD_PATH):
        os.makedirs(EVAL_DIR, exist_ok=True)
        shutil.copy2(MOOD_PATH, os.path.join(EVAL_DIR, "mood_backup.json"))


def restore_mood():
    backup = os.path.join(EVAL_DIR, "mood_backup.json")
    if os.path.exists(backup):
        shutil.copy2(backup, MOOD_PATH)


def run_variant(variant, items):
    env = os.environ.copy()
    env["DENIA_PROMPT_VARIANT"] = variant
    payload = "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items)
    total_turns = sum(len(it.get("turns") or [it.get("text", "")]) for it in items)
    proc = subprocess.run(
        [sys.executable, RUNNER],
        input=payload, capture_output=True, text=True, encoding="utf-8", env=env,
        # 每轮按 90s 上限估算，外加 2 分钟启动余量；超时直接报错而不是永久挂起
        timeout=max(180, 90 * total_turns + 120),
    )
    rows = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows and proc.returncode != 0:
        raise RuntimeError(f"{variant} 运行失败：{(proc.stderr or '')[-800:]}")
    return rows


def _pairs(row):
    """把一行结果规整成 (轮次, 回复) 两个列表，兼容旧的单轮格式。"""
    turns = row.get("turns")
    if not isinstance(turns, list) or not turns:
        turns = [row.get("text", "")] if row.get("text") else []
    replies = row.get("replies")
    if not isinstance(replies, list) or not replies:
        replies = [row["reply"]] if row.get("reply") is not None else []
    return [str(t or "") for t in turns], [str(r or "") for r in replies]


def _bigrams(text):
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", text or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _cross_turn_stats(rows):
    """跨轮指代正确率：用户只说"它呢/那个呢"时，回答是否还落在上一轮的对象上。

    判定用二元组重合做代理：回答与上一轮（用户句+回复）有实词重合即算指对。
    """
    hit = total = 0
    for row in rows:
        turns, replies = _pairs(row)
        for i in range(1, min(len(turns), len(replies))):
            u = turns[i]
            core = _bigrams(_PRONOUN_RE.sub("", u)) - _STOP_GRAMS
            if not _PRONOUN_RE.search(u) or len(core) > 2:
                continue
            total += 1
            prev = turns[i - 1] + (replies[i - 1] or "")
            if _bigrams(replies[i]) & (_bigrams(prev) - _STOP_GRAMS):
                hit += 1
    return hit, total


def _on_topic_stats(rows):
    """切题率：回复是否与当轮用户句有实词重合（太短/太泛的句子不判）。"""
    hit = total = 0
    for row in rows:
        turns, replies = _pairs(row)
        for u, r in zip(turns, replies):
            grams = _bigrams(u) - _STOP_GRAMS
            if len(grams) < 3:
                continue
            total += 1
            if _bigrams(r) & grams:
                hit += 1
    return hit, total


def _ratio(hit, total):
    return f"{hit}/{total} ({round(hit / total * 100)}%)" if total else "-"


def score(rows):
    from denia_llm import DeniaLLM
    # 超时/报错兜底不是模型真实输出，单独计数后剔除，别拿来算文风
    errors = sum(1 for r in rows if r.get("error"))
    rows = [r for r in rows if not r.get("error")]
    replies = [rep for r in rows for rep in _pairs(r)[1] if rep]
    if not replies:
        return {"组数": len(rows), "错误兜底": errors}
    total_chars = sum(len(r) for r in replies)
    _an_hit, _an_total = _cross_turn_stats(rows)
    _ot_hit, _ot_total = _on_topic_stats(rows)
    return {
        "组数": len(rows),
        "错误兜底": errors,
        "平均字数": round(total_chars / len(replies), 1),
        "助手腔命中": sum(1 for r in replies if DeniaLLM._AI_FLAVOR_RE.search(r)),
        "语气词/百字": round(sum(len(_TONE_PARTICLE_RE.findall(r)) for r in replies) / total_chars * 100, 1),
        "停顿/百字": round(sum(len(_PAUSE_RE.findall(r)) for r in replies) / total_chars * 100, 1),
        "切题率": _ratio(_ot_hit, _ot_total),
        "跨轮指代正确率": _ratio(_an_hit, _an_total),
        "中位耗时ms": int(statistics.median([r.get("ms", 0) for r in rows])),
    }


def write_report(results):
    os.makedirs(EVAL_DIR, exist_ok=True)
    path = os.path.join(EVAL_DIR, "report.md")
    metrics = {v: score(rows) for v, rows in results.items()}
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 语气 A/B 报告（多轮）\n\n")
        f.write("| 指标 | " + " | ".join(results) + " |\n")
        f.write("|---" * (len(results) + 1) + "|\n")
        keys = list(next(iter(metrics.values())).keys()) if metrics else []
        for k in keys:
            f.write(f"| {k} | " + " | ".join(str(metrics[v].get(k, "-")) for v in results) + " |\n")
        f.write("\n## 逐组对照（⏎ 分隔组内各轮）\n\n")
        f.write("| 对话 | " + " | ".join(results) + " |\n")
        f.write("|---" * (len(results) + 1) + "|\n")
        by_id = {v: {r["id"]: r for r in rows} for v, rows in results.items()}
        first = list(results)[0]
        for rid in sorted(by_id[first]):
            turns, _ = _pairs(by_id[first][rid])
            dialogue = " / ".join(t.replace("\n", " ") for t in turns)[:90]
            cells = [" ⏎ ".join(_pairs(by_id[v].get(rid, {}))[1]).replace("\n", " ")
                     for v in results]
            f.write(f"| {dialogue} | " + " | ".join(cells) + " |\n")
    return path, metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="对话组数")
    ap.add_argument("--turns", type=int, default=3, help="每组轮数（建议 3-5）")
    ap.add_argument("--variants", default="base,new")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = collect_dialogues(args.limit, args.turns)
    with open(INPUTS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"[输入] {len(items)} 组 → {INPUTS_PATH}")
    for it in items:
        print(f"  {it['id']} " + " / ".join(it["turns"]))
    if args.dry_run:
        print("[dry-run] 未调用模型")
        return

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    backup_mood()
    results = {}
    try:
        for v in variants:
            print(f"[跑批] 变体 {v} ...")
            restore_mood()
            results[v] = run_variant(v, items)
            print(f"  → {len(results[v])} 组")
            out_path = os.path.join(EVAL_DIR, f"{v}.jsonl")
            os.makedirs(EVAL_DIR, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                for row in results[v]:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        # 正常结束、异常、Ctrl+C 都要把心情还原回去
        restore_mood()
    path, metrics = write_report(results)
    print(f"[报告] {path}")
    for v, m in metrics.items():
        print(f"  {v}: {m}")


if __name__ == "__main__":
    main()
