# ============================================================
# NapCat 适配层配置文件
# ============================================================

# ---- NapCat HTTP API 地址 ----
# NapCat 默认在 3000 端口提供 HTTP API
# 如果你改过 NapCat 的端口，请对应修改
NAPCAT_HTTP_URL = "http://127.0.0.1:3000"

# ---- NapCat WebSocket 地址 ----
# WebSocket 方式接收消息更可靠
NAPCAT_WS_URL = "ws://127.0.0.1:3000"

# ---- 本服务监听端口 ----
# NapCat 会把消息上报到这个端口
LISTEN_PORT = 5700

# ---- AI 模型配置 ----
# 与桌面端共用 settings.json 的模型配置，保证 QQ 也走同一条 agent 链路
# （原生 function calling + 多步工具循环）。settings.json 缺失时回落到本地 Ollama。
import json as _json
import os as _os

_SETTINGS_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "settings.json"
)
try:
    with open(_SETTINGS_PATH, encoding="utf-8") as _f:
        _SETTINGS = _json.load(_f)
except Exception:
    _SETTINGS = {}

USE_LLM      = True                    # True=使用AI模型，False=使用内置模板回复
# QQ 端固定走云端 agnes：桌面端切到本地模型时 QQ 不受影响；
# 若 settings.json 明确选了别的云端模型（cloud:<name>）则沿用该选择。
_SETTINGS_MODEL = _SETTINGS.get("model_name") or ""
_HAS_CLOUD = bool(_SETTINGS.get("cloud_api_key") and _SETTINGS.get("cloud_base_url"))
if _SETTINGS_MODEL.startswith(("agnes", "cloud:")):
    LLM_MODEL = _SETTINGS_MODEL
elif _HAS_CLOUD:
    LLM_MODEL = "agnes-3.0-flash"
else:
    LLM_MODEL = _SETTINGS_MODEL or "qwen2.5:7b"
LLM_API_KEY  = _SETTINGS.get("cloud_api_key") or "ollama"   # 本地Ollama不需要真实Key
LLM_BASE_URL = _SETTINGS.get("cloud_base_url") or "http://127.0.0.1:11434/api/chat"
# 选了云端模型却没有 cloud_base_url 时，base 仍是 Ollama 地址，
# 拼上 /chat/completions 必然 404 —— 这里直接回落到本地模型
if LLM_MODEL.startswith(("agnes", "cloud:")) and not _SETTINGS.get("cloud_base_url"):
    print("[配置] 选了云端模型但缺少 cloud_base_url，QQ 端回落到本地 qwen2.5:7b")
    LLM_MODEL = "qwen2.5:7b"
    LLM_API_KEY = "ollama"
    LLM_BASE_URL = "http://127.0.0.1:11434/api/chat"
LLM_API_URL = ""                       # 留空则使用默认地址
# ---- 回复频率控制（防封号）----
REPLY_PROBABILITY_GROUP = 0.3           # 群聊回复概率 30%（10条消息回3条）
REPLY_PROBABILITY_PRIVATE = 1.0         # 私聊回复概率 100%（有问必答）
REPLY_COOLDOWN_GROUP = 15              # 群聊连续回复冷却间隔（秒）
REPLY_COOLDOWN_PRIVATE = 0             # 私聊连续回复冷却间隔（秒），0=无冷却
# ---- 机器人设定 ----
BOT_NAME = "达妮娅"
HISTORY_LIMIT = 10                     # 保留的对话轮数
