// ===== 达妮娅陪伴系统 · 前端逻辑（对接 Python 后端）=====

let bridge = null;      // Python 桥接对象
let chatText = "";      // 当前流式回复累积文本

// 连接状态指示
function setConnStatus(text, ok) {
    let el = document.getElementById('conn-status');
    if (!el) {
        el = document.createElement('span');
        el.id = 'conn-status';
        el.style.cssText = 'font-size:11px;padding:2px 8px;border-radius:10px;margin-left:8px;';
        document.querySelector('.topbar-right').insertBefore(el, document.querySelector('.status-dot'));
    }
    el.textContent = text;
    el.style.background = ok ? 'rgba(0,245,255,0.15)' : 'rgba(255,45,163,0.15)';
    el.style.color = ok ? '#00f5ff' : '#ff63bb';
    el.style.border = '1px solid ' + (ok ? 'rgba(0,245,255,0.4)' : 'rgba(255,45,163,0.4)');
}

// 初始化 QWebChannel（带重试，确保 qt.webChannelTransport 就绪）
function initBridge() {
    if (typeof QWebChannel === "undefined") {
        console.warn("qwebchannel.js 未加载，运行在 mock 模式");
        setConnStatus("未连接", false);
        setupMockBridge();
        return;
    }
    let tries = 0;
    function tryConnect() {
        if (typeof qt !== "undefined" && qt.webChannelTransport) {
            new QWebChannel(qt.webChannelTransport, function (channel) {
                bridge = channel.objects.bridge;
                if (!bridge) {
                    console.error("QWebChannel 连接成功但 bridge 对象为空");
                    setConnStatus("桥接失败", false);
                    return;
                }
                // 调试：打印 bridge 暴露的属性
                console.log("bridge keys:", Object.keys(bridge));
                // 连接 Python → JS 信号（逐个检查，避免 undefined 报错）
                if (bridge.chat_started) bridge.chat_started.connect(onChatStarted);
                else console.warn("bridge.chat_started 不存在");
                if (bridge.chat_delta) bridge.chat_delta.connect(onChatDelta);
                else console.warn("bridge.chat_delta 不存在");
                if (bridge.chat_finished) bridge.chat_finished.connect(onChatFinished);
                else console.warn("bridge.chat_finished 不存在");
                if (bridge.chat_failed) bridge.chat_failed.connect(onChatFailed);
                if (bridge.service_updated) bridge.service_updated.connect(onServiceUpdated);
                if (bridge.tool_event) bridge.tool_event.connect(onToolEvent);
                if (bridge.log) bridge.log.connect(onLog);
                if (bridge.reminder_fired) bridge.reminder_fired.connect(showReminder);
                console.log("[Console] 桥接已就绪");
                setConnStatus("已连接", true);
                refreshServices();
                setupWindowControls();
            });
        } else {
            tries++;
            if (tries < 50) {
                setTimeout(tryConnect, 100);
            } else {
                console.warn("qt.webChannelTransport 5 秒内未就绪，运行在 mock 模式");
                setConnStatus("未连接", false);
                setupMockBridge();
            }
        }
    }
    tryConnect();
}

// mock bridge（浏览器直接打开时使用）
function setupMockBridge() {
    // 浏览器模式：有系统窗口边框，隐藏网页内的窗口控制
    const wc = document.querySelector('.win-controls');
    if (wc) wc.style.display = 'none';
    const wrap = (val) => Promise.resolve(val);
    bridge = {
        send_message: (t) => {
            // 模拟工具协作流（含"打开"时）
            if (/打开|启动/.test(t)) {
                setTimeout(() => onToolEvent(JSON.stringify({
                    type: 'tool_start', name: 'open_app',
                    description: '打开电脑里的应用/网站/文件', query: t,
                })), 400);
                setTimeout(() => onToolEvent(JSON.stringify({
                    type: 'tool_finish', name: 'open_app',
                    description: '打开电脑里的应用/网站/文件',
                    result: '（系统信息：已为用户启动「网易云音乐」。请用达妮娅的语气确认一下）',
                })), 1200);
            }
            setTimeout(() => onChatFinished('（mock）我收到了：' + t), 1800);
        },
        replay_voice: () => { console.log("[mock] replay voice"); },
        copy_text: (t) => { try { navigator.clipboard.writeText(t); } catch (e) { } },
        get_memories: () => wrap(JSON.stringify([
            { text: "你喜欢星空，尤其夏天的银河", source: "偏好", time: "今天" },
            { text: "你习惯在深夜工作，作息偏晚", source: "日常", time: "昨天" },
        ])),
        get_tasks: () => wrap(JSON.stringify([
            { text: "整理陪伴系统灰度骨架", done: true, prio: "高" },
            { text: "确认首页与对话结构", done: false, prio: "高" },
            { text: "接入语音合成流式播放", done: false, prio: "中" },
        ])),
        get_services: () => wrap(JSON.stringify([
            { name: "ollama", display: "Ollama 推理引擎", state: "running" },
            { name: "gpt_sovits", display: "GPT-SoVITS 语音合成", state: "running" },
            { name: "napcat", display: "NapCat 消息桥接", state: "stopped" },
        ])),
        get_settings: () => wrap(JSON.stringify([
            { label: "角色形象", value: "达妮娅", type: "str" },
            { label: "默认缩放", value: "0.7x", type: "str" },
            { label: "语音合成", value: "GPT-SoVITS", type: "str" },
            { label: "LLM 模型", value: "qwen2.5:7b", type: "str" },
        ])),
        start_service: () => { },
        stop_service: () => { },
        save_setting: () => { },
        chat_started: { connect: () => { } },
        chat_delta: { connect: () => { } },
        chat_finished: { connect: (cb) => { window._mockFinish = cb; } },
        chat_failed: { connect: () => { } },
        service_updated: { connect: () => { } },
        tool_event: { connect: () => { } },
        log: { connect: () => { } },
        reminder_fired: { connect: () => { } },
    };
}

// 页面就绪后初始化桥接
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initBridge);
} else {
    initBridge();
}

// ===== 无边框窗口：顶栏拖动 / 双击最大化 / 控制按钮 / 边缘拉伸 =====
function setupWindowControls() {
    const topbar = document.querySelector('.topbar');
    const minBtn = document.getElementById('win-min');
    const maxBtn = document.getElementById('win-max');
    const closeBtn = document.getElementById('win-close');
    if (!topbar || !minBtn || !maxBtn || !closeBtn) return;

    const call = (name) => { if (bridge && bridge[name]) try { bridge[name](); } catch (e) { } };

    minBtn.addEventListener('click', () => call('minimize_window'));
    maxBtn.addEventListener('click', () => call('toggle_max_window'));
    closeBtn.addEventListener('click', () => call('close_window'));

    // 最大化/还原图标同步
    const iconMax = maxBtn.querySelector('.icon-max');
    const iconRestore = maxBtn.querySelector('.icon-restore');
    function updateMaxIcon(maxed) {
        if (iconMax) iconMax.style.display = maxed ? 'none' : '';
        if (iconRestore) iconRestore.style.display = maxed ? '' : 'none';
        maxBtn.title = maxed ? '还原' : '最大化';
    }
    if (bridge && bridge.win_state_changed) bridge.win_state_changed.connect(updateMaxIcon);
    // 初始同步：窗口默认最大化时显示还原图标
    if (bridge && bridge.is_maximized) {
        try { updateMaxIcon(!!bridge.is_maximized()); } catch (e) { }
    }

    // 顶栏拖动：按下后移动超过阈值才进入系统拖动，避免吞掉双击
    let down = null;
    topbar.addEventListener('mousedown', (e) => {
        if (e.button !== 0 || e.target.closest('button')) return;
        down = { x: e.screenX, y: e.screenY };
    });
    topbar.addEventListener('mousemove', (e) => {
        if (!down) return;
        if (Math.abs(e.screenX - down.x) + Math.abs(e.screenY - down.y) > 6) {
            down = null;
            call('drag_window');
        }
    });
    window.addEventListener('mouseup', () => { down = null; });
    topbar.addEventListener('dblclick', (e) => {
        if (e.target.closest('button')) return;
        call('toggle_max_window');
    });

    // 窗口边缘/四角拉伸
    if (bridge && bridge.resize_window) {
        const edges = [
            ['n', 'top:0;left:6px;right:6px;height:4px;cursor:n-resize'],
            ['s', 'bottom:0;left:6px;right:6px;height:4px;cursor:s-resize'],
            ['e', 'right:0;top:6px;bottom:6px;width:4px;cursor:e-resize'],
            ['w', 'left:0;top:6px;bottom:6px;width:4px;cursor:w-resize'],
            ['ne', 'top:0;right:0;width:10px;height:10px;cursor:ne-resize'],
            ['nw', 'top:0;left:0;width:10px;height:10px;cursor:nw-resize'],
            ['se', 'bottom:0;right:0;width:10px;height:10px;cursor:se-resize'],
            ['sw', 'bottom:0;left:0;width:10px;height:10px;cursor:sw-resize'],
        ];
        edges.forEach(([dir, css]) => {
            const el = document.createElement('div');
            el.style.cssText = `position:fixed;z-index:9999;${css}`;
            el.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                e.preventDefault();
                try { bridge.resize_window(dir); } catch (err) { }
            });
            document.body.appendChild(el);
        });
    }
}

// ===== 导航切换 =====
const navBtns = document.querySelectorAll('.nav-btn');
const pages = document.querySelectorAll('.page');

navBtns.forEach(btn => {
    btn.addEventListener('click', () => {
        const target = btn.dataset.page;
        navBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        pages.forEach(p => p.classList.remove('active'));
        document.getElementById('page-' + target).classList.add('active');
        // 进入页面时刷新数据
        if (target === 'memory') loadMemories();
        if (target === 'task') loadTasks();
        if (target === 'launch') refreshServices();
        if (target === 'settings') loadSettings();
        if (target === 'collab') btn.classList.remove('tool-pulse');
    });
});

// ===== 问候语 =====
function updateGreeting() {
    const h = new Date().getHours();
    let text = '你好';
    if (h < 6) text = '夜深了';
    else if (h < 11) text = '早上好';
    else if (h < 14) text = '中午好';
    else if (h < 18) text = '下午好';
    else text = '晚上好';
    document.getElementById('greeting').textContent = text;
}
updateGreeting();

// ===== 聊天 =====
const VOICE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
const COPY_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const CHECK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

// 为一条 bot 消息绑定语音重播 / 复制按钮
function bindBotActions(msgEl) {
    const bubble = msgEl.querySelector('.msg-bubble');
    const voiceBtn = msgEl.querySelector('.act-voice');
    const copyBtn = msgEl.querySelector('.act-copy');
    if (voiceBtn) {
        voiceBtn.addEventListener('click', () => {
            const t = bubble.textContent.trim();
            if (t && bridge && bridge.replay_voice) bridge.replay_voice(t);
        });
    }
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            const t = bubble.textContent.trim();
            if (!t) return;
            const done = () => {
                copyBtn.classList.add('copied');
                copyBtn.innerHTML = CHECK_ICON;
                setTimeout(() => {
                    copyBtn.classList.remove('copied');
                    copyBtn.innerHTML = COPY_ICON;
                }, 1200);
            };
            // 优先走 Python 剪贴板桥接（QWebEngine 内 navigator.clipboard 可能无权限）
            if (bridge && bridge.copy_text) {
                try { bridge.copy_text(t); done(); return; } catch (e) { }
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(t).then(done).catch(() => { });
            }
        });
    }
}

function appendMsg(text, who) {
    const box = document.getElementById('chat-messages');
    const msg = document.createElement('div');
    msg.className = 'msg msg-' + who;
    if (who === 'bot') {
        msg.innerHTML =
            '<div class="msg-avatar"><img src="assets/dania_idle.png" /></div>' +
            '<div class="msg-body">' +
            '<div class="msg-name"><span class="name-diamond">◇</span>达妮娅</div>' +
            '<div class="msg-bubble" id="bot-msg-latest"></div>' +
            '<div class="msg-actions">' +
            '<button class="msg-act act-voice" title="播放语音">' + VOICE_ICON + '</button>' +
            '<button class="msg-act act-copy" title="复制">' + COPY_ICON + '</button>' +
            '</div></div>';
        box.appendChild(msg);
        bindBotActions(msg);
    } else {
        msg.innerHTML = '<div class="msg-bubble"></div>';
        msg.querySelector('.msg-bubble').textContent = text;
        box.appendChild(msg);
    }
    box.scrollTop = box.scrollHeight;
}

// 定时提醒到点：以达妮娅气泡显示在对话区
function showReminder(text) {
    appendMsg('', 'bot');
    const box = document.getElementById('chat-messages');
    const bubbles = box.querySelectorAll('.msg-bot .msg-bubble');
    const el = bubbles[bubbles.length - 1];
    if (el) {
        el.textContent = text;
        if (el.id) el.removeAttribute('id');  // 不占用流式气泡标记
    }
    box.scrollTop = box.scrollHeight;
}

function onChatStarted() {
    chatText = "";
    appendMsg("", "bot");
}

function onChatDelta(delta) {
    chatText += delta;
    const el = document.getElementById('bot-msg-latest');
    if (el) {
        el.textContent = chatText;
        const box = document.getElementById('chat-messages');
        box.scrollTop = box.scrollHeight;
    }
}

function onChatFinished(fullText) {
    const el = document.getElementById('bot-msg-latest');
    if (el) {
        el.textContent = fullText || chatText;
        el.removeAttribute('id');
    }
    chatText = "";
    const box = document.getElementById('chat-messages');
    box.scrollTop = box.scrollHeight;
    collabFinish();
}

function onChatFailed(err) {
    const el = document.getElementById('bot-msg-latest');
    if (el) {
        el.textContent = "出错了… " + err;
        el.removeAttribute('id');
    }
    collabError();
}

function onLog(text) {
    // 可在控制台输出，或显示到日志区
    console.log("[后端]", text);
}

function sendMessage(inputEl) {
    const text = inputEl.value.trim();
    if (!text || !bridge) return;
    appendMsg(text, 'user');
    inputEl.value = '';
    collabBegin(text);
    bridge.send_message(text);
}

// 首页输入框（对话已合并到首页）
    const input1 = document.getElementById('chat-input');
    const btn1 = document.getElementById('send-btn');
    btn1.addEventListener('click', () => sendMessage(input1));
    input1.addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(input1); });

// 静态欢迎气泡的按钮绑定
document.querySelectorAll('#chat-messages .msg-bot').forEach(bindBotActions);

// ===== 协作流 =====
const TOOL_DISPLAY = {
    open_app: '启动本机应用',
    query_time: '查询时间',
    set_reminder: '设置提醒',
    screen_context: '屏幕理解',
};

let collabThinkingEl = null;
let collabToolCard = null;
let collabHasTool = false;

function toolDisplayName(ev) {
    return TOOL_DISPLAY[ev.name] || ev.description || ev.name;
}

// 从工具注入 LLM 的上下文文本中提取人类可读片段
function cleanToolLog(s) {
    if (!s) return '';
    s = s.replace(/^（系统信息：/, '').replace(/）\s*$/, '');
    s = s.replace(/请用达妮娅的语气[^。]*。?/g, '');
    s = s.replace(/（若系统[^）]*）/g, '');
    return s.trim();
}

function setBannerState(state, text) {
    const el = document.getElementById('banner-state');
    el.className = 'banner-state ' + state;
    document.getElementById('banner-state-text').textContent = text;
}

function setWorkspace(dotCls, title, sub, contentTitle, contentSub) {
    const dot = document.querySelector('#ws-status .ws-dot');
    dot.className = 'ws-dot ' + dotCls;
    document.getElementById('ws-status-title').textContent = title;
    document.getElementById('ws-status-sub').textContent = sub;
    if (contentTitle !== undefined) {
        document.getElementById('ws-content-title').textContent = contentTitle;
    }
    if (contentSub !== undefined) {
        document.getElementById('ws-content-sub').textContent = contentSub;
    }
}

function collabBegin(goal) {
    collabHasTool = false;
    collabToolCard = null;
    document.getElementById('banner-goal').textContent =
        goal.length > 40 ? goal.slice(0, 40) + '…' : goal;
    setBannerState('working', '正在处理');
    setWorkspace('working', '正在处理', '达妮娅正在思考…', '思考中', '理解你的请求并准备回复');

    const list = document.getElementById('flow-list');
    list.innerHTML = '';
    collabThinkingEl = document.createElement('div');
    collabThinkingEl.className = 'flow-thinking';
    collabThinkingEl.innerHTML = '<span></span><span></span><span></span>';
    list.appendChild(collabThinkingEl);
}

function collabRemoveThinking() {
    if (collabThinkingEl) {
        collabThinkingEl.remove();
        collabThinkingEl = null;
    }
}

function onToolEvent(jsonStr) {
    let ev;
    try { ev = JSON.parse(jsonStr); } catch (e) { return; }

    // 协作导航按钮亮起提示点
    document.querySelector('.nav-btn[data-page="collab"]').classList.add('tool-pulse');

    const displayName = toolDisplayName(ev);
    const list = document.getElementById('flow-list');

    if (ev.type === 'tool_start') {
        collabHasTool = true;
        collabRemoveThinking();
        setBannerState('working', '正在处理');
        setWorkspace('working', '正在处理', '当前工具：' + displayName,
            displayName, '已允许，正在执行…');

        collabToolCard = document.createElement('div');
        collabToolCard.className = 'flow-item working';
        collabToolCard.innerHTML =
            '<div class="flow-tag">已允许，正在执行</div>' +
            '<div class="flow-name"><span class="flow-spinner"></span>达妮娅准备调用：' + displayName + '</div>' +
            '<div class="flow-state">正在执行…</div>' +
            '<div class="flow-log"></div>';
        collabToolCard.querySelector('.flow-log').textContent = ev.description || displayName;
        list.appendChild(collabToolCard);
        list.scrollTop = list.scrollHeight;
    } else if (ev.type === 'tool_finish') {
        collabHasTool = true;
        const logText = cleanToolLog(ev.result) || '工具已完成';
        if (collabToolCard) {
            collabToolCard.className = 'flow-item done';
            collabToolCard.innerHTML =
                '<div class="flow-tag">工具操作</div>' +
                '<div class="flow-name"><span class="flow-check">✓</span>' + displayName + '</div>' +
                '<div class="flow-state">已完成</div>' +
                '<div class="flow-log"></div>';
            collabToolCard.querySelector('.flow-log').textContent =
                '[' + ev.name + '] ' + logText;
        } else {
            // 极端情况下 start 事件丢失：补一张完成卡片
            const card = document.createElement('div');
            card.className = 'flow-item done';
            card.innerHTML =
                '<div class="flow-tag">工具操作</div>' +
                '<div class="flow-name"><span class="flow-check">✓</span>' + displayName + '</div>' +
                '<div class="flow-state">已完成</div>' +
                '<div class="flow-log"></div>';
            card.querySelector('.flow-log').textContent = '[' + ev.name + '] ' + logText;
            list.appendChild(card);
        }
        setWorkspace('working', '正在处理', '当前工具：' + displayName,
            displayName, logText);
        list.scrollTop = list.scrollHeight;
    } else if (ev.type === 'tool_fail') {
        collabHasTool = true;
        collabRemoveThinking();
        const card = document.createElement('div');
        card.className = 'flow-item error';
        card.innerHTML =
            '<div class="flow-tag">工具操作</div>' +
            '<div class="flow-name">✕ ' + displayName + '</div>' +
            '<div class="flow-state">执行失败</div>' +
            '<div class="flow-log"></div>';
        card.querySelector('.flow-log').textContent = ev.error || '未知错误';
        list.appendChild(card);
        list.scrollTop = list.scrollHeight;
        setWorkspace('error', '执行失败', '当前工具：' + displayName,
            displayName, ev.error || '工具执行失败');
        setBannerState('error', '执行失败');
    }
}

function collabFinish() {
    collabRemoveThinking();
    if (collabHasTool) {
        setBannerState('done', '已完成');
        setWorkspace('done', '已完成', '工具执行完毕',
            document.getElementById('ws-content-title').textContent,
            document.getElementById('ws-content-sub').textContent);
    } else {
        // 纯对话：工作区显示对话完成
        setBannerState('done', '已完成');
        setWorkspace('done', '已完成', '达妮娅已回复', '对话回复', '无需调用工具');
    }
}

function collabError() {
    collabRemoveThinking();
    setBannerState('error', '出错了');
    setWorkspace('error', '出错了', '达妮娅回复失败', '处理异常', '请检查服务状态后重试');
}

// ===== 记忆页 =====
function loadMemories() {
    if (!bridge || !bridge.get_memories) return;
    bridge.get_memories().then(raw => {
        const list = JSON.parse(raw);
        const container = document.querySelector('.memory-list');
        container.innerHTML = "";
        if (Array.isArray(list) && list.length === 0) {
            container.innerHTML = '<div class="memory-item"><span class="mem-text" style="color:var(--text-dim)">暂无记忆</span></div>';
            return;
        }
        list.forEach(m => {
            const item = document.createElement('div');
            item.className = 'memory-item';
            item.innerHTML = `
                <span class="mem-tag">${m.source || '记忆'}</span>
                <span class="mem-text">${m.text}</span>
                <span class="mem-time">${m.time || ''}</span>`;
            container.appendChild(item);
        });
    });
}

// ===== 任务页 =====
function loadTasks() {
    if (!bridge || !bridge.get_tasks) return;
    bridge.get_tasks().then(raw => {
        const list = JSON.parse(raw);
        const container = document.querySelector('.task-list');
        container.innerHTML = "";
        if (Array.isArray(list) && list.length === 0) {
            container.innerHTML = '<div class="task-item"><span class="task-text" style="color:var(--text-dim)">暂无任务</span></div>';
            return;
        }
        list.forEach(t => {
            const item = document.createElement('div');
            item.className = 'task-item';
            const prioClass = t.prio === '高' ? 'prio-high' : (t.prio === '中' ? 'prio-mid' : 'prio-low');
            item.innerHTML = `
                <span class="task-check ${t.done ? 'done' : ''}">${t.done ? '✓' : '○'}</span>
                <span class="task-text">${t.text}</span>
                <span class="task-prio ${prioClass}">${t.prio || ''}</span>`;
            container.appendChild(item);
        });
    });
}

// ===== 服务状态页 =====
function refreshServices() {
    if (!bridge || !bridge.get_services) return;
    bridge.get_services().then(raw => {
        const list = JSON.parse(raw);
        const container = document.querySelector('.service-list');
        container.innerHTML = "";
        list.forEach(s => {
            const item = document.createElement('div');
            item.className = 'service-item';
            const stateClass = s.state === 'running' ? 'running' : 'stopped';
            const stateText = { running: '运行中', stopped: '已停止', starting: '启动中', failed: '失败' }[s.state] || s.state;
            item.innerHTML = `
                <span class="svc-name">${s.display}</span>
                <div style="display:flex;gap:8px;align-items:center;">
                    <span class="svc-status ${stateClass}">${stateText}</span>
                    <button class="svc-btn" data-name="${s.name}" data-action="${s.state === 'running' ? 'stop' : 'start'}">
                        ${s.state === 'running' ? '停止' : '启动'}
                    </button>
                </div>`;
            container.appendChild(item);
        });
        container.querySelectorAll('.svc-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const name = btn.dataset.name;
                const action = btn.dataset.action;
                if (action === 'start') bridge.start_service(name);
                else bridge.stop_service(name);
            });
        });
    });
}

function onServiceUpdated(jsonStr) {
    // 服务状态变化，刷新列表
    refreshServices();
}

// ===== 设置页 =====
function loadSettings() {
    if (!bridge || !bridge.get_settings) return;
    bridge.get_settings().then(raw => {
        const list = JSON.parse(raw);
        const container = document.querySelector('.settings-list');
        container.innerHTML = "";
        list.forEach(s => {
            const row = document.createElement('div');
            row.className = 'setting-row';
            if (s.type === 'bool') {
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <label class="switch">
                        <input type="checkbox" ${s.value ? 'checked' : ''} data-key="${s.key}" />
                        <span class="slider"></span>
                    </label>`;
            } else {
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <span class="set-value">${s.value}</span>`;
            }
            container.appendChild(row);
        });
        container.querySelectorAll('.switch input').forEach(cb => {
            cb.addEventListener('change', () => {
                bridge.save_setting(cb.dataset.key, cb.checked ? 'true' : 'false');
            });
        });
    });
}

// 给服务按钮加点样式（动态生成，放这里）
const style = document.createElement('style');
style.textContent = `
.svc-btn {
    font-size: 12px;
    padding: 4px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--panel-2);
    color: var(--text);
    cursor: pointer;
    transition: all 0.2s;
}
.svc-btn:hover { border-color: var(--cyan); color: var(--cyan); }
`;
document.head.appendChild(style);
