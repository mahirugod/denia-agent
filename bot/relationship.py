"""关系状态：跨会话的熟悉度（区别于短周期的心情）。

- familiarity 0-100：随互动缓慢增长，长时间不聊会缓慢衰减
- 只用于提示词里的"相处阶段"和主动搭话节律，不向用户展示数字
- 写入很轻：每次用户发言累加一次，进程退出时随内存状态落盘一次即可
"""
import json
import os
import threading
import time

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DIR, "relationship.json")
_lock = threading.RLock()

_state = {"familiarity": 0.0, "days": 0, "last_seen": "", "turns": 0}
_loaded = {"done": False}
_decayed = {"date": ""}  # 防止同一天反复读取时重复衰减

# 相处阶段：(上限, 名称, 提示词里的一句话)
_STAGES = (
    (10, "初识", "刚认识不久，别自来熟，保持礼貌的距离"),
    (30, "熟悉", "已经聊过一阵子，可以自然开玩笑，不用太客气"),
    (60, "亲近", "关系挺亲近，可以偶尔撒娇、用更随意的称呼"),
    (101, "依赖", "已经很依赖对方，说话随意，会主动关心对方的日常"),
)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _days_between(a: str, b: str) -> int:
    try:
        ta = time.mktime(time.strptime(a, "%Y-%m-%d"))
        tb = time.mktime(time.strptime(b, "%Y-%m-%d"))
        return max(0, int((tb - ta) // 86400))
    except Exception:
        return 0


def _load():
    if _loaded["done"]:
        return
    _loaded["done"] = True
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _state.update({
                "familiarity": float(data.get("familiarity", 0)),
                "days": int(data.get("days", 0)),
                "last_seen": str(data.get("last_seen", "")),
                "turns": int(data.get("turns", 0)),
            })
    except (OSError, ValueError, TypeError):
        pass


def _save():
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, _FILE)
    except OSError:
        pass


def state() -> dict:
    """读当前状态（只读副本）；跨天时按间隔做衰减，但不在这里落盘。"""
    _load()
    with _lock:
        today = _today()
        if (_state["last_seen"] and _state["last_seen"] != today
                and _decayed["date"] != today):
            gap = _days_between(_state["last_seen"], today)
            _decayed["date"] = today
            if gap > 1:
                # 冷落每多一天掉 0.8，单次最多掉 20，避免长期不聊直接归零
                _state["familiarity"] = max(
                    0.0, _state["familiarity"] - min(20.0, (gap - 1) * 0.8))
                _state["days"] += gap
        return dict(_state)


def bump(user_text: str) -> dict:
    """一次用户发言：基础 +1；长句 +1；带情绪词 +1；每天首次 +1。"""
    _load()
    text = user_text or ""
    gain = 1.0
    if len(text) >= 20:
        gain += 1.0
    if any(w in text for w in ("累", "难过", "开心", "喜欢", "烦", "想你", "谢谢")):
        gain += 1.0
    with _lock:
        today = _today()
        st = state()
        if st.get("last_seen") != today:
            gain += 1.0
            _state["last_seen"] = today
        _state["familiarity"] = min(100.0, float(st.get("familiarity", 0)) + gain)
        _state["turns"] = int(st.get("turns", 0)) + 1
    return dict(_state)


def stage() -> tuple:
    """返回 (名称, 提示句)。"""
    fam = state().get("familiarity", 0)
    for cap, name, hint in _STAGES:
        if fam < cap:
            return name, hint
    return _STAGES[-1][1], _STAGES[-1][2]


def hint() -> str:
    name, line = stage()
    return f"【相处】当前阶段：{name}。{line}。"


def interval_scale() -> float:
    """主动搭话的间隔倍数：越熟越短（1.0 → 0.6）。"""
    fam = max(0.0, min(100.0, state().get("familiarity", 0)))
    return round(1.0 - 0.4 * fam / 100.0, 2)


_FOLLOWUP_MIN = 10.0  # 至少到"熟悉"阶段，才会主动追问用户的私事


def followup_ok() -> bool:
    """关系是否熟到可以追问私事（如"面试后来怎么样"）。

    刚认识时追问生日/面试这类事会显得越界，先攒熟悉度再开口。
    """
    return state().get("familiarity", 0.0) >= _FOLLOWUP_MIN


def flush():
    """进程退出时落盘。"""
    with _lock:
        _save()
