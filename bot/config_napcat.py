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
USE_LLM      = True                    # True=使用AI模型，False=使用内置模板回复
LLM_API_KEY  = "ollama"               # 本地Ollama不需要真实Key
LLM_MODEL    = "qwen2.5:7b"    # 7B模型，需先 ollama pull qwen2.5:7b
LLM_BASE_URL = "http://127.0.0.1:11434/api/chat"  # Ollama API地址
LLM_API_URL = ""                       # 留空则使用默认地址
# ---- 回复频率控制（防封号）----
REPLY_PROBABILITY_GROUP = 0.3           # 群聊回复概率 30%（10条消息回3条）
REPLY_PROBABILITY_PRIVATE = 1.0         # 私聊回复概率 100%（有问必答）
REPLY_COOLDOWN_GROUP = 15              # 群聊连续回复冷却间隔（秒）
REPLY_COOLDOWN_PRIVATE = 0             # 私聊连续回复冷却间隔（秒），0=无冷却
# ---- 机器人设定 ----
BOT_NAME = "达妮娅"
HISTORY_LIMIT = 10                     # 保留的对话轮数
