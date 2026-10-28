// ===== 达妮娅终端 · 前端逻辑（对接 Python 后端）=====

let bridge = null;      // Python 桥接对象
let chatText = "";      // 当前流式回复累积文本
let _stallEl = null;    // "先应答"占位气泡（首字未到时显示）

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
                if (bridge.chat_stall) bridge.chat_stall.connect(onChatStall);
                if (bridge.chat_clear) bridge.chat_clear.connect(onChatClear);
                if (bridge.chat_partial) bridge.chat_partial.connect(onVoicePartial);
                if (bridge.chat_finished) bridge.chat_finished.connect(onChatFinished);
                else console.warn("bridge.chat_finished 不存在");
                if (bridge.chat_thinking) bridge.chat_thinking.connect(onChatThinking);
                if (bridge.chat_failed) bridge.chat_failed.connect(onChatFailed);
                if (bridge.service_updated) bridge.service_updated.connect(onServiceUpdated);
                if (bridge.tool_event) bridge.tool_event.connect(onToolEvent);
                if (bridge.log) bridge.log.connect(onLog);
                if (bridge.reminder_fired) {
                    bridge.reminder_fired.connect(showReminder);
                    bridge.reminder_fired.connect(loadHomeCards);  // 提醒触发后刷新首页任务卡
                }
                if (bridge.emotion_changed) bridge.emotion_changed.connect(setHeroEmotion);
                if (bridge.user_message) bridge.user_message.connect(onUserMessage);
                if (bridge.weather_updated) bridge.weather_updated.connect(onWeatherUpdated);
                if (bridge.warp_updated) bridge.warp_updated.connect(onWarpUpdated);
                if (bridge.bili_login_finished) bridge.bili_login_finished.connect(onBiliLoginFinished);
                if (bridge.image_message) bridge.image_message.connect(onImageMessage);
                if (bridge.system_updated) bridge.system_updated.connect(renderSystemStatus);
                console.log("[Console] 桥接已就绪");
                setConnStatus("已连接", true);
                refreshServices();
                loadHomeCards();
                loadWeather();
                scheduleWarpPoll();
                setupLogPanel();
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
                loadHomeCards();
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
            onChatStarted();
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
            // 逐字追加到气泡（与真实后端 chat_delta 行为一致）
            const full = '（mock）我收到了：' + t;
            let i = 0;
            const iv = setInterval(() => {
                if (i >= full.length) {
                    clearInterval(iv);
                    onChatFinished('');
                    return;
                }
                onChatDelta(full[i]);
                i += 1;
            }, 15);
        },
        send_image: (b64, name, text) => {
            onChatStarted();
            const full = '（mock）我看到你发的图了' + (text ? '：' + text : '。');
            let i = 0;
            const iv = setInterval(() => {
                if (i >= full.length) { clearInterval(iv); onChatFinished(''); return; }
                onChatDelta(full[i]);
                i += 1;
            }, 15);
        },
        replay_voice: () => { console.log("[mock] replay voice"); },
        copy_text: (t) => { try { navigator.clipboard.writeText(t); } catch (e) { } },
        get_tasks: () => wrap(JSON.stringify([
            { text: "整理终端灰度骨架", done: true, prio: "高" },
            { text: "确认首页与对话结构", done: false, prio: "高" },
            { text: "接入语音合成流式播放", done: false, prio: "中" },
        ])),
        get_services: () => wrap(JSON.stringify([
            { name: "warp", display: "Cloudflare WARP", state: "stopped" },
            { name: "gpt_sovits", display: "GPT-SoVITS 语音合成", state: "running" },
            { name: "napcat", display: "NapCat 消息桥接", state: "stopped" },
        ])),
        get_settings: () => wrap(JSON.stringify([
            { label: "角色形象", value: "达妮娅", type: "str" },
            { label: "默认缩放", value: "0.7x", type: "str" },
            { label: "语音合成", value: "GPT-SoVITS", type: "str" },
            { label: "LLM 模型", value: "qwen2.5:7b", type: "str" },
        ])),
        get_weather: () => wrap("{}"),
        get_system_status: () => wrap("{}"),
        get_warp_status: () => wrap("{}"),
        get_recent_logs: () => wrap("[]"),
        get_recent_traces: () => wrap("[]"),
        toggle_warp: () => { },
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
        emotion_changed: { connect: () => { } },
        weather_updated: { connect: () => { } },
        warp_updated: { connect: () => { } },
        system_updated: { connect: () => { } },
        user_message: { connect: () => { } },
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
    const petBtn = document.getElementById('pet-btn');
    if (!topbar || !minBtn || !maxBtn || !closeBtn) return;

    const call = (name) => { if (bridge && bridge[name]) try { bridge[name](); } catch (e) { } };

    minBtn.addEventListener('click', () => call('minimize_window'));
    maxBtn.addEventListener('click', () => call('toggle_max_window'));
    closeBtn.addEventListener('click', () => call('close_window'));
    if (petBtn) petBtn.addEventListener('click', () => call('show_pet'));

    // 最大化/还原图标同步
    const iconMax = maxBtn.querySelector('.icon-max');
    const iconRestore = maxBtn.querySelector('.icon-restore');
    function updateMaxIcon(maxed) {
        if (iconMax) iconMax.style.display = maxed ? 'none' : '';
        if (iconRestore) iconRestore.style.display = maxed ? '' : 'none';
        maxBtn.dataset.tip = maxed ? '还原' : '最大化';
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
        if (target === 'task') loadTasks();
        if (target === 'home') loadHomeCards();
        if (target === 'status') loadSystemStatus();
        if (target === 'launch') refreshServices();
        if (target === 'settings') loadSettings();
        if (target === 'collab') btn.classList.remove('tool-pulse');
    });
});

// 首页底部卡片点击跳转（复用导航切换逻辑，自动刷新数据）
document.querySelectorAll('.info-card[data-page]').forEach(card => {
    card.addEventListener('click', () => {
        const page = card.dataset.page;
        const navBtn = document.querySelector('.nav-btn[data-page="' + page + '"]');
        if (navBtn) navBtn.click();
    });
});

// ===== 首页协作卡片同步（展示协作页部分内容） =====
function syncCollabCard() {
    const title = document.getElementById('collab-card-title');
    const desc = document.getElementById('collab-card-desc');
    if (!title || !desc) return;
    const goalEl = document.getElementById('banner-goal');
    const stateEl = document.getElementById('ws-status-title');
    const goal = (goalEl && goalEl.textContent) || '';
    const state = (stateEl && stateEl.textContent) || '';
    title.textContent = (goal && goal !== '等待新任务…') ? goal : '暂无协作';
    desc.textContent = state || '空闲';
}

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
            '<div class="msg-body">' +
            '<div class="msg-bubble" id="bot-msg-latest"></div>' +
            '<div class="msg-actions">' +
            '<button class="msg-act act-voice" data-tip="播放语音">' + VOICE_ICON + '</button>' +
            '<button class="msg-act act-copy" data-tip="复制">' + COPY_ICON + '</button>' +
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

// 语音识别出的用户文本 → 回显为用户气泡（与键盘输入一致）
function onUserMessage(text) {
    text = (text || '').trim();
    if (text) appendMsg(text, 'user');
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

// 打字机队列：LLM 快吐 token 时不闪，固定 500ms/字匀速出现
let _typeQueue = "";
let _typeTimer = null;
let _streamEnded = false;

function onChatStarted() {
    chatText = "";
    _typeQueue = "";
    _streamEnded = false;
    _stallEl = null;
    if (_typeTimer) { clearTimeout(_typeTimer); _typeTimer = null; }
    const existing = document.getElementById('bot-msg-latest');
    if (existing) {
        // 重试场景：复用已有气泡并清零，避免新建第二个气泡
        existing.textContent = "";
    } else {
        appendMsg("", "bot");
    }
}

function _typePump() {
    const el = document.getElementById('bot-msg-latest');
    if (!el) { _typeQueue = ""; _typeTimer = null; return; }
    if (_typeQueue.length > 0) {
        chatText += _typeQueue[0];
        _typeQueue = _typeQueue.slice(1);
        el.textContent = chatText;
        const box = document.getElementById('chat-messages');
        box.scrollTop = box.scrollHeight;
        // 积压多时自动加速：每 20 字把间隔缩短一半，最低 50ms
        const backlog = _typeQueue.length;
        const interval = backlog > 60 ? 50 : backlog > 30 ? 100 : 200;
        _typeTimer = setTimeout(_typePump, interval);
    } else {
        // 队列消化完
        if (_streamEnded) {
            el.removeAttribute('id');
            const box = document.getElementById('chat-messages');
            if (box) box.scrollTop = box.scrollHeight;
        }
        _typeTimer = null;
    }
}

// 首字迟迟未到：先在气泡里占位回应，真实内容到达后清掉
function onChatStall(text) {
    if (chatText || _typeQueue.length > 0) return;
    const el = document.getElementById('bot-msg-latest');
    if (!el) return;
    el.textContent = text;
    _stallEl = el;
    const box = document.getElementById('chat-messages');
    if (box) box.scrollTop = box.scrollHeight;
}

function onChatClear() {
    if (!_stallEl) return;
    if (chatText || _typeQueue.length > 0) { _stallEl = null; return; }
    _stallEl.textContent = "";
    _stallEl = null;
}

function onChatDelta(delta) {
    // 真实内容到了：先清掉占位文字
    if (_stallEl) {
        _stallEl.textContent = "";
        _stallEl = null;
    }
    _typeQueue += delta;
    if (!_typeTimer) _typePump();
}

// 思考链：在机器人气泡上方插一个可折叠的「达妮娅想了想」块
function onChatThinking(thinking) {
    if (!thinking) return;
    const box = document.getElementById('chat-messages');
    if (!box) return;
    const el = document.getElementById('bot-msg-latest')
        || box.querySelector('.msg-bot:last-child .msg-bubble');
    if (!el) return;
    // 避免重复插入（重试场景）
    const msgWrap = el.closest('.msg-bot') || el.parentElement;
    if (msgWrap.querySelector('.thinking-block')) return;
    const detail = document.createElement('details');
    detail.className = 'thinking-block';
    detail.innerHTML =
        '<summary>🤔 达妮娅想了想</summary>' +
        '<div class="thinking-content"></div>';
    detail.querySelector('.thinking-content').textContent = thinking;
    el.before(detail);
    box.scrollTop = box.scrollHeight;
}

function onChatFinished(errText) {
    // 整轮都没等到真实内容：把占位语定格成一条正常消息
    if (_stallEl && !chatText && _typeQueue.length === 0) {
        _stallEl.removeAttribute('id');
        _stallEl = null;
    }
    // 不在这里清 timer / 倒出剩余队列——让 _typePump 继续逐字消化，
    // 保证打字机效果不因流结束而跳变。
    // errText 非空且从未收到任何 delta → 显示错误。
    if (_typeQueue.length === 0 && !chatText && errText) {
        // 完全没收到内容，直接显示错误
        const el = document.getElementById('bot-msg-latest');
        if (el) {
            el.textContent = errText;
            el.removeAttribute('id');
        }
        chatText = "";
        const box = document.getElementById('chat-messages');
        if (box) box.scrollTop = box.scrollHeight;
        return;
    }
    // 有内容（或队列还有剩余）：标记"定型"，pump 消化完后不再滚动
    _streamEnded = true;
    // 如果 pump 还没启动（极端情况：最后几个字符刚好在 pump 间隔内到达）
    if (!_typeTimer && _typeQueue.length > 0) {
        _typePump();
    }
}

function onChatFailed(err) {
    if (_typeTimer) { clearTimeout(_typeTimer); _typeTimer = null; }
    _typeQueue = "";
    _stallEl = null;
    const el = document.getElementById('bot-msg-latest');
    if (el) {
        el.textContent = "出错了… " + err;
        el.removeAttribute('id');
    }
    collabError();
}

// ===== 运行日志面板（启动页）=====
const _MAX_LOG_LINES = 500;

function _logLevelOf(t) {
    if (/❌|Traceback|失败|异常|超时|\berror\b/i.test(t)) return 'lv-err';
    if (/⚠️|警告|\bwarn\b/i.test(t)) return 'lv-warn';
    if (/✅|就绪|成功|已启动|已连接/i.test(t)) return 'lv-ok';
    return '';
}

function appendLogLine(text) {
    const box = document.getElementById('log-box');
    if (!box) return;
    const empty = box.querySelector('.log-empty');
    if (empty) empty.remove();
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const div = document.createElement('div');
    div.className = 'log-line ' + _logLevelOf(text);
    div.textContent = text;
    box.appendChild(div);
    while (box.children.length > _MAX_LOG_LINES) box.removeChild(box.firstChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
    const c = document.getElementById('log-count');
    if (c) c.textContent = box.children.length;
}

function onLog(text) {
    appendLogLine(text);
}

function loadRecentLogs() {
    if (!bridge || !bridge.get_recent_logs) return;
    Promise.resolve(bridge.get_recent_logs()).then(raw => {
        let list = [];
        try { list = JSON.parse(raw || "[]"); } catch (e) { return; }
        const box = document.getElementById('log-box');
        if (!box) return;
        box.innerHTML = '';
        list.slice(-_MAX_LOG_LINES).forEach(appendLogLine);
        const c = document.getElementById('log-count');
        if (c) c.textContent = box.children.length;
        box.scrollTop = box.scrollHeight;
    }).catch(() => {});
}

function setupLogPanel() {
    const btn = document.getElementById('log-clear');
    if (btn) btn.addEventListener('click', () => {
        const box = document.getElementById('log-box');
        if (!box) return;
        box.innerHTML = '<div class="log-empty">暂无日志</div>';
        const c = document.getElementById('log-count');
        if (c) c.textContent = '0';
    });
    loadRecentLogs();
    setupTracePanel();
}

// ===== 最近对话（启动页复盘：输入 / 直连工具 / 回复）=====
function _escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function loadTraces() {
    if (!bridge || !bridge.get_recent_traces) return;
    Promise.resolve(bridge.get_recent_traces()).then(raw => {
        let rows = [];
        try { rows = JSON.parse(raw || "[]"); } catch (e) { return; }
        const box = document.getElementById('trace-box');
        if (!box) return;
        if (!rows.length) {
            box.innerHTML = '<div class="log-empty">暂无对话记录</div>';
        } else {
            box.innerHTML = rows.map(r => {
                const tool = r.tool
                    ? `<span class="trace-tool">⚙ ${_escHtml(r.tool)}</span>`
                    : '<span class="trace-tool trace-none">模型决策</span>';
                return `<div class="trace-line">` +
                    `<span class="trace-time">${_escHtml(r.ts)}</span>` +
                    `<span class="trace-chat">${_escHtml(r.chat)}</span>` +
                    `<span class="trace-text">${_escHtml(r.text)}</span>${tool}` +
                    `<span class="trace-reply">→ ${_escHtml(r.reply)}</span></div>`;
            }).join('');
        }
        const c = document.getElementById('trace-count');
        if (c) c.textContent = rows.length;
        box.scrollTop = box.scrollHeight;
    }).catch(() => {});
}

function setupTracePanel() {
    const btn = document.getElementById('trace-refresh');
    if (btn) btn.addEventListener('click', loadTraces);
    loadTraces();
}

// 流式语音识别：边说边把文字写进输入框；结束（空串）时还原用户原本的草稿
let _voicePartialDraft = null;
function onVoicePartial(text) {
    const inputEl = document.getElementById('chat-input');
    if (!inputEl) return;
    if (text) {
        if (_voicePartialDraft === null) _voicePartialDraft = inputEl.value;
        inputEl.value = text;
        inputEl.classList.add('voice-live');
    } else {
        inputEl.classList.remove('voice-live');
        if (_voicePartialDraft !== null) {
            inputEl.value = _voicePartialDraft;
            _voicePartialDraft = null;
        }
    }
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

// ===== 发图入口（＋选择文件 / Ctrl+V 粘贴）=====
// 前端先压到 ≤1400px 的 JPEG，base64 交给后端视觉模型：原图直传，不做文字描述中转
function compressImage(file, maxSide = 1400, quality = 0.85) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
            URL.revokeObjectURL(url);
            const scale = Math.min(1, maxSide / Math.max(img.width || 1, img.height || 1));
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(img.width * scale));
            canvas.height = Math.max(1, Math.round(img.height * scale));
            canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
            resolve(canvas.toDataURL('image/jpeg', quality));
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('无法读取图片')); };
        img.src = url;
    });
}

// 用户气泡：图片缩略图 + 随图输入的文字
function appendImageMsg(dataUrl, text) {
    const box = document.getElementById('chat-messages');
    if (!box) return;
    const msg = document.createElement('div');
    msg.className = 'msg msg-user';
    msg.innerHTML =
        '<div class="msg-bubble msg-bubble-img">' +
        '<img class="msg-img" alt="图片" />' +
        '<div class="msg-img-caption"></div></div>';
    msg.querySelector('.msg-img').src = dataUrl;
    if (text) msg.querySelector('.msg-img-caption').textContent = text;
    box.appendChild(msg);
    box.scrollTop = box.scrollHeight;
}

function sendImageFile(file) {
    if (!file || !bridge || !bridge.send_image) return;
    const prompt = input1.value.trim();
    compressImage(file).then(dataUrl => {
        const b64 = dataUrl.split(',')[1] || '';
        if (!b64) return;
        appendImageMsg(dataUrl, prompt);
        input1.value = '';
        collabBegin(prompt || '看这张图');
        bridge.send_image(b64, file.name || 'pasted.jpg', prompt);
    }).catch(() => onLog('[图片] 这张图读不出来，换一张试试'));
}

const attachBtn = document.getElementById('attach-btn');
const imageFileInput = document.getElementById('image-file');
if (attachBtn && imageFileInput) {
    attachBtn.addEventListener('click', () => imageFileInput.click());
    imageFileInput.addEventListener('change', () => {
        const f = imageFileInput.files && imageFileInput.files[0];
        imageFileInput.value = '';
        if (f) sendImageFile(f);
    });
}

// 粘贴剪贴板图片（只在首页对话页生效，避免在设置页误触）
document.addEventListener('paste', (e) => {
    const homePage = document.getElementById('page-home');
    if (!homePage || !homePage.classList.contains('active')) return;
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const item of items) {
        if (item.type && item.type.startsWith('image/')) {
            const f = item.getAsFile();
            if (f) { e.preventDefault(); sendImageFile(f); }
            return;
        }
    }
});

// 静态欢迎气泡的按钮绑定
document.querySelectorAll('#chat-messages .msg-bot').forEach(bindBotActions);

// ===== 协作流 =====
const TOOL_DISPLAY = {
    open_app: '启动本机应用',
    query_time: '查询时间',
    query_weather: '查询天气',
    set_reminder: '设置提醒',
    screen_context: '屏幕理解',
    web_search: '正在查资料…',
    browse_url: '正在看这个页面…',
    image_search: '正在找图…',
    bilibili_feed: '正在看B站动态…',
    system_sense: '正在看系统状态…',
    model_detect: '正在看本地模型…',
    list_dir: '正在看文件夹…',
    read_file: '正在读文件…',
    write_file: '正在写文件…',
    organize_folder: '正在整理文件夹…',
    set_volume: '正在调音量…',
    set_brightness: '正在调亮度…',
    read_clipboard: '正在看剪贴板…',
    write_clipboard: '正在复制…',
    send_message: '正在发消息…',
};

let collabThinkingEl = null;
let collabOpenCards = [];   // 多步执行：同时可能有多个"正在执行"的卡片
let collabHasTool = false;

function toolDisplayName(ev) {
    return TOOL_DISPLAY[ev.name] || ev.description || ev.name;
}

// 取出并移除某个工具最近一张"正在执行"的卡片（多步执行时每步一张，互不覆盖）
function takeOpenCard(name) {
    for (let i = collabOpenCards.length - 1; i >= 0; i--) {
        if (collabOpenCards[i].name === name) {
            const el = collabOpenCards[i].el;
            collabOpenCards.splice(i, 1);
            return el;
        }
    }
    return null;
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

// ===== 情绪驱动立绘 =====
// 细腻情绪 → 头像图片映射（复用现有 5 张立绘资产）
const EMOTION_IMG = {
    happy: '../assets/dania_happy.png', love: '../assets/dania_happy.png', shy: '../assets/dania_happy.png',
    angry: '../assets/dania_idle.png', sad: '../assets/dania_idle.png',
    surprised: '../assets/dania_thinking.png', thinking: '../assets/dania_thinking.png',
    sleepy: '../assets/dania_sleeping.png', tired: '../assets/dania_sleeping.png', sleeping: '../assets/dania_sleeping.png',
    speaking: '../assets/dania_speaking.png',
    idle: '../assets/dania_idle.png',
};
// 细腻情绪 → CSS 动画类（同类情绪共用动画）
const EMOTION_ANIM = {
    happy: 'emotion-happy', love: 'emotion-happy', shy: 'emotion-happy',
    angry: 'emotion-angry',
    sad: 'emotion-sad',
    surprised: 'emotion-surprised',
    thinking: 'emotion-thinking',
    sleepy: 'emotion-sleepy', tired: 'emotion-sleepy', sleeping: 'emotion-sleepy',
    speaking: 'emotion-speaking',
};

function setHeroEmotion(state) {
    state = (state || 'idle').toLowerCase();
    const avatar = document.getElementById('hero-avatar');
    if (!avatar) return;
    const img = EMOTION_IMG[state] || EMOTION_IMG.idle;
    if (avatar.getAttribute('src') !== img) avatar.setAttribute('src', img);
    const wrapEl = avatar.closest('.avatar-wrap');
    if (wrapEl) {
        wrapEl.className = wrapEl.className.replace(/\s*emotion-\S+/g, '').trim();
        const anim = EMOTION_ANIM[state];
        if (anim) wrapEl.classList.add(anim);
    }
}

// ===== 顶栏天气胶囊 =====
let _wTip = null;
function _ensureWeatherTip() {
    if (_wTip) return _wTip;
    _wTip = document.createElement('div');
    _wTip.id = 'weather-tip';
    _wTip.className = 'weather-tip-float';
    document.body.appendChild(_wTip);
    const chip = document.getElementById('weather-chip');
    if (chip) {
        chip.addEventListener('mouseenter', () => {
            if (!chip.classList.contains('ready') || !_wTip.textContent) return;
            const r = chip.getBoundingClientRect();
            _wTip.style.top = (r.bottom + 8) + 'px';
            _wTip.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
            _wTip.style.display = 'block';
        });
        chip.addEventListener('mouseleave', () => { _wTip.style.display = 'none'; });
    }
    return _wTip;
}

function renderWeather(d) {
    const el = document.getElementById('weather-chip');
    if (!el || !d || d.temp === undefined || d.temp === null) return;
    el.querySelector('.w-icon').textContent = d.icon || '🌡';
    el.querySelector('.w-temp').textContent = d.temp + '°';
    el.querySelector('.w-desc').textContent = d.desc || '';
    _ensureWeatherTip().textContent = (d.city ? d.city + ' · ' : '') +
        `体感 ${d.feels}° · 湿度 ${d.humidity}% · 风速 ${d.wind}km/h`;
    el.classList.add('ready');
}

function onWeatherUpdated(raw) {
    try { renderWeather(JSON.parse(raw)); } catch (e) { }
}

function loadWeather() {
    if (!bridge || !bridge.get_weather) return;
    try {
        Promise.resolve(bridge.get_weather()).then(raw => {
            try {
                const d = JSON.parse(raw);
                if (d && d.temp !== undefined) renderWeather(d);
            } catch (e) { }
        }).catch(() => { });
    } catch (e) { }
}

// ===== 通用自绘提示框 =====
// QWebEngine 的原生 title 提示框是空黑框，所以全部改成 data-tip + 自绘浮层
let _tipBox = null;

function _tipHide() {
    if (_tipBox) _tipBox.style.display = 'none';
}

function _tipShow(target) {
    const text = target.getAttribute('data-tip');
    if (!text) return;
    if (!_tipBox) {
        _tipBox = document.createElement('div');
        _tipBox.className = 'weather-tip-float';
        document.body.appendChild(_tipBox);
    }
    _tipBox.textContent = text;
    _tipBox.style.display = 'block';
    const r = target.getBoundingClientRect();
    const w = _tipBox.offsetWidth || 80;
    let left = r.left + r.width / 2 - w / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    _tipBox.style.top = (r.bottom + 8) + 'px';
    _tipBox.style.left = left + 'px';
    _tipBox.style.right = 'auto';
}

document.addEventListener('mouseover', (e) => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (t) _tipShow(t);
});
document.addEventListener('mouseout', (e) => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (t && (!e.relatedTarget || !t.contains(e.relatedTarget))) _tipHide();
});
window.addEventListener('blur', _tipHide);

// ===== Cloudflare WARP（启动页服务行）=====
let _warpTimer = null;

// 启动页「Cloudflare WARP」行：12s 轮询刷新状态（WARP 连接/断开走 start_service/stop_service）
function scheduleWarpPoll() {
    if (_warpTimer) return;
    _warpTimer = setInterval(() => {
        if (bridge && bridge.get_services) refreshServices();
    }, 12000);
}

// WARP 状态更新回推：刷新启动页服务行
function onWarpUpdated() {
    if (bridge && bridge.get_services) refreshServices();
}

// ===== 电脑状态页 =====
function _barCls(p) { return p >= 85 ? ' hot' : (p >= 60 ? ' warn' : ''); }

function _fmtKb(kb) {
    if (kb >= 1024 * 1024) return (kb / 1024 / 1024).toFixed(1) + ' MB/s';
    if (kb >= 1024) return (kb / 1024).toFixed(1) + ' MB/s';
    return kb.toFixed(0) + ' KB/s';
}

function renderSystemStatus(raw) {
    let d;
    try { d = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch (e) { return; }
    if (!d || !d.cpu) return;
    const $ = (id) => document.getElementById(id);
    if (!$('cpu-percent')) return;

    // CPU
    const cpuP = Math.round(d.cpu.percent || 0);
    $('cpu-percent').textContent = cpuP;
    $('cpu-bar').style.width = cpuP + '%';
    $('cpu-bar').className = 'st-bar-fill' + _barCls(cpuP);
    $('cpu-freq').textContent = d.cpu.freq_mhz ? (d.cpu.freq_mhz / 1000).toFixed(1) + ' GHz · ' + d.cpu.count + ' 线程' : '';
    const coresEl = $('cpu-cores');
    const cores = d.cpu.cores || [];
    if (coresEl.childElementCount !== cores.length) {
        coresEl.innerHTML = cores.map(() => '<div class="core-bar"><div class="core-fill"></div></div>').join('');
    }
    coresEl.querySelectorAll('.core-fill').forEach((el, i) => {
        const p = Math.round(cores[i] || 0);
        el.style.height = p + '%';
        el.className = 'core-fill' + _barCls(p).trim();
    });

    // 内存
    const mem = d.memory || {};
    const memP = Math.round(mem.percent || 0);
    $('mem-percent').textContent = memP;
    $('mem-bar').style.width = memP + '%';
    $('mem-bar').className = 'st-bar-fill' + _barCls(memP);
    $('mem-detail').textContent = (mem.used || 0) + ' / ' + (mem.total || 0) + ' GB';
    $('swap-detail').textContent = mem.swap_total ?
        `虚拟内存 ${mem.swap_used} / ${mem.swap_total} GB（${Math.round(mem.swap_percent)}%）` : '';

    // 网络
    const net = d.net || {};
    $('net-up').textContent = _fmtKb(net.up_kb || 0);
    $('net-down').textContent = _fmtKb(net.down_kb || 0);
    $('net-total').textContent = `累计发送 ${net.total_sent || 0} GB · 接收 ${net.total_recv || 0} GB`;

    // 系统
    $('sys-uptime').textContent = d.uptime || '--';
    $('sys-procs').textContent = d.processes || '--';
    if (d.battery) {
        $('sys-battery-row').style.display = '';
        $('sys-battery').textContent = Math.round(d.battery.percent) + '%' +
            (d.battery.plugged ? '（充电中）' : '');
    } else {
        $('sys-battery-row').style.display = 'none';
    }

    // 磁盘
    const dl = $('disk-list');
    dl.innerHTML = (d.disks || []).map(k => {
        const p = Math.round(k.percent || 0);
        return '<div class="disk-row">' +
            '<span class="disk-mount">' + k.mount + '</span>' +
            '<div class="st-bar disk-bar"><div class="st-bar-fill' + _barCls(p) + '" style="width:' + p + '%"></div></div>' +
            '<span class="disk-detail">' + k.used + ' / ' + k.total + ' GB</span>' +
            '</div>';
    }).join('') || '<div class="st-sub">未检测到磁盘分区</div>';
}

function loadSystemStatus() {
    if (!bridge || !bridge.get_system_status) return;
    try {
        Promise.resolve(bridge.get_system_status()).then(renderSystemStatus).catch(() => { });
    } catch (e) { }
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
    syncCollabCard();
}

function collabBegin(goal) {
    collabHasTool = false;
    collabOpenCards = [];
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

        const card = document.createElement('div');
        card.className = 'flow-item working';
        card.innerHTML =
            '<div class="flow-tag">已允许，正在执行</div>' +
            '<div class="flow-name"><span class="flow-spinner"></span>达妮娅准备调用：' + displayName + '</div>' +
            '<div class="flow-state">正在执行…</div>' +
            '<div class="flow-log"></div>';
        card.querySelector('.flow-log').textContent = ev.description || displayName;
        list.appendChild(card);
        collabOpenCards.push({ name: ev.name, el: card });
        list.scrollTop = list.scrollHeight;
    } else if (ev.type === 'tool_finish') {
        collabHasTool = true;
        const logText = cleanToolLog(ev.result) || '工具已完成';
        const doneCard = takeOpenCard(ev.name);
        if (doneCard) {
            doneCard.className = 'flow-item done';
            doneCard.innerHTML =
                '<div class="flow-tag">工具操作</div>' +
                '<div class="flow-name"><span class="flow-check">✓</span>' + displayName + '</div>' +
                '<div class="flow-state">已完成</div>' +
                '<div class="flow-log"></div>';
            doneCard.querySelector('.flow-log').textContent =
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
        // 失败时把对应的"正在执行"卡片摘掉，避免留下永远转圈的卡片
        takeOpenCard(ev.name);
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

// ===== 首页信息卡片（真实任务） =====
function loadHomeCards() {
    if (!bridge || !bridge.get_tasks) return;
    bridge.get_tasks().then(raw => {
        try {
            const list = JSON.parse(raw);
            const real = (Array.isArray(list) ? list : []).filter(t => t.text && t.text !== '暂无提醒任务');
            const tEl = document.getElementById('task-title');
            const dEl = document.getElementById('task-desc');
            if (!tEl || !dEl) return;
            if (real.length > 0) {
                tEl.textContent = real[0].text;
                dEl.textContent = real.length > 1 ? `还有 ${real.length - 1} 个进行中的提醒` : '到点会提醒你';
            } else {
                tEl.textContent = '暂无任务';
                dEl.textContent = '让达妮娅帮你记一件事吧';
            }
        } catch (e) { /* 忽略解析失败 */ }
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

// ===== 达妮娅发来的图片（page_image / image_search 等工具，桌面端无 QQ 会话时）=====
function onImageMessage(raw) {
    let data = {};
    try { data = JSON.parse(raw || '{}'); } catch (e) { return; }
    const imgs = data.images || [];
    if (!imgs.length) return;
    const box = document.getElementById('chat-messages');
    if (!box) return;
    const msg = document.createElement('div');
    msg.className = 'msg msg-bot';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble msg-bubble-img';
    imgs.forEach(it => {
        if (!it || !it.src) return;
        const img = document.createElement('img');
        img.className = 'msg-img';
        img.alt = it.name || '图片';
        img.src = it.src;
        bubble.appendChild(img);
    });
    if (!bubble.children.length) return;
    msg.appendChild(bubble);
    box.appendChild(msg);
    box.scrollTop = box.scrollHeight;
}

// ===== 设置页 =====
function onBiliLoginFinished(raw) {
    // 扫码登录结束：结果写进日志面板，并刷新设置行状态
    let data = {};
    try { data = JSON.parse(raw); } catch (e) { data = { text: String(raw) }; }
    if (typeof onLog === 'function') onLog(`[B站登录] ${data.text || raw}`);
    loadSettings();
}

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
            } else if (s.type === 'model_select' && s.options) {
                let opts = s.options.map(o =>
                    `<option value="${o.value}" ${o.value === s.value ? 'selected' : ''}>${o.label}</option>`
                ).join('');
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <select class="model-select" data-key="${s.key}">${opts}</select>`;
            } else if (s.type === 'str' && s.key) {
                const inputVal = s.value ? s.value.replace(/"/g, '&quot;') : '';
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <input type="text" class="setting-input" data-key="${s.key}" data-placeholder="${s.placeholder ? s.placeholder.replace(/"/g, '&quot;') : ''}" value="${inputVal}" placeholder="${s.placeholder || ''}" />`;
            } else if (s.type === 'hint') {
                row.classList.add('setting-hint-row');
                row.innerHTML = `<span class="set-hint">${s.label}</span>`;
            } else if (s.type === 'action') {
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <span class="set-value">${s.value || ''}</span>
                    <button class="btn-mini" data-action="${s.key}">${s.button || '执行'}</button>`;
            } else {
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <span class="set-value">${s.value}</span>`;
            }
            container.appendChild(row);
        });
        // bool 开关
        container.querySelectorAll('.switch input').forEach(cb => {
            cb.addEventListener('change', () => {
                bridge.save_setting(cb.dataset.key, cb.checked ? 'true' : 'false');
            });
        });
        // 模型下拉框（切换后重新拉取设置，因为配置项条目会随模型类型变化）
        container.querySelectorAll('.model-select').forEach(sel => {
            sel.addEventListener('change', () => {
                bridge.save_setting(sel.dataset.key, sel.value);
                // 延迟重渲染，避免 input 焦点状态丢失
                setTimeout(() => loadSettings(), 600);
            });
        });
        // 文本输入框（带 key 的 str 类型）
        container.querySelectorAll('.setting-input').forEach(inp => {
            inp.addEventListener('change', () => {
                bridge.save_setting(inp.dataset.key, inp.value);
            });
            inp.addEventListener('blur', () => {
                bridge.save_setting(inp.dataset.key, inp.value);
            });
        });
        // 操作按钮（扫码登录等）
        container.querySelectorAll('.btn-mini').forEach(btn => {
            btn.addEventListener('click', () => {
                if (btn.dataset.action === 'bilibili_login' && bridge.start_bilibili_login) {
                    btn.disabled = true;
                    btn.textContent = '等待扫码…';
                    bridge.start_bilibili_login();
                }
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
