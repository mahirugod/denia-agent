"""语气 A/B 的多轮运行器：从 stdin 读对话组，逐组逐轮调用 DeniaLLM，
把结果以 JSONL 写到 stdout。

由 prompt_ab.py 通过子进程调用，环境变量 DENIA_PROMPT_VARIANT 决定 prompt 拼装顺序。
DeniaLLM 自身会往 stdout 打日志，所以构造和调用期间把 stdout 重定向到 stderr，
避免污染给父进程读的 JSONL。

每组是一段独立会话：组内保留历史（测情绪/摘要是否延续），组间清空历史。
"""
import contextlib
import json
import os
import shutil
import sys
import time

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(BOT_DIR)
sys.path.insert(0, BOT_DIR)


def load_config():
    with open(os.path.join(ROOT_DIR, "settings.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return {
        "model_name": cfg.get("model_name", ""),
        "api_key": cfg.get("cloud_api_key", ""),
        "base_url": cfg.get("cloud_base_url", ""),
    }


def _item_turns(item):
    """兼容旧格式（单条 text）与新格式（turns 数组）。"""
    turns = item.get("turns")
    if isinstance(turns, list) and turns:
        return [str(t) for t in turns]
    return [item["text"]] if item.get("text") else []


def main():
    out = sys.stdout
    items = [json.loads(line) for line in sys.stdin if line.strip()]
    cfg = load_config()
    # 评测用的假对话不写真实长期记忆：把 memory_store 的落盘路径整体指到 eval 目录，
    # 并在每次跑批前清空，避免上一轮跑批的摘要/情节混进来
    import memory_store
    _eval_mem = os.path.join(BOT_DIR, "data", "eval", "memory")
    shutil.rmtree(_eval_mem, ignore_errors=True)
    os.makedirs(os.path.join(_eval_mem, "summaries"), exist_ok=True)
    memory_store._DIR = _eval_mem
    memory_store._EPISODES = os.path.join(_eval_mem, "episodes.jsonl")
    memory_store._SUMMARY_DIR = os.path.join(_eval_mem, "summaries")
    memory_store._VEC_PATH = os.path.join(_eval_mem, "vectors.npy")
    memory_store._VEC_META = os.path.join(_eval_mem, "vectors_meta.json")
    memory_store._ARCHIVE = os.path.join(_eval_mem, "episodes_archive.jsonl")
    from denia_llm import DeniaLLM

    with contextlib.redirect_stdout(sys.stderr):
        llm = DeniaLLM(
            model_name=cfg["model_name"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            history_key="eval_ab",
            allow_confirm=False,
        )
        # 评测用的假对话不写长期记忆，避免污染真实 episodes
        llm.flush_memory_on_exit = lambda: None
        # 心情也要隔离：否则跑批会改写用户真实的 data/mood.json
        llm._mood_file = os.path.join(_eval_mem, "mood.json")
        llm._mood = 0
    variant = os.environ.get("DENIA_PROMPT_VARIANT", "new")
    for item in items:
        turns = _item_turns(item)
        t0 = time.time()
        replies, errors = [], []
        with contextlib.redirect_stdout(sys.stderr):
            # 组间隔离：历史、滚动摘要、实体、心情全部回到起点；
            # 组内不动，才能测出"上一句情绪是否延续""摘要是否被正确注入"
            try:
                llm.clear_history()
                llm._mood = 0
                llm._mood_low_active = False
                llm._tone_transient = None
                llm._mem_pending.clear()
            except Exception as e:
                print(f"[评测] 组间重置失败（忽略）: {e}")
            for turn in turns:
                llm._last_decode_error = None
                try:
                    reply = llm.get_response(turn)
                except Exception as e:
                    reply = f"<ERROR> {e}"
                    llm._last_decode_error = "exception"
                replies.append(reply)
                errors.append(llm._last_decode_error)
                pending = getattr(llm, "_mem_pending", None)
                if isinstance(pending, list):
                    pending.clear()
        out.write(json.dumps({
            "id": item["id"],
            "turns": turns,
            "replies": replies,
            "ms": int((time.time() - t0) * 1000),
            "variant": variant,
            "error": next((e for e in errors if e), None),
        }, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
