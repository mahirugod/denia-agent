// ===== 达妮娅终端 · 前端逻辑（对接 Python 后端）=====

let bridge = null;      // Python 桥接对象
let chatText = "";      // 当前流式回复累积文本
let _stallEl = null;    // "先应答"占位气泡（首字未到时显示）

// 连接状态：顶栏唯一的状态组件（在线 / 重连中 / 离线 + 最近检查时间）
const CONN_LABEL = { online: '在线', reconnecting: '重连中', offline: '离线' };
let _connDetail = '';

function setConnStatus(state, detail) {
    const pill = document.getElementById('conn-pill');
    if (!pill) return;
    if (detail !== undefined) _connDetail = detail;
    const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    pill.dataset.state = state;
    pill.dataset.tip = (_connDetail ? _connDetail + ' · ' : '') + '最近检查 ' + stamp;
    const text = document.getElementById('conn-text');
    if (text) text.textContent = CONN_LABEL[state] || state;
}

// 初始化 QWebChannel（带重试，确保 qt.webChannelTransport 就绪）
function initBridge() {
    if (typeof QWebChannel === "undefined") {
        console.warn("qwebchannel.js 未加载，运行在 mock 模式");
        setConnStatus("offline", "浏览器预览模式，未连接本地服务");
        setupMockBridge();
        return;
    }
    setConnStatus("reconnecting", "正在建立本地连接");
    let tries = 0;
    function tryConnect() {
        if (typeof qt !== "undefined" && qt.webChannelTransport) {
            new QWebChannel(qt.webChannelTransport, function (channel) {
                bridge = channel.objects.bridge;
                if (!bridge) {
                    console.error("QWebChannel 连接成功但 bridge 对象为空");
                    setConnStatus("offline", "桥接对象为空");
                    return;
                }
                // 调试：打印 bridge 暴露的属性
                console.log("bridge keys:", Object.keys(bridge));
                // 连接 Python → JS 信号（逐个检查，避免 undefined 报错）
                if (bridge.chat_started) bridge.chat_started.connect(onChatStarted);
                else console.warn("bridge.chat_started 不存在");
                if (bridge.chat_delta) bridge.chat_delta.connect(onChatDelta);
                else console.warn("bridge.chat_delta 不存在");
                // 语速同步：后端按每句音频时长/字数算出每字毫秒，打字机跟着语音走
                if (bridge.voice_rhythm) bridge.voice_rhythm.connect(function (ms) {
                    const v = Number(ms);
                    if (isFinite(v) && v >= 40 && v <= 400) _typeInterval = v;
                });
                if (bridge.voice_segment) bridge.voice_segment.connect(onVoiceSegment);
                if (bridge.voice_stream_end) bridge.voice_stream_end.connect(onVoiceStreamEnd);
                if (bridge.voice_replay_state) bridge.voice_replay_state.connect(onVoiceReplayState);
                if (bridge.chat_stall) bridge.chat_stall.connect(onChatStall);
                if (bridge.chat_clear) bridge.chat_clear.connect(onChatClear);
                if (bridge.chat_partial) bridge.chat_partial.connect(onVoicePartial);
                if (bridge.chat_finished) bridge.chat_finished.connect(onChatFinished);
                else console.warn("bridge.chat_finished 不存在");
                if (bridge.chat_final) bridge.chat_final.connect(onChatFinal);
                if (bridge.chat_thinking) bridge.chat_thinking.connect(onChatThinking);
                if (bridge.chat_failed) bridge.chat_failed.connect(onChatFailed);
                if (bridge.service_updated) bridge.service_updated.connect(onServiceUpdated);
                if (bridge.tool_event) bridge.tool_event.connect(onToolEvent);
                if (bridge.log) bridge.log.connect(onLog);
                if (bridge.reminder_fired) {
                    bridge.reminder_fired.connect(showReminder);
                }
                if (bridge.emotion_changed) bridge.emotion_changed.connect(setHeroEmotion);
                if (bridge.user_message) bridge.user_message.connect(onUserMessage);
                if (bridge.weather_updated) bridge.weather_updated.connect(onWeatherUpdated);
                if (bridge.warp_updated) bridge.warp_updated.connect(onWarpUpdated);
                if (bridge.bili_login_finished) bridge.bili_login_finished.connect(onBiliLoginFinished);
                if (bridge.image_message) bridge.image_message.connect(onImageMessage);
                if (bridge.system_updated) bridge.system_updated.connect(renderSystemStatus);
                console.log("[Console] 桥接已就绪");
                setConnStatus("online", "本地服务已就绪");
                refreshServices();
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
                setConnStatus("offline", "5 秒内未检测到本地服务");
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
            { idx: 0, text: "整理终端灰度骨架", done: true, prio: "高" },
            { idx: 1, text: "确认首页与对话结构", done: false, prio: "高" },
            { idx: 2, text: "接入语音合成流式播放", done: false, prio: "中" },
        ])),
        complete_task: () => { },
        get_memories: () => wrap(JSON.stringify({
            entities: [{ key: "我的称呼", value: "小林" }, { key: "我在玩", value: "鸣潮" }],
            loops: [{ id: "m1", text: "面试", due: "11-13 10:00" }],
            episodes: [
                { id: "m2", date: "2026-11-12", kind: "event", text: "用户今天加班到很晚", importance: 3 },
                { id: "m3", date: "2026-11-12", kind: "diary", text: "今天他提到：面试；加班。就这样安静地过了一天，也还好。", importance: 5 },
            ],
            stats: { total: 2, loops: 1, entities: 2 },
        })),
        delete_memory: () => wrap(true),
        get_services: () => wrap(JSON.stringify([
            { name: "warp", display: "Cloudflare WARP", state: "stopped" },
            { name: "gpt_sovits", display: "GPT-SoVITS 语音合成", state: "running" },
            { name: "napcat", display: "NapCat 消息桥接", state: "stopped" },
        ])),
        get_settings: () => wrap(JSON.stringify([
            { label: "角色形象", value: "达妮娅", type: "readonly", group: "身份" },
            { label: "默认缩放", value: "0.7x", type: "readonly", group: "外观" },
            { label: "语音合成", value: "GPT-SoVITS", type: "readonly", group: "语音" },
            { label: "LLM 模型", key: "model_name", value: "qwen2.5:7b", type: "model_select", group: "模型", options: [
                { label: "qwen2.5:7b（本地）", value: "qwen2.5:7b" },
                { label: "DeepSeek V4（云端）", value: "deepseek" },
            ] },
            { label: "认知档位", key: "cognition_tier", value: "standard", type: "preset_select", group: "认知", options: [
                { label: "省电 · 预算 8k / 召回 2 条 / 每 4 条压缩", value: "saver" },
                { label: "标准 · 预算 14k / 召回 4 条 / 每 6 条压缩（默认）", value: "standard" },
                { label: "深度 · 预算 24k / 召回 7 条 / 每 8 条压缩", value: "deep" },
            ] },
            { label: "启用 NapCat（QQ 消息转发）", key: "napcat", value: false, type: "bool", group: "运行" },
            { label: "启动时自动检测并补齐服务", key: "autostart", value: true, type: "bool", group: "运行" },
        ])),
        get_weather: () => wrap("{}"),
        get_system_status: () => wrap("{}"),
        get_warp_status: () => wrap("{}"),
        get_recent_logs: () => wrap("[]"),
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
        if (target === 'memory') loadMemories();
        if (target === 'status') loadSystemStatus();
        if (target === 'launch') refreshServices();
        if (target === 'settings') loadSettings();
        if (target === 'collab') btn.classList.remove('tool-pulse');
    });
});

// 状态页行内重试
const statusRetryBtn = document.getElementById('status-retry');
if (statusRetryBtn) statusRetryBtn.addEventListener('click', retrySystemStatus);

// 首页底部卡片点击跳转（复用导航切换逻辑，自动刷新数据）
document.querySelectorAll('.info-card[data-page]').forEach(card => {
    card.addEventListener('click', () => {
        const page = card.dataset.page;
        const navBtn = document.querySelector('.nav-btn[data-page="' + page + '"]');
        if (navBtn) navBtn.click();
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
const STOP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>';

// 气泡语音重播：同一时刻只允许一条在播，点第二次即打断
let _voiceBtn = null;
let _voiceTimer = null;

function setVoicePlaying(btn) {
    if (_voiceBtn === btn) return;
    if (_voiceBtn) {
        _voiceBtn.classList.remove('playing');
        _voiceBtn.innerHTML = VOICE_ICON;
        _voiceBtn.setAttribute('aria-label', '重播这句话');
        _voiceBtn.setAttribute('data-tip', '重播这句话');
    }
    clearTimeout(_voiceTimer);
    _voiceTimer = null;
    _voiceBtn = btn;
    if (btn) {
        btn.classList.add('playing');
        btn.innerHTML = STOP_ICON;
        btn.setAttribute('aria-label', '停止播放');
        btn.setAttribute('data-tip', '停止播放');
        // 兜底：后端没回报状态时，30 秒后自动复位
        _voiceTimer = setTimeout(() => setVoicePlaying(null), 30000);
    }
}

function onVoiceReplayState(playing) {
    if (!playing) {
        setVoicePlaying(null);
        return;
    }
    if (_voiceBtn) {
        clearTimeout(_voiceTimer);
        _voiceTimer = setTimeout(() => setVoicePlaying(null), 30000);
    }
}

// 为一条 bot 消息绑定语音重播 / 复制按钮
function bindBotActions(msgEl) {
    const bubble = msgEl.querySelector('.msg-bubble');
    const voiceBtn = msgEl.querySelector('.act-voice');
    const copyBtn = msgEl.querySelector('.act-copy');
    if (voiceBtn) {
        voiceBtn.addEventListener('click', () => {
            const t = (bubble.dataset.raw || bubble.textContent).trim();
            if (!t || !bridge) return;
            // 已在播这条 → 停止；播别的 → 先打断再切过去
            if (_voiceBtn === voiceBtn) {
                try { if (bridge.interrupt_voice) bridge.interrupt_voice(); } catch (e) { }
                setVoicePlaying(null);
                return;
            }
            if (_voiceBtn) {
                try { if (bridge.interrupt_voice) bridge.interrupt_voice(); } catch (e) { }
            }
            setVoicePlaying(voiceBtn);
            if (bridge.replay_voice) bridge.replay_voice(t);
        });
    }
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            const t = (bubble.dataset.raw || bubble.textContent).trim();
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

// 对话一开始就把首页大立绘收成小头像，消息区接管剩余高度
function compactHero() {
    const hero = document.getElementById('home-hero');
    if (hero) hero.classList.add('compact');
}

// ===== 气泡富文本：轻量 Markdown（先转义，再渲染行内样式 / 列表 / 段落）=====
// 流式半成品（未闭合的反引号、星号）按纯文本显示，不会吞字也不会报错。
let _bubbleSrcLen = 0;   // 气泡当前显示内容的原文长度（渲染后 textContent 不含标记，不能当游标用）

function _escapeHTML(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _inlineMarkdown(s) {
    return s
        .replace(/`([^`\n]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
        // 内心独白：（…）只显示不朗读，弱化处理
        .replace(/（([^（）\n]{1,24})）/g, '<i class="inner-os">（$1）</i>')
        .replace(/\(([^()\n]{1,24})\)/g, '<i class="inner-os">($1)</i>');
}

function renderRichText(text) {
    const lines = _escapeHTML(text || '').split('\n');
    let html = '';
    let list = null;   // 'ul' | 'ol'
    let para = [];
    const flushPara = () => {
        if (!para.length) return;
        html += '<p>' + _inlineMarkdown(para.join('<br>')) + '</p>';
        para = [];
    };
    const flushList = () => {
        if (!list) return;
        html += '</' + list + '>';
        list = null;
    };
    for (const line of lines) {
        const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
        const num = line.match(/^\s*\d+[.)]\s+(.*)$/);
        if (bullet || num) {
            flushPara();
            const tag = bullet ? 'ul' : 'ol';
            if (list !== tag) {
                flushList();
                html += '<' + tag + '>';
                list = tag;
            }
            html += '<li>' + _inlineMarkdown((bullet || num)[1]) + '</li>';
        } else if (!line.trim()) {
            flushPara();
            flushList();
        } else {
            flushList();
            para.push(line);
        }
    }
    flushPara();
    flushList();
    return html;
}

// 写入达妮娅气泡：同时记住原文，复制/重播用原文而不是渲染后的残留标记
// track=false：这是旁路消息（提醒等），不影响当前流式气泡的语音游标
function setBubbleText(el, text, track) {
    if (!el) return;
    text = text || '';
    if (track !== false) _bubbleSrcLen = text.length;
    el.dataset.raw = text;
    el.innerHTML = renderRichText(text);
}

// 用户往上翻看历史时不要抢滚动条：只有本来就在底部才贴着新内容走
function _isNearBottom(box) {
    return !box || (box.scrollHeight - box.scrollTop - box.clientHeight) < 80;
}

function _pinBottom(box, wasNearBottom) {
    if (box && wasNearBottom) box.scrollTop = box.scrollHeight;
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
            '<button class="msg-act act-voice" data-tip="重播这句话" aria-label="重播这句话">' + VOICE_ICON + '</button>' +
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
    compactHero();
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
        setBubbleText(el, text, false);
        if (el.id) el.removeAttribute('id');  // 不占用流式气泡标记
    }
    box.scrollTop = box.scrollHeight;
}

// 打字机队列：默认 80ms/字；有语音时由 voice_rhythm 按实际语速实时校正
let _typeQueue = "";
let _typeTimer = null;
let _typeInterval = 80;
let _streamEnded = false;
let _finalText = null;      // 后端最终清洗文本：流结束后用来兜底替换气泡
let _chatBubbleEl = null;   // 本轮气泡元素（id 摘掉后仍能拿到）

// 段内时钟：voice_segment 到达后，本段文字按音频时长逐字揭示，段边界强制对齐
let _voiceSync = false;     // 是否已进入「字跟声音」模式
let _voiceBase = "";        // 已播完的段落文本
let _curSegText = "";       // 当前正在播的段落
let _curSegDur = 0;         // 当前段音频时长（ms）
let _curSegT0 = 0;          // 当前段开播时刻（performance.now）
let _segStartMs = [];       // 本段每个字的起始偏移（标点加权重，句内停顿更准）
let _segRaf = null;
let _voiceEnded = false;
let _voiceFinalized = false;

function onChatStarted() {
    compactHero();
    setVoicePlaying(null);
    const existing = document.getElementById('bot-msg-latest');
    const prevText = chatText;
    const prevQueue = _typeQueue;
    if (existing && (prevText || prevQueue)) {
        // 上一轮还在打字就被新消息打断：剩余文字直接落回旧气泡并定稿
        setBubbleText(existing, _finalText || (prevText + prevQueue), false);
        existing.removeAttribute('id');
    }
    _bubbleSrcLen = 0;
    chatText = "";
    _typeQueue = "";
    _typeInterval = 80;
    _streamEnded = false;
    _stallEl = null;
    _finalText = null;
    _voiceSync = false;
    _voiceBase = "";
    _curSegText = "";
    _curSegDur = 0;
    _voiceEnded = false;
    _voiceFinalized = false;
    if (_segRaf) { cancelAnimationFrame(_segRaf); _segRaf = null; }
    if (_typeTimer) { clearTimeout(_typeTimer); _typeTimer = null; }
    if (existing && !prevText && !prevQueue) {
        // 同一轮的重复 started：复用空气泡，避免出现两个空气泡
        setBubbleText(existing, "");
        _chatBubbleEl = existing;
    } else {
        appendMsg("", "bot");
        _chatBubbleEl = document.getElementById('bot-msg-latest');
    }
}

function _typePump() {
    const el = document.getElementById('bot-msg-latest') || _chatBubbleEl;
    if (!el) { _typeQueue = ""; _typeTimer = null; return; }

    // prefers-reduced-motion：一次性倒完所有剩余字符，不做逐字蹦
    if (matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) {
        if (_typeQueue.length) chatText += _typeQueue;
        _typeQueue = "";
        setBubbleText(el, _finalText !== null ? _finalText : chatText);
        _streamEnded = true;
        el.removeAttribute('id');
        const box = document.getElementById('chat-messages');
        if (box) box.scrollTop = box.scrollHeight;
        _applyFinalText(el);
        collabFinish();
        _typeTimer = null;
        return;
    }

    if (_typeQueue.length > 0) {
        const box = document.getElementById('chat-messages');
        const atBottom = _isNearBottom(box);
        // 积压越多一次吐越多：模型一次回几百字时，纯 1 字/80ms 会让气泡
        // 落后好几秒，看起来就是"卡住不动"。这里按积压量平滑追赶。
        const backlog = _typeQueue.length;
        const step = backlog > 96 ? 6 : backlog > 48 ? 4 : backlog > 24 ? 3
            : backlog > 8 ? 2 : 1;
        chatText += _typeQueue.slice(0, step);
        _typeQueue = _typeQueue.slice(step);
        setBubbleText(el, chatText);
        _pinBottom(box, atBottom);
        // 积压特别大时缩短间隔，尽快追上模型的输出
        const interval = backlog > 96 ? Math.min(_typeInterval, 50) : _typeInterval;
        _typeTimer = setTimeout(_typePump, interval);
    } else {
        // 队列消化完
        if (_streamEnded) {
            _applyFinalText(el);  // 打字机走完后用最终文本兜底（去掉流式残片）
            el.removeAttribute('id');
            const box = document.getElementById('chat-messages');
            if (box) box.scrollTop = box.scrollHeight;
            collabFinish();
        }
        _typeTimer = null;
    }
}

// 首字迟迟未到：先在气泡里占位回应，真实内容到达后清掉
function onChatStall(text) {
    if (chatText || _typeQueue.length > 0) return;
    const el = document.getElementById('bot-msg-latest');
    if (!el) return;
    setBubbleText(el, text);
    _stallEl = el;
    const box = document.getElementById('chat-messages');
    if (box) box.scrollTop = box.scrollHeight;
}

function onChatClear() {
    if (!_stallEl) return;
    if (chatText || _typeQueue.length > 0) { _stallEl = null; return; }
    _stallEl.textContent = "";
    _bubbleSrcLen = 0;
    _stallEl = null;
}

function onChatDelta(delta) {
    // 真实内容到了：先清掉占位文字
    if (_stallEl) {
        _stallEl.textContent = "";
        _stallEl = null;
    }
    if (_voiceSync) return;  // 语音段接管显示：增量只留给最终文本兜底
    _typeQueue += delta;
    if (!_typeTimer) _typePump();
}

// 后端最终清洗文本（流结束后推一次）：等打字机走完再替换气泡，兜底流式残片
function onChatFinal(text) {
    if (!text) return;
    _finalText = text;
    if (_voiceSync && !_voiceEnded) return;  // 语音还在播：等播完再补齐尾巴，别抢在声音前
    if (!_streamEnded || _typeTimer) return;  // 还在打字：交给 _typePump 收尾时替换
    _applyFinalText(document.getElementById('bot-msg-latest') || _chatBubbleEl);
}

function _applyFinalText(el) {
    if (_finalText === null) return;
    const t = _finalText;
    _finalText = null;
    el = el || document.getElementById('bot-msg-latest') || _chatBubbleEl;
    if (!el || t === chatText) return;
    // 流式过程中拼出来的文本与最终清洗结果不一致（残片/半截标签）→ 整段替换
    chatText = t;
    _typeQueue = "";
    if (_typeTimer) { clearTimeout(_typeTimer); _typeTimer = null; }
    setBubbleText(el, t);
    const box = document.getElementById('chat-messages');
    if (box) box.scrollTop = box.scrollHeight;
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

// ===== 字音同步：以音频段为时钟，一段一段揭示文字 =====
// 后端在每段 wav 真正开播前推 (本段文本, 该段时长ms)，所以事件到达时刻≈音频起点。
function onVoiceSegment(text, durMs) {
    const el = document.getElementById('bot-msg-latest') || _chatBubbleEl;
    if (!el) return;
    // 已经按普通打字机跑起来了（首句超时放行 / 语音不可用）：不再回跳抢文字
    if (!_voiceSync && (chatText || _typeQueue)) return;
    if (!_voiceSync) {
        _voiceSync = true;
        if (_typeTimer) { clearTimeout(_typeTimer); _typeTimer = null; }
        _typeQueue = "";
    }
    if (_stallEl) { _stallEl.textContent = ""; _bubbleSrcLen = 0; _stallEl = null; }
    _voiceBase += _curSegText;          // 上一段音频已播完：整段结算
    _curSegText = text || "";
    _curSegDur = Number(durMs) > 0 ? Number(durMs) : 0;
    _curSegT0 = performance.now();
    _buildSegTimeline(_curSegText, _curSegDur);
    if (_segRaf) cancelAnimationFrame(_segRaf);
    _voicePump();
}

// 标点加权：标点后的停顿占字数权重，让文字在逗号/句号处停一下再继续
function _buildSegTimeline(text, durMs) {
    const marks = "，。！？、；：…,.!?;:";
    let acc = 0;
    const starts = [];
    const raw = [];
    for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        raw.push(acc);
        acc += marks.includes(ch) ? 2.2 : (/\s/.test(ch) ? 0.25 : 1);
    }
    const total = acc || 1;
    for (let i = 0; i < raw.length; i++) starts.push(durMs * raw[i] / total);
    _segStartMs = starts;
}

function _voicePump() {
    const el = document.getElementById('bot-msg-latest') || _chatBubbleEl;
    if (!el) { _segRaf = null; return; }
    const len = _curSegText.length;
    let k = len;
    if (_curSegDur > 0) {
        const elapsed = performance.now() - _curSegT0;
        k = 0;
        while (k < len && _segStartMs[k] <= elapsed) k++;
    }
    const shown = _voiceBase + _curSegText.slice(0, k);
    // 只增不减，避免段边界结算时文字倒跳；内容没变化就不重建 DOM
    if (shown.length >= _bubbleSrcLen && shown !== el.dataset.raw) {
        const box = document.getElementById('chat-messages');
        const atBottom = _isNearBottom(box);
        setBubbleText(el, shown);
        chatText = shown;
        _pinBottom(box, atBottom);
    }
    if (k < len) {
        _segRaf = requestAnimationFrame(_voicePump);
    } else {
        _segRaf = null;
    }
}

function onVoiceStreamEnd() {
    if (!_voiceSync) return;  // 没进语音同步（语音关闭/合成失败）：不干预普通打字机
    _voiceEnded = true;
    if (_streamEnded) _finishVoice();
}

// 语音播完：把没读出来的尾巴（表情/代码/被截断部分）用最终文本补齐并定稿
function _finishVoice() {
    if (_voiceFinalized) return;
    _voiceFinalized = true;
    if (_segRaf) { cancelAnimationFrame(_segRaf); _segRaf = null; }
    _voiceBase += _curSegText;
    _curSegText = "";
    const el = document.getElementById('bot-msg-latest') || _chatBubbleEl;
    if (!el) return;
    _typeQueue = "";
    if (_typeTimer) { clearTimeout(_typeTimer); _typeTimer = null; }
    if (_finalText !== null) {
        _applyFinalText(el);
    } else {
        chatText = _voiceBase;
        setBubbleText(el, _voiceBase);
    }
    el.removeAttribute('id');
    const box = document.getElementById('chat-messages');
    if (box) box.scrollTop = box.scrollHeight;
    collabFinish();
}

function onChatFinished(errText) {
    // 语音同步模式：文字进度跟着音频走，定稿要等 voice_stream_end
    if (_voiceSync) {
        _streamEnded = true;
        if (_voiceEnded) _finishVoice();
        return;
    }
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
            setBubbleText(el, errText);
            el.removeAttribute('id');
        }
        chatText = "";
        const box = document.getElementById('chat-messages');
        if (box) box.scrollTop = box.scrollHeight;
        collabError();
        return;
    }
    // 有内容（或队列还有剩余）：标记"定型"，pump 消化完后不再滚动
    _streamEnded = true;
    // 如果 pump 还没启动（极端情况：最后几个字符刚好在 pump 间隔内到达）
    if (!_typeTimer && _typeQueue.length > 0) {
        _typePump();
    } else if (!_typeTimer && _typeQueue.length === 0) {
        _applyFinalText(document.getElementById('bot-msg-latest') || _chatBubbleEl);
        const el = document.getElementById('bot-msg-latest');
        if (el) el.removeAttribute('id');
        collabFinish();
    }
}

function onChatFailed(err) {
    if (_typeTimer) { clearTimeout(_typeTimer); _typeTimer = null; }
    _typeQueue = "";
    _stallEl = null;
    const el = document.getElementById('bot-msg-latest');
    if (el) {
        setBubbleText(el, "出错了… " + err);
        el.removeAttribute('id');
    }
    collabError();
}

// ===== 运行日志面板（启动页）=====
const _MAX_LOG_LINES = 500;
const _LOG_EMPTY_HTML = '<div class="log-empty"><div class="log-empty-title">暂无日志</div>' +
    '<div class="log-empty-sub">服务启动、工具调用和报错都会输出到这里</div></div>';

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
        if (!list.length) box.innerHTML = _LOG_EMPTY_HTML;
        const c = document.getElementById('log-count');
        if (c) c.textContent = list.length ? Math.min(list.length, _MAX_LOG_LINES) : 0;
        box.scrollTop = box.scrollHeight;
    }).catch(() => {});
}

function setupLogPanel() {
    const btn = document.getElementById('log-clear');
    if (btn) btn.addEventListener('click', () => {
        const box = document.getElementById('log-box');
        if (!box) return;
        box.innerHTML = _LOG_EMPTY_HTML;
        const c = document.getElementById('log-count');
        if (c) c.textContent = '0';
    });
    loadRecentLogs();
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

// 待发送图片：选图/粘贴后先在输入框上方预览，点缩略图看大图、可复制，再按发送
let pendingImage = null;   // { dataUrl, name }

function ensurePendingBar() {
    let bar = document.getElementById('pending-image');
    if (bar) return bar;
    const inputBar = document.querySelector('.input-bar');
    if (!inputBar) return null;
    bar = document.createElement('div');
    bar.id = 'pending-image';
    bar.className = 'pending-image';
    bar.style.display = 'none';
    bar.innerHTML =
        '<div class="pending-thumb"><img class="msg-img" alt="待发送图片" /></div>' +
        '<div class="pending-info">' +
        '<div class="pending-name"></div>' +
        '<div class="pending-tip">点图片可预览放大</div>' +
        '</div>' +
        '<div class="pending-actions">' +
        '<button class="lb-btn" data-act="copy">复制图片</button>' +
        '<button class="lb-btn" data-act="remove">移除</button>' +
        '</div>';
    inputBar.parentNode.insertBefore(bar, inputBar);
    bar.querySelector('[data-act="remove"]').addEventListener('click', clearPendingImage);
    bar.querySelector('[data-act="copy"]').addEventListener('click', e => {
        if (pendingImage) copyImage(pendingImage.dataUrl, e.currentTarget);
    });
    return bar;
}

function renderPendingImage() {
    const bar = ensurePendingBar();
    if (!bar) return;
    if (!pendingImage) {
        bar.style.display = 'none';
        bar.querySelector('.pending-thumb .msg-img').removeAttribute('src');
        return;
    }
    bar.querySelector('.pending-thumb .msg-img').src = pendingImage.dataUrl;
    bar.querySelector('.pending-name').textContent = pendingImage.name || '图片';
    bar.style.display = 'flex';
}

function setPendingImage(dataUrl, name) {
    pendingImage = { dataUrl, name: name || 'pasted.jpg' };
    renderPendingImage();
}

function clearPendingImage() {
    pendingImage = null;
    renderPendingImage();
}

// 有待发图片时，发送键先发图（带着输入框里的文字）；否则走普通文字发送
function handleSend(inputEl) {
    if (pendingImage && bridge && bridge.send_image) {
        const text = inputEl.value.trim();
        const img = pendingImage;
        const b64 = img.dataUrl.split(',')[1] || '';
        if (!b64) return;
        appendImageMsg(img.dataUrl, text);
        inputEl.value = '';
        collabBegin(text || '看这张图');
        bridge.send_image(b64, img.name, text);
        clearPendingImage();
        return;
    }
    sendMessage(inputEl);
}

// 首页输入框（对话已合并到首页）
    const input1 = document.getElementById('chat-input');
    const btn1 = document.getElementById('send-btn');
    btn1.addEventListener('click', () => handleSend(input1));
    input1.addEventListener('keydown', e => { if (e.key === 'Enter') handleSend(input1); });

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
    compactHero();
    box.scrollTop = box.scrollHeight;
}

function sendImageFile(file) {
    if (!file) return;
    compressImage(file).then(dataUrl => {
        setPendingImage(dataUrl, file.name || 'pasted.jpg');
        // 选图后把焦点放回输入框：可以直接补一句说明，再按回车发送
        if (input1) input1.focus();
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

// ===== 电脑状态页：等待首次采样 / 就绪 / 失败三态 =====
let _statusPendingTimer = null;

// 迟迟没有数据时转入失败态，避免永远停在这句“等待首次采样…”
function armStatusPendingTimeout() {
    // 已在计时就不重置：否则每次轮询返回空数据都会把超时往后推
    if (_statusPendingTimer !== null) return;
    _statusPendingTimer = setTimeout(() => {
        _statusPendingTimer = null;
        const grid = document.getElementById('status-grid');
        if (grid && grid.dataset.state === 'pending') {
            setStatusPageState('error', '等待超时，点击重试');
        }
    }, 4000);
}

function setStatusPageState(state, detail) {
    const grid = document.getElementById('status-grid');
    if (!grid) return;
    grid.classList.toggle('pending', state !== 'ready');
    grid.dataset.state = state;
    if (state === 'pending') {
        armStatusPendingTimeout();
    } else {
        clearTimeout(_statusPendingTimer);
        _statusPendingTimer = null;
    }
    const sub = document.getElementById('status-sub');
    if (sub) {
        if (state === 'ready') sub.textContent = '实时监控 · 每 3 秒刷新';
        else if (state === 'error') sub.textContent = detail || '采样失败';
        else sub.textContent = '等待首次采样…';
    }
    const retry = document.getElementById('status-retry');
    if (retry) retry.style.display = state === 'error' ? '' : 'none';
}

function retrySystemStatus() {
    setStatusPageState('pending');
    loadSystemStatus();
}

function renderSystemStatus(raw) {
    let d;
    try { d = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch (e) {
        setStatusPageState('error', '状态数据解析失败');
        return;
    }
    if (!d || !d.cpu) { setStatusPageState('pending'); return; }
    const $ = (id) => document.getElementById(id);
    if (!$('cpu-percent')) return;
    setStatusPageState('ready');

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
        setStatusPageState('pending');
        Promise.resolve(bridge.get_system_status())
            .then(renderSystemStatus)
            .catch(() => setStatusPageState('error', '获取电脑状态失败'));
    } catch (e) {
        setStatusPageState('error', '获取电脑状态失败');
    }
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
    document.querySelector('.nav-btn[data-page="collab"]')?.classList.remove('tool-pulse');
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
    document.querySelector('.nav-btn[data-page="collab"]')?.classList.remove('tool-pulse');
    setBannerState('error', '出错了');
    setWorkspace('error', '出错了', '达妮娅回复失败', '处理异常', '请检查服务状态后重试');
}

// ===== 任务页 =====
function taskLoadFailed() {
    const container = document.querySelector('.task-list');
    if (!container) return;
    container.innerHTML = '<div class="empty-state"><div class="empty-title">任务加载失败</div>' +
        '<div class="empty-sub">没能拿到任务数据<button type="button" class="inline-retry" id="task-retry">重试</button></div></div>';
    const retry = document.getElementById('task-retry');
    if (retry) retry.addEventListener('click', loadTasks);
}

function loadTasks() {
    if (!bridge || !bridge.get_tasks) return;
    bridge.get_tasks().then(raw => {
        let list;
        try { list = JSON.parse(raw); } catch (e) { taskLoadFailed(); return; }
        if (!Array.isArray(list)) { taskLoadFailed(); return; }
        const container = document.querySelector('.task-list');
        container.innerHTML = "";
        if (list.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-title">暂无待办任务</div>' +
                '<div class="empty-sub">和达妮娅说「帮我记一件事」，任务会出现在这里</div></div>';
            return;
        }
        list.forEach(t => {
            const item = document.createElement('div');
            item.className = 'task-item' + (t.done ? ' is-done' : '');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'task-check';
            cb.checked = !!t.done;
            cb.setAttribute('aria-label', (t.text || '') + (t.done ? '，已完成' : '，待完成'));
            const txt = document.createElement('span');
            txt.className = 'task-text';
            txt.textContent = t.text || '';
            const prio = document.createElement('span');
            const prioClass = t.prio === '高' ? 'prio-high' : (t.prio === '中' ? 'prio-mid' : 'prio-low');
            prio.className = 'task-prio ' + prioClass;
            prio.textContent = t.prio || '';
            item.appendChild(cb);
            item.appendChild(txt);
            item.appendChild(prio);
            // 勾选即完成：后端取消对应提醒，随后刷新列表
            cb.addEventListener('change', () => {
                if (!cb.checked) return;
                cb.disabled = true;
                if (bridge && bridge.complete_task && t.idx !== undefined) {
                    try { bridge.complete_task(t.idx); } catch (e) { }
                    setTimeout(loadTasks, 400);
                } else {
                    cb.disabled = false;
                    cb.checked = false;
                }
            });
            container.appendChild(item);
        });
    }).catch(taskLoadFailed);
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
            const stateClass = ['running', 'starting', 'failed'].includes(s.state) ? s.state : 'stopped';
            const stateText = { running: '运行中', stopped: '已停止', starting: '启动中', failed: '失败' }[s.state] || s.state;
            const btnLabel = s.state === 'running' ? '停止'
                : s.state === 'starting' ? '启动中'
                : s.state === 'failed' ? '重试' : '启动';
            item.innerHTML = `
                <div class="svc-info">
                    <span class="svc-name">${_escapeHTML(s.display)}</span>
                    ${s.note ? `<span class="svc-note">${_escapeHTML(s.note)}</span>` : ''}
                </div>
                <div class="svc-actions">
                    <span class="svc-status ${stateClass}">${_escapeHTML(stateText)}</span>
                    <button class="svc-btn" data-name="${_escapeHTML(s.name)}" data-action="${s.state === 'running' ? 'stop' : 'start'}"
                        ${s.state === 'starting' ? 'disabled' : ''}>${btnLabel}</button>
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

// 推特式图片网格：1 张大图，2 张并排，3 张左大右两小，4 张以上九宫格
function buildImageGrid(imgs) {
    const grid = document.createElement('div');
    grid.className = 'msg-img-grid';
    grid.dataset.count = String(imgs.length);
    imgs.forEach(it => {
        const cell = document.createElement('div');
        cell.className = 'msg-img-cell';
        const img = document.createElement('img');
        img.className = 'msg-img';
        img.alt = it.name || '图片';
        img.src = it.src;
        cell.appendChild(img);
        grid.appendChild(cell);
    });
    return grid;
}

function onImageMessage(raw) {
    let data = {};
    try { data = JSON.parse(raw || '{}'); } catch (e) { return; }
    const imgs = (data.images || []).filter(it => it && it.src);
    if (!imgs.length) return;
    const box = document.getElementById('chat-messages');
    if (!box) return;
    const msg = document.createElement('div');
    msg.className = 'msg msg-bot';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble msg-bubble-img';
    bubble.appendChild(buildImageGrid(imgs));
    msg.appendChild(bubble);
    box.appendChild(msg);
    box.scrollTop = box.scrollHeight;
}

// ===== 图片预览层（点击缩略图放大，可复制图片）=====
let _lightbox = null;

function ensureLightbox() {
    if (_lightbox) return _lightbox;
    const mask = document.createElement('div');
    mask.className = 'img-lightbox';
    mask.innerHTML =
        '<div class="img-lightbox-tools">' +
        '<button class="lb-btn" data-act="copy">复制图片</button>' +
        '<button class="lb-btn" data-act="close">关闭</button></div>' +
        '<img alt="预览" />';
    document.body.appendChild(mask);

    const img = mask.querySelector('img');
    const close = () => {
        mask.classList.remove('open');
        setTimeout(() => { if (!mask.classList.contains('open')) img.removeAttribute('src'); }, 200);
    };
    mask.addEventListener('click', e => { if (e.target === mask) close(); });
    mask.querySelector('[data-act="close"]').addEventListener('click', close);
    mask.querySelector('[data-act="copy"]').addEventListener('click', e => copyImage(img.src, e.currentTarget));
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && mask.classList.contains('open')) close();
    });
    _lightbox = mask;
    return mask;
}

function openImagePreview(src) {
    if (!src) return;
    const mask = ensureLightbox();
    mask.querySelector('img').src = src;
    mask.classList.add('open');
}

async function copyImage(src, btn) {
    if (!src) return;
    const label = btn ? btn.dataset.label || btn.textContent : '';
    if (btn) { btn.dataset.label = label; btn.disabled = true; btn.textContent = '复制中…'; }
    let ok = false;
    if (bridge && bridge.copy_image) {
        try { ok = await bridge.copy_image(src); } catch (e) { ok = false; }
    }
    if (!ok) {
        // 浏览器 mock 模式兜底
        try {
            const blob = await (await fetch(src)).blob();
            await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })]);
            ok = true;
        } catch (e) { ok = false; }
    }
    if (btn) {
        btn.textContent = ok ? '已复制' : '复制失败';
        setTimeout(() => { btn.disabled = false; btn.textContent = label; }, 1600);
    }
}

// 所有聊天图片（含用户自己发的）点一下都能放大预览
document.addEventListener('click', e => {
    const img = e.target.closest ? e.target.closest('.msg-img') : null;
    if (img && img.src) openImagePreview(img.src);
});

// ===== 设置页 =====
function onBiliLoginFinished(raw) {
    // 扫码登录结束：结果写进日志面板，并刷新设置行状态
    let data = {};
    try { data = JSON.parse(raw); } catch (e) { data = { text: String(raw) }; }
    if (typeof onLog === 'function') onLog(`[B站登录] ${data.text || raw}`);
    loadSettings();
}

// ===== 设置页 =====
let _settingsToastTimer = null;

// 保存反馈：底部 toast + 撤销（撤销即写回原值并重渲染）
function showSettingsToast(text, undo) {
    const page = document.getElementById('page-settings');
    if (!page) return;
    let el = document.getElementById('settings-toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'settings-toast';
        el.className = 'settings-toast';
        el.setAttribute('role', 'status');
        el.setAttribute('aria-live', 'polite');
        page.appendChild(el);
    }
    el.innerHTML = '';
    const msg = document.createElement('span');
    msg.className = 'toast-msg';
    msg.textContent = text;
    el.appendChild(msg);
    const hide = () => el.classList.remove('show');
    if (undo) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'toast-undo';
        btn.textContent = '撤销';
        btn.addEventListener('click', () => { hide(); undo(); });
        el.appendChild(btn);
    }
    el.classList.add('show');
    clearTimeout(_settingsToastTimer);
    _settingsToastTimer = setTimeout(hide, 4000);
}

function loadSettings() {
    if (!bridge || !bridge.get_settings) return;
    bridge.get_settings().then(raw => {
        const list = JSON.parse(raw);
        const container = document.querySelector('.settings-list');
        container.innerHTML = "";
        let lastGroup = '';
        list.forEach(s => {
            const group = s.group || '其他';
            if (group !== lastGroup) {
                const title = document.createElement('div');
                title.className = 'set-group-title';
                title.textContent = group;
                container.appendChild(title);
                lastGroup = group;
            }
            const row = document.createElement('div');
            row.className = 'setting-row';
            if (s.type === 'bool') {
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <label class="switch">
                        <input type="checkbox" ${s.value ? 'checked' : ''} data-key="${s.key}" data-old="${s.value ? 'true' : 'false'}" aria-label="${s.label}" />
                        <span class="slider"></span>
                    </label>`;
            } else if (s.type === 'model_select' && s.options) {
                let opts = s.options.map(o =>
                    `<option value="${o.value}" ${o.value === s.value ? 'selected' : ''}>${o.label}</option>`
                ).join('');
                const selLabel = (s.options.find(o => o.value === s.value) || {}).label || s.value;
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <select class="model-select" data-key="${s.key}" data-old="${s.value}" aria-label="${s.label}" title="${String(selLabel).replace(/"/g, '&quot;')}">${opts}</select>`;
            } else if (s.type === 'preset_select' && s.options) {
                let opts = s.options.map(o =>
                    `<option value="${o.value}" ${o.value === s.value ? 'selected' : ''}>${o.label}</option>`
                ).join('');
                const presetLabel = (s.options.find(o => o.value === s.value) || {}).label || s.value;
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <select class="preset-select" data-key="${s.key}" data-old="${s.value}" aria-label="${s.label}" title="${String(presetLabel).replace(/"/g, '&quot;')}">${opts}</select>`;
            } else if (s.type === 'str' && s.key) {
                const inputVal = s.value ? s.value.replace(/"/g, '&quot;') : '';
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <input type="text" class="setting-input" data-key="${s.key}" data-old="${inputVal}" data-label="${s.label}" aria-label="${s.label}" data-placeholder="${s.placeholder ? s.placeholder.replace(/"/g, '&quot;') : ''}" value="${inputVal}" placeholder="${s.placeholder || ''}" />`;
            } else if (s.type === 'readonly') {
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <span class="set-value is-readonly">${s.value}</span>`;
            } else if (s.type === 'hint') {
                row.classList.add('setting-hint-row');
                row.innerHTML = `<span class="set-hint">${s.label}</span>`;
            } else if (s.type === 'action') {
                row.classList.add('setting-action-row');
                const actVal = String(s.value || '').replace(/"/g, '&quot;');
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <span class="set-value" title="${actVal}">${s.value || ''}</span>
                    <button class="btn-mini" data-action="${s.key}">${s.button || '执行'}</button>`;
            } else {
                row.innerHTML = `
                    <span class="set-label">${s.label}</span>
                    <span class="set-value is-readonly">${s.value}</span>`;
            }
            container.appendChild(row);
        });
        // bool 开关
        container.querySelectorAll('.switch input').forEach(cb => {
            cb.addEventListener('change', () => {
                const key = cb.dataset.key;
                const old = cb.dataset.old;
                bridge.save_setting(key, cb.checked ? 'true' : 'false');
                showSettingsToast('已保存', () => {
                    bridge.save_setting(key, old);
                    setTimeout(loadSettings, 300);
                });
            });
        });
        // 模型下拉框（切换后重新拉取设置，因为配置项条目会随模型类型变化）
        container.querySelectorAll('.model-select').forEach(sel => {
            sel.addEventListener('change', () => {
                const key = sel.dataset.key;
                const old = sel.dataset.old;
                bridge.save_setting(key, sel.value);
                showSettingsToast('已保存，模型将在下一次回复生效', () => {
                    bridge.save_setting(key, old);
                    setTimeout(loadSettings, 300);
                });
                // 延迟重渲染，避免 input 焦点状态丢失
                setTimeout(() => loadSettings(), 600);
            });
        });
        // 认知档位下拉框（省电/标准/深度，切换后立即生效）
        container.querySelectorAll('.preset-select').forEach(sel => {
            sel.addEventListener('change', () => {
                const key = sel.dataset.key;
                const old = sel.dataset.old;
                bridge.save_setting(key, sel.value);
                showSettingsToast('已保存，认知档位立即生效', () => {
                    bridge.save_setting(key, old);
                    setTimeout(loadSettings, 300);
                });
            });
        });
        // 文本输入框（带 key 的 str 类型）
        container.querySelectorAll('.setting-input').forEach(inp => {
            const commit = () => {
                if (inp.value === inp.dataset.old) return;
                const key = inp.dataset.key;
                const prev = inp.dataset.old;
                const label = inp.dataset.label || key;
                inp.dataset.old = inp.value;
                bridge.save_setting(key, inp.value);
                showSettingsToast('已保存：' + label, () => {
                    bridge.save_setting(key, prev);
                    setTimeout(loadSettings, 300);
                });
            };
            inp.addEventListener('change', commit);
            inp.addEventListener('blur', commit);
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
    }).catch(() => { });
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

// ===== 记忆页 =====
const MEM_KIND_LABEL = { event: "经历", promise: "约定", preference: "偏好", mood: "心情", diary: "日记" };

function memEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
}

function forgetMemory(id, btn) {
    if (!bridge || !bridge.delete_memory || !id) return;
    btn.disabled = true;
    Promise.resolve(bridge.delete_memory(id)).then(ok => {
        if (ok) loadMemories();
        else btn.disabled = false;
    }).catch(() => { btn.disabled = false; });
}

function renderMemoryItem(ep) {
    const item = memEl('div', 'mem-item');
    const main = memEl('div', 'mem-item-main');
    const meta = memEl('div', 'mem-item-meta');
    meta.appendChild(memEl('span', 'mem-kind mem-kind-' + (ep.kind || 'event'),
        MEM_KIND_LABEL[ep.kind] || ep.kind || '经历'));
    meta.appendChild(memEl('span', 'mem-date', ep.date || ''));
    main.appendChild(meta);
    main.appendChild(memEl('div', 'mem-text', ep.text || ''));
    const del = memEl('button', 'mem-del', '忘记');
    del.type = 'button';
    del.dataset.tip = '让她忘掉这条记忆';
    del.addEventListener('click', () => forgetMemory(ep.id, del));
    item.appendChild(main);
    item.appendChild(del);
    return item;
}

function loadMemories() {
    if (!bridge || !bridge.get_memories) return;
    Promise.resolve(bridge.get_memories()).then(raw => {
        let data = null;
        try { data = JSON.parse(raw); } catch (e) { }
        const sub = document.getElementById('mem-sub');
        const stats = document.getElementById('mem-stats');
        const entsBox = document.getElementById('mem-entities');
        const loopBox = document.getElementById('mem-loops');
        const loopBlock = document.getElementById('mem-loop-block');
        const list = document.getElementById('mem-list');
        if (!data || data.error) {
            if (sub) sub.textContent = '读取失败';
            return;
        }
        const st = data.stats || {};
        if (sub) {
            sub.textContent = `${st.total || 0} 条记忆 · ${st.entities || 0} 个实体 · ${st.loops || 0} 件待追问`;
        }
        if (stats) {
            stats.innerHTML = '';
            [['记忆', st.total || 0], ['实体', st.entities || 0], ['待追问', st.loops || 0]].forEach(([k, v]) => {
                const chip = memEl('div', 'mem-stat');
                chip.appendChild(memEl('span', 'mem-stat-num', String(v)));
                chip.appendChild(memEl('span', 'mem-stat-label', k));
                stats.appendChild(chip);
            });
        }
        if (entsBox) {
            entsBox.innerHTML = '';
            const ents = data.entities || [];
            if (!ents.length) {
                entsBox.appendChild(memEl('div', 'mem-empty', '还没记住关于你的事，多聊聊吧。'));
            } else {
                ents.forEach(e => {
                    const chip = memEl('span', 'mem-entity');
                    chip.appendChild(memEl('span', 'mem-entity-key', (e.key || '') + '：'));
                    chip.appendChild(memEl('span', 'mem-entity-val', e.value || ''));
                    entsBox.appendChild(chip);
                });
            }
        }
        if (loopBox && loopBlock) {
            loopBox.innerHTML = '';
            const loops = data.loops || [];
            loopBlock.style.display = loops.length ? '' : 'none';
            loops.forEach(l => {
                const item = memEl('div', 'mem-loop');
                item.appendChild(memEl('span', 'mem-loop-due', l.due || ''));
                item.appendChild(memEl('span', 'mem-loop-text', l.text || ''));
                const del = memEl('button', 'mem-del', '忘记');
                del.type = 'button';
                del.addEventListener('click', () => forgetMemory(l.id, del));
                item.appendChild(del);
                loopBox.appendChild(item);
            });
        }
        if (list) {
            list.innerHTML = '';
            const eps = data.episodes || [];
            if (!eps.length) {
                list.appendChild(memEl('div', 'mem-empty', '暂无记忆片段。聊过的事会在这里慢慢积累。'));
            } else {
                eps.forEach(ep => list.appendChild(renderMemoryItem(ep)));
            }
        }
    }).catch(() => {
        const sub = document.getElementById('mem-sub');
        if (sub) sub.textContent = '读取失败';
    });
}

const memRefresh = document.getElementById('mem-refresh');
if (memRefresh) memRefresh.addEventListener('click', loadMemories);
