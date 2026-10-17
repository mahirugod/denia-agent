# denia-agent

有一些功能的 agent，帮助你完成一些事情。

**达妮娅终端**是一个 Windows 桌面 AI 陪伴应用：一个住在你电脑里的角色「达妮娅」，可以聊天、说话（GPT-SoVITS 定制音色）、帮你定时提醒、打开应用、看屏幕，还能通过 NapCat 以 QQ 机器人的形式陪聊。支持本地模型（Ollama 可选）与云端模型（ags / 任意 OpenAI 兼容）两条路由，语音、知识库、工具全部本地运行，LLM 可按需选择本地或云端。

## 功能特性

### 对话与语音
- **LLM 对话**：支持本地模型（Ollama 可选，或其它兼容推理后端）、ags 云端模型（`agnes` 前缀）、通用云端模型（`cloud:` 前缀）三种路由，流式输出，支持多轮上下文
- **语音合成**：GPT-SoVITS api_v2 定制音色（v2ProPlus 微调权重），失败即静默不降级
- **语音识别**：麦克风输入转文字（mic_stream + 识别器）
- **图像识别**：发送图片让达妮娅「看」屏幕内容

### 工具能力（自动路由）
- **定时提醒**：「一分钟后提醒我喝水」，中文数字、多种语序都支持，到点气泡通知
- **打开应用**：「帮我启动网易云音乐」——开始菜单快捷方式 / 本地 exe / 注册表解析，找不到就如实说
- **时间查询**、**截屏看屏** 等内置工具（`bot/dania_tools.py` 一文件注册，`@tool` 装饰器自动路由）

### 桌面集成
- **Web 控制台**：无边框窗口 + 动漫风格 UI，打字机效果逐字输出，语音与气泡同步
- **悬浮桌宠**：随状态切换立绘（idle / speaking / thinking / happy / sleeping）
- **终端窗口**：经典终端风格对话界面
- **系统托盘**：最小化到托盘常驻

### QQ 机器人（可选）
- 通过 **NapCat** WebSocket 接入 QQ，支持私聊 / 群聊、语音回复、图片理解
- 知识库（`bot/knowledge_base.py`）

## 技术架构

| 组件 | 技术 |
|---|---|
| 桌面 UI | Python 3.12 + PySide6 + QtWebEngine |
| LLM | 本地模型（Ollama 可选，端口 11434）/ ags 云端 / 通用云端（`cloud:` 前缀） |
| 语音合成 | GPT-SoVITS api_v2（端口 9880，v2ProPlus） |
| QQ 接入 | NapCat（WebSocket） |
| 服务管理 | process_manager 统一拉起 / 掉线自动重启 |

## 目录结构

```
denia_terminal/
├── main.py              # 应用入口（pythonw main.py 启动）
├── console/             # Web 控制台前端（HTML/CSS/JS）
├── bot/                 # 对话后端（LLM、语音、工具、QQ 适配）
├── GPT-SoVITS/          # 语音服务（仅入库改过的 api_v2.py）
├── napcat/              # QQ 协议端运行时（不入库）
└── assets/              # 达妮娅立绘与托盘图标
```

## 快速开始

```bash
# 1. 安装 Python 依赖
pip install -r bot/requirements.txt

# 2. 安装本地推理后端（Ollama 可选，二选一）
#    方式 A：Ollama（推荐本地默认）
#      下载地址: https://ollama.com/download
#      安装后若不在默认位置（C:\Program Files\Ollama），
#      设置环境变量 OLLAMA_HOME 指向其安装目录（process_manager 会自动探测）
#    方式 B：其它兼容推理后端（LM Studio / vLLM / llama.cpp 等）
#      在 settings.json 的 llm_url 指向该后端的 OpenAI 兼容 /chat 接口即可，无需安装 Ollama

# 3. 拉取/导入模型（两种方式，任选其一）
#    本地 Ollama 模型：
#    先启动 Ollama 服务，再执行：
#      ollama pull qwen2.5:3b
#    若 Ollama 默认拉到了别的目录（如 ~/.ollama/models 或 OLLAMA_MODELS 指向的目录），
#    用导入脚本一键搬进项目：
python import_models.py                  # 自动检测 Ollama 模型库，导入全部
python import_models.py qwen2.5:3b       # 只导入指定模型
python import_models.py --from D:\ollama_models   # 从指定目录导入
#    导入目标统一是 项目/models/（Ollama 重定向库布局）

#    非 Ollama 本地模型（gguf/其它推理后端）：
#    把模型放进 项目/models/local/<model_name>/（目录或 .gguf 文件均可），
#    并把 settings.json 的 model_name 设为 <model_name>，llm_url 指向你的推理后端
#    模型已有下载文件，可用导入脚本一键搬到通用布局：
python import_models.py --layout local --from D:\my_models_dir my_model_name

# 4. 部署 GPT-SoVITS 语音服务
#    代码: https://github.com/RVC-Boss/GPT-SoVITS
#    预训练模型: https://huggingface.co/lj1995/GPT-SoVITS
#               （国内镜像: https://hf-mirror.com/lj1995/GPT-SoVITS）
#    下载后放入 GPT_SoVITS/pretrained_models/，再启动 api_v2（端口 9880）

# 5. 启动达妮娅终端
pythonw main.py
#    启动后 Ollama 服务（仅当 model_name 是本地 Ollama 模型时）会被 process_manager 自动拉起，
#    控制台会打印 [模型检测] 本地可用模型: qwen2.5:3b ...
#    对达妮娅说「有哪些模型」可查询可用模型；设置页 LLM 下拉也会自动列出
```

## 模型导入说明

达妮娅**只识别项目内 `models/` 文件夹里的模型**，不依赖 Ollama 服务，也不扫描全局 `~/.ollama/models`。

支持两种布局：

**布局 1：Ollama 重定向库布局（本地 Ollama 模型）**
```
项目/models/
├── manifests/registry.ollama.ai/library/<model>/<tag>   # manifest 文件
└── blobs/sha256-<hex>                                   # 模型权重（平铺）
```

**布局 2：通用本地模型布局（非 Ollama 推理后端）**
```
项目/models/local/<model_name>/        # 目录形式
项目/models/local/<model_name>.gguf    # 或直接放 gguf 文件
```

**导入方式（Ollama 模型）**：
- **方式 A（推荐）**：设好 `OLLAMA_HOME` 后启动 Ollama，执行 `ollama pull qwen2.5:3b`——`process_manager` 会把 Ollama 重定向到项目 `models/`，直接拉进项目
- **方式 B**：模型已拉到别处（`~/.ollama/models` 或 `D:\ollama_models` 等），用导入脚本一键搬进项目：
  ```bash
  python import_models.py                  # 自动检测 Ollama 模型库，导入全部
  python import_models.py qwen2.5:3b       # 只导入指定模型
  python import_models.py --from D:\ollama_models   # 从指定目录导入
  ```
  导入目标统一是 `项目/models/`。

**导入方式（非 Ollama 本地模型）**：
- 直接把模型文件放到 `项目/models/local/<model_name>/`（或 `models/local/<model_name>.gguf`），再把 `settings.json` 的 `model_name` 设为 `<model_name>`，`llm_url` 指向你的推理后端即可；
- 或已有模型文件目录，用导入脚本一键搬运：
  ```bash
  python import_models.py --layout local --from D:\my_models_dir my_model_name
  ```

**`process_manager` 如何定位 Ollama 可执行文件**（跨机器通用，无需改代码；仅本地 Ollama 模型需要）：
1. 环境变量 `OLLAMA_HOME`（指向 Ollama 安装目录或 ollama.exe）
2. 常见安装位置：`C:\Program Files\Ollama`、`C:\Program Files (x86)\Ollama`、`D:\Ollama`、`%LOCALAPPDATA%\Ollama`
3. `PATH` 中的 `ollama`

都找不到时启动会给出提示，手动设 `OLLAMA_HOME` 即可。使用非 Ollama 后端则无需关心此部分。

**`process_manager` 如何定位模型库**：启动 Ollama 时设 `OLLAMA_MODELS=项目/models`，因此 Ollama 模型随项目走，clone 后只要 `models/` 里有模型即可用，无需重新拉取。

**检测机制**（`bot/skills/model_detect.py`）：
- **只扫描本地 `models/` 文件夹**（同时支持上述两种布局），不依赖 Ollama 服务是否在跑
- 启动时后台自检并打印结果；对话中说「有哪些模型」可达妮娅转述清单
- 如果 `models/` 里没有模型，达妮娅会提示「本地没有可用模型，请把模型放进 models/ 文件夹」

## 云端模型说明（ags / 通用云端）

不想用本地推理后端时，可直接用云端模型，**完全不依赖 Ollama**：

- **ags 模型**（`model_name` 形如 `agnes-3.0-flash`，以 `agnes` 开头）：
  读取 `settings.json` 的 `cloud_api_key` / `cloud_base_url`，模型名直接用 `model_name`，直接在设置页填入即可。
- **通用云端模型**（`model_name` 形如 `cloud:<model>`）：
  同样读取 `cloud_api_key` / `cloud_base_url`，模型名用 `model_name` 去掉 `cloud:` 前缀，支持任意 OpenAI 兼容接口。

切到云端模型后，Ollama 服务不会被拉起，主动巡检也不会检查本地推理后端状态。

## 用户画像（自动记忆）

- 达妮娅会通过对话**自动学习**你的城市、昵称、喜好、作息等，存到 `bot/data/user_profile.json`
- 学习到的画像会注入 system prompt，让她回答更贴合你（如查天气自动用你所在的城市）
- **仅云端大模型生效**：本地小模型（Ollama 3B 级）上下文有限，画像注入会自动跳过，避免撑爆 4K 窗口
- 想手动预填或修改，直接编辑 `bot/data/user_profile.json`（字段见文件内注释）

## 天气查询自动补城市

- 你说"今天下雨吗"但没带城市时，达妮娅会自动补上你画像里的城市（没有就回落 `settings.json` 的 `weather_city`）
- 搜索结果里的旧日期不会误导"今天/明天"的判断——当前日期始终以系统注入的时间为准

## TTS 读法优化

- 温度会按口语读出来：`15~23℃` → "十五到二十三摄氏度"，`-1℃` → "零下一度"
- 由 `bot/voice/engine_adapters.py` 的 `normalize_for_tts` 统一处理

## 敏感文件说明

- `settings.json` 含你的 API Key，**不入库**（已 gitignore）
- 仓库提供脱敏模板 `settings.template.json`（Key 留空），新使用者复制为 `settings.json` 后填入自己的 Key 即可

## 说明

- GPT-SoVITS 目录仅提交了修改过的 `api_v2.py`（/tts 失败时打印完整 traceback），
  运行时与预训练模型需按上方地址自行获取
- 仓库不包含达妮娅音色权重（单文件超 GitHub 100MB 限制），如需使用音色请联系作者获取
- `dania.char` 为角色配置文件
