"""语气 A/B 评测：同一批真实输入，分别用 base / new 两种 prompt 拼装顺序跑一遍。

用法：
    python bot/tools/prompt_ab.py --dry-run        # 只准备输入并检查变体，不调用模型
    python bot/tools/prompt_ab.py --limit 40       # 两套 prompt 各跑 40 条

产物：
    bot/data/eval_inputs.json
    bot/data/eval/<variant>.jsonl
    bot/data/eval/report.md        对照表 + 自动指标

注意：
- 每次跑批约消耗 2×N 次云端请求，先用 --limit 5 试跑确认配置可用。
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


def collect_inputs(limit):
    """从真实对话历史里取用户消息，过滤掉过短、过长和重复的。"""
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
        if len(picked) >= limit:
            break
    picked.reverse()
    return [{"id": f"u{i:03d}", "text": t} for i, t in enumerate(picked, 1)]


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
    proc = subprocess.run(
        [sys.executable, RUNNER],
        input=payload, capture_output=True, text=True, encoding="utf-8", env=env,
        # 每条按 90s 上限估算，外加 2 分钟启动余量；超时直接报错而不是永久挂起
        timeout=max(180, 90 * len(items)),
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


def score(rows):
    from denia_llm import DeniaLLM
    # 超时/报错兜底不是模型真实输出，单独计数后剔除，别拿来算文风
    errors = sum(1 for r in rows if r.get("error"))
    rows = [r for r in rows if not r.get("error")]
    replies = [r["reply"] for r in rows if r.get("reply")]
    if not replies:
        return {"错误兜底": errors}
    total_chars = sum(len(r) for r in replies)
    return {
        "条数": len(replies),
        "错误兜底": errors,
        "平均字数": round(total_chars / len(replies), 1),
        "助手腔命中": sum(1 for r in replies if DeniaLLM._AI_FLAVOR_RE.search(r)),
        "语气词/百字": round(sum(len(_TONE_PARTICLE_RE.findall(r)) for r in replies) / total_chars * 100, 1),
        "停顿/百字": round(sum(len(_PAUSE_RE.findall(r)) for r in replies) / total_chars * 100, 1),
        "中位耗时ms": int(statistics.median([r.get("ms", 0) for r in rows])),
    }


def write_report(results):
    os.makedirs(EVAL_DIR, exist_ok=True)
    path = os.path.join(EVAL_DIR, "report.md")
    metrics = {v: score(rows) for v, rows in results.items()}
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 语气 A/B 报告\n\n")
        f.write("| 指标 | " + " | ".join(results) + " |\n")
        f.write("|---" * (len(results) + 1) + "|\n")
        keys = list(next(iter(metrics.values())).keys()) if metrics else []
        for k in keys:
            f.write(f"| {k} | " + " | ".join(str(metrics[v].get(k, "-")) for v in results) + " |\n")
        f.write("\n## 逐条对照\n\n")
        f.write("| 用户 | " + " | ".join(results) + " |\n")
        f.write("|---" * (len(results) + 1) + "|\n")
        by_id = {v: {r["id"]: r for r in rows} for v, rows in results.items()}
        for rid in sorted(by_id[list(results)[0]]):
            text = by_id[list(results)[0]][rid]["text"].replace("\n", " ")
            cells = [by_id[v].get(rid, {}).get("reply", "").replace("\n", " ") for v in results]
            f.write(f"| {text} | " + " | ".join(cells) + " |\n")
    return path, metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--variants", default="base,new")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = collect_inputs(args.limit)
    with open(INPUTS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"[输入] {len(items)} 条 → {INPUTS_PATH}")
    for it in items:
        print(f"  {it['id']} {it['text']}")
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
            print(f"  → {len(results[v])} 条")
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
