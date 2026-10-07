# denia-agent

有一些功能的 agent，帮助你完成一些事情。

**达妮娅终端**是一个 Windows 桌面 AI 陪伴应用：一个住在你电脑里的角色「达妮娅」，可以聊天、说话（GPT-SoVITS 定制音色）、帮你定时提醒、打开应用、看屏幕，还能通过 NapCat 以 QQ 机器人的形式陪聊。所有对话、语音、知识库全部本地运行，不依赖任何云端 API。

## 功能特性

### 对话与语音
- **LLM 对话**：本地 Ollama 模型（qwen2.5:7b）流式输出，支持多轮上下文
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
- 独立记忆存储（`bot/memory_store.py`）与知识库（`bot/knowledge_base.py`）

## 技术架构

| 组件 | 技术 |
|---|---|
| 桌面 UI | Python 3.12 + PySide6 + QtWebEngine |
| LLM | Ollama（本地，端口 11434） |
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

# 2. 启动 Ollama 并拉取模型
ollama pull qwen2.5:7b

# 3. 启动 GPT-SoVITS api_v2（详见 GPT-SoVITS 官方仓库）

# 4. 启动达妮娅终端
pythonw main.py
```

> GPT-SoVITS 目录仅提交了修改过的 `api_v2.py`（/tts 失败时打印完整 traceback），
> 运行时与预训练模型需从 [GPT-SoVITS 官方仓库](https://github.com/RVC-Boss/GPT-SoVITS) 获取。

## 说明

- 仓库不包含达妮娅音色权重（单文件超 GitHub 100MB 限制），如需使用音色请联系作者获取
- `dania.char` 为角色配置文件
