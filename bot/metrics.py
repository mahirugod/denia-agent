"""每轮对话指标：一行一轮落在 data/metrics.jsonl，用 summarize() 看分布。

为什么要这个：网关不稳定（seed 被吞、temperature=0 也不复现），单条回复
没法比较；只能看统计量。改提示词/改示范前后各跑一批，比"长度分布、空回
复率、工具成功率、耗时"而不是凭感觉。

口径（一行一条）：
    ts            时间
    kind          cloud / local
    model         模型名
    harness       档位名（tiny/small/std/cloud）
    input_chars   用户消息长度
    reply_chars   回复长度（0 = 空回复）
    latency_ms    整轮耗时
    steps         工具循环走了几步
    tools         本轮调用的工具名（去重）
    tool_calls    工具调用次数
    tool_fails    其中失败次数
    counters      进程内计数（http_429 / empty_reply 之类，按轮 drain）
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "data" / "metrics.jsonl"
_LOCK = threading.Lock()
_COUNTERS = {}
_STATS_LOCK = threading.Lock()
_TOOL_STATS = {}


# ── 进程内计数器（本轮内发生多少次 429、多少次空回复）──
def bump(kind: str, n: int = 1):
    with _LOCK:
        _COUNTERS[kind] = _COUNTERS.get(kind, 0) + n


def drain() -> dict:
    """取走并清零本轮计数"""
    with _LOCK:
        out = dict(_COUNTERS)
        _COUNTERS.clear()
    return out


# ── 工具侧统计（由 tool_registry 打点）──
def note_tool(name: str, failed: bool = False):
    with _STATS_LOCK:
        rec = _TOOL_STATS.setdefault(name, {"calls": 0, "fails": 0})
        rec["fails" if failed else "calls"] += 1


def drain_tool_stats() -> dict:
    with _STATS_LOCK:
        out = {k: dict(v) for k, v in _TOOL_STATS.items()}
        _TOOL_STATS.clear()
    return out


# ── 落盘 ──
def log_turn(**fields) -> dict:
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    rec.update(fields)
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with open(_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[metrics] 写入失败: {e}")
    return rec


def _tail(limit: int) -> list:
    if not _PATH.exists():
        return []
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def summarize(limit: int = 200) -> str:
    recs = _tail(limit)
    if not recs:
        return f"还没有数据（{_PATH}）"
    n = len(recs)
    lens = [r.get("reply_chars", 0) for r in recs]
    lats = [r.get("latency_ms", 0) for r in recs]
    empty = sum(1 for v in lens if not v)
    tools = sum(r.get("tool_calls", 0) for r in recs)
    fails = sum(r.get("tool_fails", 0) for r in recs)
    c429 = sum((r.get("counters") or {}).get("http_429", 0) for r in recs)
    lore = sum((r.get("counters") or {}).get("lore_leak", 0) for r in recs)
    copy = sum((r.get("counters") or {}).get("lore_copy", 0) for r in recs)
    copy_fix = sum((r.get("counters") or {}).get("lore_copy_fixed", 0) for r in recs)
    fb = sum((r.get("counters") or {}).get("style_fallback", 0) for r in recs)
    weak = sum((r.get("counters") or {}).get("style_weak", 0) for r in recs)
    mid_err = sum((r.get("counters") or {}).get("stream_mid_err", 0) for r in recs)
    s_retry = sum((r.get("counters") or {}).get("stream_retry", 0) for r in recs)
    sorted_lens = sorted(lens)
    p50 = sorted_lens[n // 2] if n else 0
    p90 = sorted_lens[min(n - 1, int(n * 0.9))] if n else 0
    used = {}
    for r in recs:
        for t in r.get("tools") or []:
            used[t] = used.get(t, 0) + 1
    top = "、".join(f"{k}({v})" for k, v in
                    sorted(used.items(), key=lambda kv: -kv[1])[:5]) or "无"
    avg_lat = int(sum(lats) / n) if n else 0
    return "\n".join([
        f"最近 {n} 轮  {recs[0].get('ts')} → {recs[-1].get('ts')}",
        f"回复长度  平均 {int(sum(lens) / n)}  中位 {p50}  p90 {p90}",
        f"空回复    {empty} 条（{empty * 100 // n}%）",
        f"整轮耗时  平均 {avg_lat} ms",
        f"工具      调用 {tools} 次，失败 {fails} 次",
        f"429       {c429} 次",
        f"专名泄漏  {lore} 次（已自动清理）",
        f"台词复读  {copy} 次（与样本重叠≥8字）",
        f"复读重写  {copy_fix} 次（重写后有改善）",
        f"风格兜底  {fb} 次（检索无命中，退回随机）",
        f"风格弱命中 {weak} 次（没有特别像的组，余量<0.10）",
        f"流中断    {mid_err} 次（中途断连/网关 error），其中降预算重发 {s_retry} 次",
        f"常用工具  {top}",
    ])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(summarize(int(sys.argv[1]) if len(sys.argv) > 1 else 200))
