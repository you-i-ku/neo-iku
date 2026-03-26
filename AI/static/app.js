// neo-iku フロントエンド

const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const connectionStatus = document.getElementById("connection-status");
const memorySearch = document.getElementById("memory-search");
const searchBtn = document.getElementById("search-btn");
const memoryList = document.getElementById("memory-list");
const modeBtn = document.getElementById("mode-btn");
const thoughtLog = document.getElementById("thought-log");

let ws = null;
let currentStreamEl = null;
let isStreaming = false;
let currentMode = "normal";
let countdownInterval = null;
let countdownRemaining = 0;
let processingIndicator = null; // チャット欄のthinking表示

// 思考ログの自動スクロール制御（ユーザーが上を見てる時はスクロールしない）
let devUserScrolledUp = false;
thoughtLog.addEventListener("scroll", () => {
    const threshold = 80;
    devUserScrolledUp = thoughtLog.scrollHeight - thoughtLog.scrollTop - thoughtLog.clientHeight > threshold;
});
function devScrollToBottom() {
    if (!devUserScrolledUp) {
        devScrollToBottom();
    }
}

// --- タブ切り替え ---

const tabBtns = document.querySelectorAll(".tab-btn");
const tabContents = document.querySelectorAll(".tab-content");

tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
        const tab = btn.dataset.tab;
        tabBtns.forEach(b => b.classList.remove("active"));
        tabContents.forEach(c => c.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById(`tab-${tab}`).classList.add("active");

        // ログタブ初回表示時にWebSocket接続
        if (tab === "log" && !logWs) connectLog();
        // 自律度タブ初回表示時に蒸留ログを自動読み込み
        if (tab === "report" && !distillationLoaded) loadDistillationLog();
    });
});

// --- WebSocket ---

function connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws/chat`);

    ws.onopen = () => {
        connectionStatus.className = "status-dot online";
        updateStatusBarLLM(true);
        sendBtn.disabled = false;
    };

    ws.onclose = () => {
        connectionStatus.className = "status-dot offline";
        updateStatusBarLLM(false);
        sendBtn.disabled = true;
        setTimeout(connect, 3000);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        switch (data.type) {
            // --- 開発者タブ: セッション・思考ログ ---
            case "dev_session_start":
                addDevSession(data.source, data.preview);
                break;

            case "dev_round_start":
                addDevRound(data.round, data.source);
                break;

            case "dev_think_start":
            case "dev_think_end":
                // think/streamは統合表示、開始/終了マーカー不要
                break;

            case "dev_think":
                appendDevContent(data.content, true);
                break;

            case "dev_stream":
                appendDevContent(data.content, false);
                break;

            // --- チャットタブ ---
            case "message_ack":
                showMessageAck();
                break;

            case "responding_start":
                showRespondingIndicator();
                break;

            case "responding_end":
                hideRespondingIndicator();
                break;

            case "processing_start":
                showProcessingIndicator();
                break;

            case "processing_end":
                hideProcessingIndicator();
                break;

            case "output":
                hideProcessingIndicator();
                hideRespondingIndicator();
                if (data.source === "autonomous") {
                    addMessage("autonomous", data.content);
                } else {
                    addMessage("assistant", data.content);
                }
                loadMemories(memorySearch.value.trim());
                break;

            case "stopped":
                hideProcessingIndicator();
                addMessage("system", "⏹ 出力を中断しました");
                break;

            case "error":
                hideProcessingIndicator();
                addMessage("error", data.content);
                break;

            case "tool_call":
                addMessage("tool_call", data.content);
                break;

            case "dev_tool_call":
                appendDevToolCall(data.content);
                break;

            case "dev_tool_result":
                appendDevToolResult(data.name, data.content);
                loadMemories(memorySearch.value.trim());
                if (data.name === "update_self_model" || data.name === "read_self_model") loadSelfModel();
                break;

            case "dev_env_stimulus":
                appendDevEnvStimulus(data.content);
                break;

            case "write_approval":
                showWriteApproval(data);
                break;

            case "exec_approval":
                showExecApproval(data);
                break;

            case "create_tool_approval":
                showCreateToolApproval(data);
                break;

            case "approval_timeout":
                // 承認待ちUIを閉じてタイムアウト通知を表示
                closeApprovalDialog();
                appendSystemMessage(data.message || "承認要求がタイムアウトしました");
                break;

            case "exec_start":
                showExecTerminal(data);
                break;

            case "exec_output":
                appendExecOutput(data);
                break;

            case "exec_end":
                finalizeExecTerminal(data);
                break;

            case "autonomous_countdown":
                startCountdown(data.seconds);
                break;

            case "autonomous_tool":
                updateAutonomousToolStatus(data.name, data.status);
                break;

            case "autonomous_think_start":
                updateStatusBarCountdown("行動中...");
                startAutonomousThink();
                break;

            case "autonomous_think_end":
                stopAutonomousThink();
                break;

            case "motivation_energy":
                updateMotivationEnergy(data.energy, data.threshold, data.breakdown);
                break;

            case "user_interrupt_ack":
                addMessage("system", `💬 割り込みメッセージを受け付けました: ${data.content}`);
                break;

            case "distillation_session":
                if (distillationLoaded) {
                    prependDistillationSession(data.session);
                }
                break;

            case "distillation_update":
                updateDistillationResponse(data.conv_id, data.distillation_response, data.principle);
                break;
        }
    };
}

// --- 開発者タブ: セッション・思考ログ ---

// ソースごとに状態を分離（chat/autonomous並行対応）
let devState = {
    chat: { sessionEl: null, roundEl: null, contentEl: null },
    autonomous: { sessionEl: null, roundEl: null, contentEl: null },
};
let activeDevSource = "chat";
let devSessionCount = 0;

function devS() { return devState[activeDevSource]; }

function clearDevEmpty() {
    const empty = thoughtLog.querySelector(".thought-log-empty");
    if (empty) empty.remove();
}

function addDevSession(source, preview) {
    clearDevEmpty();
    devSessionCount++;
    const el = document.createElement("div");
    const isAuto = source === "autonomous";
    el.className = `dev-session ${isAuto ? "dev-session-autonomous" : ""}`;

    const time = new Date().toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const label = isAuto ? "💭 自律行動" : `💬 ${escapeHtml(preview)}`;
    el.innerHTML = `<div class="dev-session-header"><span class="dev-session-label">#${devSessionCount} ${label}</span><span class="dev-session-time">${time}</span></div><div class="dev-session-rounds"></div>`;

    thoughtLog.appendChild(el);
    const key = isAuto ? "autonomous" : "chat";
    devState[key].sessionEl = el.querySelector(".dev-session-rounds");
    devState[key].roundEl = null;
    devState[key].contentEl = null;
    devScrollToBottom();
}

function addDevRound(round, source) {
    clearDevEmpty();
    activeDevSource = source === "autonomous" ? "autonomous" : "chat";
    const s = devS();

    const el = document.createElement("div");
    el.className = `dev-round ${source === "autonomous" ? "dev-round-autonomous" : ""}`;
    el.innerHTML = `<details class="dev-round-details"><summary class="dev-round-header">${source === "autonomous" ? "💭 " : ""}ラウンド ${round}</summary><div class="dev-round-content"></div></details>`;

    const parent = s.sessionEl || thoughtLog;
    parent.appendChild(el);
    s.roundEl = el.querySelector(".dev-round-content");
    s.contentEl = null;
    devScrollToBottom();
}

// think + stream 統合表示
function appendDevContent(content, isThink) {
    const s = devS();
    if (!s.roundEl) return;
    if (!s.contentEl) {
        s.contentEl = document.createElement("div");
        s.contentEl.className = "dev-content-text";
        s.roundEl.appendChild(s.contentEl);
    }
    const span = document.createElement("span");
    span.className = isThink ? "dev-text-think" : "dev-text-stream";
    span.textContent = content;
    s.contentEl.appendChild(span);
    devScrollToBottom();
}

// --- 開発者タブ: ツール呼び出し/結果 ---

function appendDevToolCall(content) {
    const s = devS();
    if (!s.roundEl) return;
    s.contentEl = null; // ツール後のストリームは新しいブロック
    const el = document.createElement("div");
    el.className = "dev-tool-call";
    el.textContent = "⚙ " + content;
    s.roundEl.appendChild(el);
    devScrollToBottom();
}

function appendDevToolResult(name, content) {
    const s = devS();
    if (!s.roundEl) return;
    const el = document.createElement("div");
    el.className = "dev-tool-result";
    if (content.length > 100) {
        const preview = content.slice(0, 100) + "…";
        el.innerHTML = `<details><summary class="dev-tool-result-summary">${escapeHtml(preview)}</summary><pre class="dev-tool-result-full">${escapeHtml(content)}</pre></details>`;
    } else {
        el.textContent = content;
    }
    s.roundEl.appendChild(el);
    devScrollToBottom();
}

function appendDevEnvStimulus(content) {
    const s = devS();
    if (!s.sessionEl) return;
    const el = document.createElement("div");
    el.className = "dev-env-stimulus";
    el.textContent = "~ " + content;
    s.sessionEl.appendChild(el);
    devScrollToBottom();
}

// --- チャット: 処理中インジケーター ---

function showProcessingIndicator() {
    hideProcessingIndicator();
    const el = document.createElement("div");
    el.className = "message processing-indicator";
    el.innerHTML = `<span class="processing-dots">thinking</span>`;
    chatMessages.appendChild(el);
    processingIndicator = el;

    // ドットアニメーション
    let dotCount = 1;
    el._dotInterval = setInterval(() => {
        dotCount = (dotCount % 3) + 1;
        const span = el.querySelector(".processing-dots");
        if (span) span.textContent = "thinking" + ".".repeat(dotCount);
    }, 400);

    scrollToBottom();
}

function hideProcessingIndicator() {
    if (processingIndicator) {
        if (processingIndicator._dotInterval) {
            clearInterval(processingIndicator._dotInterval);
        }
        processingIndicator.remove();
        processingIndicator = null;
    }
}

// --- チャット: メッセージ受付・応答中インジケーター ---

let respondingIndicator = null;

function showMessageAck() {
    // 「受け付けました」を一時表示（既存のackがあれば差し替え）
    hideMessageAck();
    const el = document.createElement("div");
    el.className = "message message-ack";
    el.textContent = "受け付けました";
    el.id = "message-ack";
    chatMessages.appendChild(el);
    scrollToBottom();
}

function hideMessageAck() {
    const existing = document.getElementById("message-ack");
    if (existing) existing.remove();
}

function showRespondingIndicator() {
    hideMessageAck();
    hideRespondingIndicator();
    const el = document.createElement("div");
    el.className = "message responding-indicator";
    el.innerHTML = `<span class="responding-dots">お返事中です.</span>`;
    chatMessages.appendChild(el);
    respondingIndicator = el;

    let dotCount = 1;
    el._dotInterval = setInterval(() => {
        dotCount = (dotCount % 3) + 1;
        const span = el.querySelector(".responding-dots");
        if (span) span.textContent = "お返事中です" + ".".repeat(dotCount);
    }, 500);

    scrollToBottom();
}

function hideRespondingIndicator() {
    if (respondingIndicator) {
        if (respondingIndicator._dotInterval) {
            clearInterval(respondingIndicator._dotInterval);
        }
        respondingIndicator.remove();
        respondingIndicator = null;
    }
}

// --- チャット: メッセージ ---

function addMessage(type, content) {
    const el = document.createElement("div");
    el.className = `message ${type}`;
    if (type === "tool_result") {
        const preview = content.length > 80 ? content.slice(0, 80) + "…" : content;
        el.innerHTML = `<details><summary class="tool-result-summary">${escapeHtml(preview)}</summary><pre class="tool-result-full">${escapeHtml(content)}</pre></details>`;
    } else {
        el.textContent = content;
    }
    chatMessages.appendChild(el);
    scrollToBottom();
    return el;
}

// --- 自律行動 ---

let autonomousThinkEl = null;
let autonomousThinkInterval = null;

function startAutonomousThink() {
    stopAutonomousThink();

    const el = document.createElement("div");
    el.className = "message think-block autonomous-think";
    el.innerHTML = `<div class="think-summary-static">think.</div>`;
    chatMessages.appendChild(el);
    autonomousThinkEl = el;

    let dotCount = 1;
    autonomousThinkInterval = setInterval(() => {
        if (!autonomousThinkEl) { stopAutonomousThink(); return; }
        dotCount = (dotCount % 3) + 1;
        const label = autonomousThinkEl.querySelector(".think-summary-static");
        if (label) label.textContent = "think" + ".".repeat(dotCount);
    }, 400);

    scrollToBottom();
}

function updateAutonomousToolStatus(toolName, status) {
    if (status === "running") {
        if (autonomousThinkEl) {
            const label = autonomousThinkEl.querySelector(".think-summary-static");
            if (label) label.textContent = `⚙ ${toolName} 実行中...`;
        }
        return;
    }
    if (status === "error") {
        addMessage("autonomous-tool", `⚠ ${toolName} — エラーが発生しました`);
    } else {
        addMessage("autonomous-tool", `⚙ ${toolName} を使用しました`);
    }
    scrollToBottom();
}

function stopAutonomousThink() {
    if (autonomousThinkInterval) {
        clearInterval(autonomousThinkInterval);
        autonomousThinkInterval = null;
    }
    if (autonomousThinkEl) {
        autonomousThinkEl.remove();
        autonomousThinkEl = null;
    }
}

// --- スクロール ---

function isNearBottom() {
    const threshold = 80;
    return chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < threshold;
}

let userScrolledUp = false;

chatMessages.addEventListener("scroll", () => {
    userScrolledUp = !isNearBottom();
});

function scrollToBottom(force = false) {
    if (force || !userScrolledUp) {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
}

// --- 入力・送信 ---

const stopBtn = document.getElementById("stop-btn");

function setStreaming(active) {
    isStreaming = active;
    stopBtn.disabled = !active;
}

function stopStreaming() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const feedback = chatInput.value.trim();
    ws.send(JSON.stringify({ type: "stop", feedback }));
    if (feedback) {
        chatInput.value = "";
        chatInput.style.height = "auto";
    }
}

function sendMessage() {
    const text = chatInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    addMessage("user", text);
    ws.send(JSON.stringify({ message: text }));
    chatInput.value = "";
    chatInput.style.height = "auto";
    userScrolledUp = false;
    scrollToBottom(true);
    loadMemories(text);
}

sendBtn.addEventListener("click", sendMessage);
stopBtn.addEventListener("click", stopStreaming);

chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

chatInput.addEventListener("input", () => {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + "px";
});

// --- モード・ペルソナ切替 ---

let activePersona = null; // {id, name, display_name, color_theme, ...} or null

function updateModeUI(mode, persona) {
    currentMode = mode;
    activePersona = persona || null;

    if (persona) {
        modeBtn.textContent = persona.display_name;
        modeBtn.className = `mode-btn persona`;
        document.body.className = `theme-${persona.color_theme || 'purple'}`;
        document.querySelector('.tab-btn-persona').style.display = '';
        updateStatusBarMode(persona.display_name);
        loadPersonaTab(persona.id);
    } else {
        modeBtn.textContent = "ノーマル";
        modeBtn.className = `mode-btn normal`;
        document.body.className = '';
        document.querySelector('.tab-btn-persona').style.display = 'none';
        updateStatusBarMode("ノーマル");
        // ペルソナタブがアクティブなら切替
        if (document.getElementById('tab-persona').classList.contains('active')) {
            document.querySelector('.tab-btn[data-tab="chat"]').click();
        }
    }
}

modeBtn.addEventListener("click", () => {
    document.getElementById('persona-popup').style.display = 'flex';
    loadPersonaPopup();
});

// ペルソナポップアップ
document.querySelector('.persona-popup-close').addEventListener('click', () => {
    document.getElementById('persona-popup').style.display = 'none';
});
document.getElementById('persona-popup').addEventListener('click', (e) => {
    if (e.target.id === 'persona-popup') e.target.style.display = 'none';
});

async function loadPersonaPopup() {
    const list = document.getElementById('persona-popup-list');
    try {
        const resp = await fetch('/api/personas');
        const data = await resp.json();
        let html = `<div class="persona-popup-normal ${!activePersona ? 'active' : ''}" onclick="selectPersona(null)">ノーマルモード</div>`;
        for (const p of data.personas) {
            const active = activePersona && activePersona.id === p.id ? 'active' : '';
            html += `<div class="persona-popup-item ${active}" onclick="selectPersona(${p.id})">
                <span class="dot" style="background:${{purple:'#8b5cf6',blue:'#3b82f6',green:'#22c55e',orange:'#f97316',red:'#ef4444',pink:'#ec4899'}[p.color_theme]||'#8b5cf6'}"></span>
                <span class="name">${p.display_name}</span>
            </div>`;
        }
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = '<div style="color:#f08080">読み込みエラー</div>';
    }
}

async function selectPersona(id) {
    try {
        if (id === null) {
            await fetch('/api/personas/deactivate', { method: 'POST' });
            updateModeUI('normal', null);
        } else {
            const resp = await fetch(`/api/personas/${id}/activate`, { method: 'POST' });
            const data = await resp.json();
            if (data.active_persona) {
                updateModeUI('persona', data.active_persona);
            }
        }
        document.getElementById('persona-popup').style.display = 'none';
        updateStatus();
    } catch (e) {
        console.error("ペルソナ切替エラー:", e);
    }
}

document.getElementById('persona-create-btn').addEventListener('click', async () => {
    const name = document.getElementById('persona-new-name').value.trim();
    const displayName = document.getElementById('persona-new-display').value.trim();
    if (!name || !displayName) return;
    try {
        const resp = await fetch('/api/personas', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, display_name: displayName }),
        });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }
        document.getElementById('persona-new-name').value = '';
        document.getElementById('persona-new-display').value = '';
        loadPersonaPopup();
    } catch (e) {
        console.error("ペルソナ作成エラー:", e);
    }
});

// --- モデル選択 ---

const modelSelect = document.getElementById("model-select");

async function loadModels() {
    try {
        const resp = await fetch("/api/models");
        const data = await resp.json();
        modelSelect.innerHTML = "";

        if (data.models.length === 0) {
            modelSelect.innerHTML = '<option value="">モデルなし</option>';
            return;
        }

        for (const model of data.models) {
            const opt = document.createElement("option");
            opt.value = model;
            opt.textContent = model.length > 30 ? model.slice(0, 30) + "…" : model;
            opt.title = model;
            if (model === data.current) opt.selected = true;
            modelSelect.appendChild(opt);
        }
    } catch (e) {
        console.error("モデル一覧取得エラー:", e);
    }
}

modelSelect.addEventListener("change", async () => {
    const model = modelSelect.value;
    if (!model) return;
    try {
        await fetch("/api/models/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model }),
        });
    } catch (e) {
        console.error("モデル切替エラー:", e);
    }
});

// --- ステータスバー ---

const statusLLM = document.getElementById("status-llm");
const statusMemories = document.getElementById("status-memories");
const statusCountdown = document.getElementById("status-countdown");
const statusMode = document.getElementById("status-mode");

function updateStatusBarLLM(available) {
    statusLLM.textContent = available ? "LLM: 接続中" : "LLM: 未接続";
    statusLLM.style.color = available ? "#3fb950" : "#f08080";
}

function updateStatusBarMode(label) {
    statusMode.textContent = `モード: ${label}`;
    statusMode.style.color = activePersona ? "var(--accent-text, #a78bfa)" : "#8b949e";
}

function updateStatusBarCountdown(text) {
    statusCountdown.textContent = `次の自律: ${text}`;
}

async function updateStatus() {
    try {
        const resp = await fetch("/api/status");
        const data = await resp.json();

        updateStatusBarLLM(data.llm_available);
        statusMemories.textContent = `記憶: ${data.message_count}件`;
        if (data.mode) updateModeUI(data.mode, data.active_persona);
    } catch (e) {
        console.error("状態取得エラー:", e);
    }
}

async function loadMemories(query = "") {
    try {
        let items = [];

        if (query) {
            const resp = await fetch(`/api/memories/search?q=${encodeURIComponent(query)}`);
            const data = await resp.json();
            for (const m of (data.chat || [])) {
                items.push({ ...m, source: "chat" });
            }
            for (const m of (data.iku_logs || [])) {
                items.push({ ...m, source: "過去ログ" });
            }
        } else {
            const resp = await fetch("/api/memories/recent?limit=10");
            items = await resp.json();
        }

        memoryList.innerHTML = "";
        if (items.length === 0) {
            memoryList.innerHTML = '<div style="color:#484f58;font-size:13px;">記憶がありません</div>';
            return;
        }

        for (const mem of items) {
            const el = document.createElement("div");
            el.className = "memory-item";

            const date = mem.created_at ? mem.created_at.slice(0, 10) : "";
            const role = mem.role === "user" ? "ユーザー" : "イク";
            const source = mem.source || "chat";
            const preview = mem.content.length > 200 ? mem.content.slice(0, 200) + "…" : mem.content;

            el.innerHTML = `
                <div><span class="memory-role">${escapeHtml(role)}:</span> ${escapeHtml(preview)}</div>
                <div class="memory-meta">
                    <span>${date}</span>
                    <span class="memory-source">${escapeHtml(source)}</span>
                </div>
            `;
            memoryList.appendChild(el);
        }
    } catch (e) {
        console.error("記憶取得エラー:", e);
    }
}

function closeApprovalDialog() {
    // 承認待ちUIのボタンを無効化（タイムアウト時）
    document.querySelectorAll(".write-approval, .exec-approval, .create-tool-approval").forEach(el => {
        el.querySelectorAll("button").forEach(b => b.disabled = true);
        const fb = el.querySelector(".approval-feedback");
        if (fb) fb.disabled = true;
    });
}

function appendSystemMessage(text) {
    const el = document.createElement("div");
    el.className = "message system-notice";
    el.textContent = text;
    chatMessages.appendChild(el);
    scrollToBottom();
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML;
}

searchBtn.addEventListener("click", () => {
    loadMemories(memorySearch.value.trim());
});

memorySearch.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
        loadMemories(memorySearch.value.trim());
    }
});

// --- ログタブ ---

const logContent = document.getElementById("log-content");
const logFilterBtns = document.querySelectorAll(".log-filter-btn");
const logClearBtn = document.getElementById("log-clear-btn");
let logWs = null;
let activeLogFilter = "ALL";

logFilterBtns.forEach(btn => {
    btn.addEventListener("click", () => {
        logFilterBtns.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        activeLogFilter = btn.dataset.level;

        logContent.className = "log-content-full";
        if (activeLogFilter !== "ALL") {
            logContent.classList.add(`filter-${activeLogFilter}`);
        }
    });
});

logClearBtn.addEventListener("click", () => {
    logContent.innerHTML = "";
});

function connectLog() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    logWs = new WebSocket(`${protocol}//${location.host}/ws/logs`);

    logWs.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "log") {
            const line = document.createElement("div");
            line.className = `log-line ${data.level}`;
            line.textContent = data.msg;
            logContent.appendChild(line);
            while (logContent.children.length > 500) {
                logContent.removeChild(logContent.firstChild);
            }
            logContent.scrollTop = logContent.scrollHeight;
        }
    };

    logWs.onclose = () => {
        logWs = null;
        const logTab = document.getElementById("tab-log");
        if (logTab.classList.contains("active")) {
            setTimeout(connectLog, 3000);
        }
    };
}

// --- ファイル上書き承認UI ---

function showWriteApproval(data) {
    const el = document.createElement("div");
    el.className = "message write-approval";
    el.innerHTML = `
        <div class="write-approval-header">⚠ ファイル上書き承認: ${escapeHtml(data.path)}</div>
        <div class="write-approval-meta">${data.old_size}文字 → ${data.new_size}文字</div>
        <details><summary>変更前（先頭500文字）</summary><pre class="write-preview">${escapeHtml(data.old_content)}</pre></details>
        <details open><summary>変更後（先頭500文字）</summary><pre class="write-preview">${escapeHtml(data.new_content)}</pre></details>
        <div class="approval-feedback-area">
            <input class="approval-feedback" type="text" placeholder="コメント（任意）">
        </div>
        <div class="write-approval-buttons">
            <button class="write-btn approve">承認</button>
            <button class="write-btn reject">拒否</button>
        </div>
    `;
    chatMessages.appendChild(el);
    scrollToBottom();

    function disableAll() {
        el.querySelectorAll("button").forEach(b => b.disabled = true);
        el.querySelector(".approval-feedback").disabled = true;
    }

    el.querySelector(".approve").onclick = () => {
        const feedback = el.querySelector(".approval-feedback").value.trim();
        disableAll();
        ws.send(JSON.stringify({ type: "write_response", action: "approve", feedback }));
    };
    el.querySelector(".reject").onclick = () => {
        const feedback = el.querySelector(".approval-feedback").value.trim();
        disableAll();
        ws.send(JSON.stringify({ type: "write_response", action: "reject", feedback }));
    };
}

// --- リスク表示ヘルパー ---

function buildRiskHtml(data) {
    if (!data.risk_level) return "";
    const reasons = (data.risk_reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join("");
    const levelClass = data.risk_level.toLowerCase();
    return `
        <div class="risk-section risk-${levelClass}">
            <span class="risk-badge">${data.risk_emoji || ""} リスク: ${data.risk_level}</span>
            ${reasons ? `<ul class="risk-reasons">${reasons}</ul>` : ""}
        </div>
    `;
}

// --- コード実行承認UI ---

function showExecApproval(data) {
    const el = document.createElement("div");
    el.className = "message exec-approval";
    el.innerHTML = `
        <div class="exec-approval-header">⚠ コード実行承認</div>
        ${buildRiskHtml(data)}
        <details open><summary>実行するコード</summary><pre class="write-preview">${escapeHtml(data.code)}</pre></details>
        <div class="approval-feedback-area">
            <input class="approval-feedback" type="text" placeholder="コメント（任意）">
        </div>
        <div class="write-approval-buttons">
            <button class="write-btn approve">実行</button>
            <button class="write-btn reject">拒否</button>
        </div>
    `;
    chatMessages.appendChild(el);
    scrollToBottom();

    function disableAll() {
        el.querySelectorAll("button").forEach(b => b.disabled = true);
        el.querySelector(".approval-feedback").disabled = true;
    }

    el.querySelector(".approve").onclick = () => {
        const feedback = el.querySelector(".approval-feedback").value.trim();
        disableAll();
        ws.send(JSON.stringify({ type: "exec_response", action: "approve", feedback }));
    };
    el.querySelector(".reject").onclick = () => {
        const feedback = el.querySelector(".approval-feedback").value.trim();
        disableAll();
        ws.send(JSON.stringify({ type: "exec_response", action: "reject", feedback }));
    };
}

// --- ツール作成承認UI ---

function showCreateToolApproval(data) {
    const el = document.createElement("div");
    el.className = "message create-tool-approval";
    el.innerHTML = `
        <div class="create-tool-header">🔧 ツール作成承認: ${escapeHtml(data.name)}</div>
        <div class="create-tool-meta">
            <div><strong>説明:</strong> ${escapeHtml(data.description || "なし")}</div>
            <div><strong>引数:</strong> ${escapeHtml(data.args_desc || "なし")}</div>
        </div>
        ${buildRiskHtml(data)}
        <details open><summary>ツールのコード</summary><pre class="write-preview">${escapeHtml(data.code)}</pre></details>
        <div class="approval-feedback-area">
            <input class="approval-feedback" type="text" placeholder="コメント（任意）">
        </div>
        <div class="write-approval-buttons">
            <button class="write-btn approve">承認</button>
            <button class="write-btn reject">拒否</button>
        </div>
    `;
    chatMessages.appendChild(el);
    scrollToBottom();

    function disableAll() {
        el.querySelectorAll("button").forEach(b => b.disabled = true);
        el.querySelector(".approval-feedback").disabled = true;
    }

    el.querySelector(".approve").onclick = () => {
        const feedback = el.querySelector(".approval-feedback").value.trim();
        disableAll();
        ws.send(JSON.stringify({ type: "create_tool_response", action: "approve", feedback }));
    };
    el.querySelector(".reject").onclick = () => {
        const feedback = el.querySelector(".approval-feedback").value.trim();
        disableAll();
        ws.send(JSON.stringify({ type: "create_tool_response", action: "reject", feedback }));
    };
}

// --- ターミナルポップアップ ---

let currentTerminalEl = null;

function showExecTerminal(data) {
    const overlay = document.createElement("div");
    overlay.className = "exec-terminal-overlay";

    const win = document.createElement("div");
    win.className = "exec-terminal-window";
    win.innerHTML = `
        <div class="exec-terminal-titlebar">
            <div class="exec-terminal-dots">
                <span class="exec-dot-close"></span>
                <span class="exec-dot-min"></span>
                <span class="exec-dot-max"></span>
            </div>
            <span class="exec-terminal-titletext running">実行中...</span>
            <span class="exec-terminal-time"></span>
        </div>
        <div class="exec-terminal-body">
            <div class="exec-prompt">$ python -c</div>
            <pre class="exec-terminal-input">${escapeHtml(data.code)}</pre>
            <div class="exec-terminal-output"></div>
        </div>
        <div class="exec-terminal-statusbar">
            <span class="exec-status-text">実行中...</span>
            <span class="exec-status-time"></span>
        </div>
    `;

    if (data.backup) {
        const backupLine = document.createElement("div");
        backupLine.className = "exec-line system";
        backupLine.textContent = data.backup;
        win.querySelector(".exec-terminal-output").appendChild(backupLine);
    }

    overlay.appendChild(win);
    document.body.appendChild(overlay);
    currentTerminalEl = overlay;

    win.querySelector(".exec-dot-close").onclick = () => {
        overlay.remove();
        currentTerminalEl = null;
    };

    win.querySelector(".exec-dot-min").onclick = () => {
        overlay.remove();
        currentTerminalEl = null;
    };

    win.querySelector(".exec-terminal-titlebar").ondblclick = () => {
        win.classList.toggle("minimized");
    };
}

function appendExecOutput(data) {
    if (!currentTerminalEl) return;
    const output = currentTerminalEl.querySelector(".exec-terminal-output");
    if (!output) return;
    const line = document.createElement("div");
    line.className = `exec-line ${data.stream}`;
    line.textContent = data.content;
    output.appendChild(line);
    if (output.children.length > 500) {
        output.removeChild(output.firstChild);
    }
    const body = currentTerminalEl.querySelector(".exec-terminal-body");
    if (body) body.scrollTop = body.scrollHeight;
}

function finalizeExecTerminal(data) {
    if (!currentTerminalEl) return;
    const win = currentTerminalEl.querySelector(".exec-terminal-window");
    const titleText = currentTerminalEl.querySelector(".exec-terminal-titletext");
    const statusBar = currentTerminalEl.querySelector(".exec-terminal-statusbar");
    const statusText = currentTerminalEl.querySelector(".exec-status-text");
    const statusTime = currentTerminalEl.querySelector(".exec-status-time");
    const titleTime = currentTerminalEl.querySelector(".exec-terminal-time");

    if (data.return_code === 0) {
        titleText.textContent = "✓ 実行完了";
        titleText.className = "exec-terminal-titletext success";
        statusBar.className = "exec-terminal-statusbar success";
        statusText.textContent = "正常終了";
        win.style.borderColor = "#3fb950";
    } else {
        titleText.textContent = "✗ 実行失敗";
        titleText.className = "exec-terminal-titletext error";
        statusBar.className = "exec-terminal-statusbar error";
        statusText.textContent = `終了コード: ${data.return_code}`;
        win.classList.add("error");
    }
    statusTime.textContent = `${data.elapsed}秒`;
    titleTime.textContent = `${data.elapsed}秒`;

    const icon = data.return_code === 0 ? "✓" : "✗";
    addMessage("tool_result", `${icon} exec_code 完了 (${data.elapsed}秒)`);
}

// --- 動機エネルギー ---

const statusEnergy = document.getElementById("status-energy");

function updateMotivationEnergy(energy, threshold, breakdown) {
    const pct = threshold > 0 ? Math.min(100, Math.round(energy / threshold * 100)) : 0;
    statusEnergy.textContent = `⚡ ${energy}/${threshold}`;
    // 色で強度を表現
    if (pct >= 80) {
        statusEnergy.style.color = "#f06030";
    } else if (pct >= 50) {
        statusEnergy.style.color = "#d29922";
    } else {
        statusEnergy.style.color = "#8b5cf6";
    }
    // breakdown tooltip
    if (breakdown && Object.keys(breakdown).length > 0) {
        const lines = Object.entries(breakdown)
            .sort((a, b) => b[1] - a[1])
            .map(([t, v]) => `${t}: ${v}`);
        statusEnergy.title = `エネルギー内訳\n${lines.join("\n")}`;
    } else {
        statusEnergy.title = "内発的動機エネルギー";
    }
}

// --- カウントダウン ---

function startCountdown(seconds) {
    if (countdownInterval) clearInterval(countdownInterval);
    countdownRemaining = seconds;
    updateStatusBarCountdown(formatCountdown(countdownRemaining));
    countdownInterval = setInterval(() => {
        countdownRemaining--;
        if (countdownRemaining <= 0) {
            clearInterval(countdownInterval);
            countdownInterval = null;
            updateStatusBarCountdown("まもなく...");
        } else {
            updateStatusBarCountdown(formatCountdown(countdownRemaining));
        }
    }, 1000);
}

function formatCountdown(sec) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m > 0 ? `${m}分${s.toString().padStart(2, "0")}秒` : `${s}秒`;
}

// --- 開発用ツール ---

const devConcurrentToggle = document.getElementById("dev-concurrent");

devConcurrentToggle.addEventListener("change", async () => {
    try {
        await fetch("/api/dev/concurrent-mode", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: devConcurrentToggle.checked }),
        });
    } catch (e) {
        console.error("concurrent mode変更エラー:", e);
    }
});

const devIntervalInput = document.getElementById("dev-interval");
const devIntervalBtn = document.getElementById("dev-interval-btn");
const devTriggerBtn = document.getElementById("dev-trigger-btn");
const devResetBtn = document.getElementById("dev-reset-btn");
const devClearSelfModelBtn = document.getElementById("dev-clear-selfmodel-btn");

async function loadDevSettings() {
    try {
        const resp = await fetch("/api/dev/settings");
        const data = await resp.json();
        devIntervalInput.value = data.autonomous_interval;
        if (data.concurrent_mode !== undefined) {
            devConcurrentToggle.checked = data.concurrent_mode;
        }
        if (data.motivation_energy !== undefined) {
            const thr = data.motivation_threshold || 0;
            updateMotivationEnergy(data.motivation_energy, thr, data.energy_breakdown);
        }
        // Ablationフラグ同期
        if (data.ablation) {
            const map = { energy: "abl-energy", self_model: "abl-self-model", prediction: "abl-prediction", distillation: "abl-distillation" };
            for (const [key, id] of Object.entries(map)) {
                const el = document.getElementById(id);
                if (el) el.checked = !!data.ablation[key];
            }
        }
    } catch (e) {
        console.error("開発設定取得エラー:", e);
    }
}

// --- Ablationトグル ---
["energy", "self_model", "prediction", "distillation"].forEach(flag => {
    const id = flag === "self_model" ? "abl-self-model" : `abl-${flag}`;
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("change", async () => {
        try {
            await fetch("/api/dev/ablation", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ flag, enabled: el.checked }),
            });
        } catch (e) {
            console.error(`ablation ${flag} 変更エラー:`, e);
        }
    });
});

devIntervalBtn.addEventListener("click", async () => {
    const sec = parseInt(devIntervalInput.value);
    if (isNaN(sec) || sec < 10) return;
    devIntervalBtn.disabled = true;
    try {
        await fetch("/api/dev/autonomous-interval", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ seconds: sec }),
        });
        devIntervalBtn.textContent = "✓";
        setTimeout(() => { devIntervalBtn.textContent = "設定"; devIntervalBtn.disabled = false; }, 1000);
    } catch (e) {
        devIntervalBtn.disabled = false;
    }
});



devTriggerBtn.addEventListener("click", async () => {
    devTriggerBtn.disabled = true;
    devTriggerBtn.textContent = "実行中...";
    try {
        await fetch("/api/dev/autonomous-trigger", { method: "POST" });
    } catch (e) {
        console.error("即時実行エラー:", e);
    }
    setTimeout(() => { devTriggerBtn.textContent = "今すぐ自律行動"; devTriggerBtn.disabled = false; }, 3000);
});

devResetBtn.addEventListener("click", async () => {
    if (!confirm("過去ログ（iku_logs）以外の全データを削除します。よろしいですか？")) return;
    devResetBtn.disabled = true;
    devResetBtn.textContent = "リセット中...";
    try {
        await fetch("/api/dev/reset-db", { method: "POST" });
        devResetBtn.textContent = "✓ 完了";
        updateStatus();
        loadMemories();
        setTimeout(() => { devResetBtn.textContent = "DBリセット（過去ログ以外）"; devResetBtn.disabled = false; }, 2000);
    } catch (e) {
        devResetBtn.textContent = "エラー";
        setTimeout(() => { devResetBtn.textContent = "DBリセット（過去ログ以外）"; devResetBtn.disabled = false; }, 2000);
    }
});

devClearSelfModelBtn.addEventListener("click", async () => {
    if (!confirm("自己モデル（self_model.json）の内容をすべて削除します。よろしいですか？")) return;
    devClearSelfModelBtn.disabled = true;
    devClearSelfModelBtn.textContent = "クリア中...";
    try {
        await fetch("/api/dev/clear-self-model", { method: "POST" });
        devClearSelfModelBtn.textContent = "✓ 完了";
        loadSelfModel();
        setTimeout(() => { devClearSelfModelBtn.textContent = "自己モデルクリア"; devClearSelfModelBtn.disabled = false; }, 2000);
    } catch (e) {
        devClearSelfModelBtn.textContent = "エラー";
        setTimeout(() => { devClearSelfModelBtn.textContent = "自己モデルクリア"; devClearSelfModelBtn.disabled = false; }, 2000);
    }
});

// --- ベクトル再構築 ---

const devVectorReindexBtn = document.getElementById("dev-vector-reindex-btn");
if (devVectorReindexBtn) {
    devVectorReindexBtn.addEventListener("click", async () => {
        if (!confirm("全メッセージ・日記のベクトルを再構築します。時間がかかる場合があります。")) return;
        devVectorReindexBtn.disabled = true;
        devVectorReindexBtn.textContent = "再構築中...";
        try {
            const resp = await fetch("/api/dev/vector-reindex", { method: "POST" });
            const data = await resp.json();
            const msg = data.counts ? `メッセージ:${data.counts.messages} 日記:${data.counts.memory_summaries}` : "完了";
            devVectorReindexBtn.textContent = `✓ ${msg}`;
            setTimeout(() => { devVectorReindexBtn.textContent = "ベクトル再構築"; devVectorReindexBtn.disabled = false; }, 4000);
        } catch (e) {
            devVectorReindexBtn.textContent = "エラー";
            setTimeout(() => { devVectorReindexBtn.textContent = "ベクトル再構築"; devVectorReindexBtn.disabled = false; }, 2000);
        }
    });
}

// --- 自己モデル表示 ---

const selfmodelDisplay = document.getElementById("selfmodel-display");
const selfmodelRefreshBtn = document.getElementById("selfmodel-refresh-btn");

async function loadSelfModel() {
    try {
        const resp = await fetch("/api/dev/self-model");
        const data = await resp.json();
        if (Object.keys(data).length === 0) {
            selfmodelDisplay.textContent = "（空）";
        } else {
            selfmodelDisplay.textContent = JSON.stringify(data, null, 2);
        }
    } catch (e) {
        selfmodelDisplay.textContent = "取得エラー";
    }
}

selfmodelRefreshBtn.addEventListener("click", loadSelfModel);

// --- 自律度レポートタブ ---

const reportContent = document.getElementById("report-content");
const reportLoadBtn = document.getElementById("report-load-btn");
const reportFrom = document.getElementById("report-from");
const reportTo = document.getElementById("report-to");

reportLoadBtn.addEventListener("click", loadReport);

async function loadReport() {
    reportLoadBtn.disabled = true;
    reportLoadBtn.textContent = "集計中...";
    reportContent.innerHTML = '<div class="report-loading">集計中...</div>';
    try {
        const f = reportFrom.value || "2020-01-01";
        const t = reportTo.value || "2030-01-01";
        const resp = await fetch(`/api/autonomy-report?from=${f}&to=${t}`);
        const data = await resp.json();
        renderReport(data);
    } catch (e) {
        reportContent.innerHTML = `<div class="report-error">取得エラー: ${escapeHtml(e.message)}</div>`;
    } finally {
        reportLoadBtn.textContent = "集計";
        reportLoadBtn.disabled = false;
    }
}

function renderReport(data) {
    const s = data.summary;
    const m = data.metrics;

    const levelColors = {
        operator: "#6e7681", collaborator: "#8b949e", consultant: "#d29922",
        approver: "#3fb950", observer: "#58a6ff",
    };
    const levelLabels = {
        operator: "Operator（人間主導）", collaborator: "Collaborator（協働）",
        consultant: "Consultant（AI提案）", approver: "Approver（AI主導）",
        observer: "Observer（完全自律）",
    };

    const scoreColor = s.autonomy_score >= 0.6 ? "#3fb950" : s.autonomy_score >= 0.3 ? "#d29922" : "#8b949e";
    const barWidth = Math.round(s.autonomy_score * 100);

    let html = `
        <div class="report-summary">
            <div class="report-score-card">
                <div class="report-score" style="color:${scoreColor}">${s.autonomy_score.toFixed(3)}</div>
                <div class="report-level" style="color:${levelColors[s.autonomy_level] || '#8b949e'}">
                    ${levelLabels[s.autonomy_level] || s.autonomy_level}
                </div>
                <div class="report-score-bar"><div class="report-score-fill" style="width:${barWidth}%;background:${scoreColor}"></div></div>
                <div class="report-total">総行動数: ${s.total_actions}</div>
            </div>
        </div>
        <div class="report-grid">
    `;

    // 1. Autonomy Ratio
    const ar = m.autonomy_ratio;
    const arPct = ar.ratio > 0 ? Math.round(ar.ratio * 100) : 0;
    const tr = ar.trigger || {energy: 0, timer: 0, manual: 0, energy_ratio: 0};
    const energyPct = Math.round(tr.energy_ratio * 100);
    html += renderMetricCard("自律性比率", `${arPct}%`, `
        <div class="report-bar-pair">
            <div class="report-bar-row"><span>自律</span><div class="report-bar"><div class="report-bar-fill auto" style="width:${ar.autonomous + ar.chat > 0 ? Math.round(ar.autonomous / (ar.autonomous + ar.chat) * 100) : 0}%"></div></div><span>${ar.autonomous}</span></div>
            <div class="report-bar-row"><span>チャット</span><div class="report-bar"><div class="report-bar-fill chat" style="width:${ar.autonomous + ar.chat > 0 ? Math.round(ar.chat / (ar.autonomous + ar.chat) * 100) : 0}%"></div></div><span>${ar.chat}</span></div>
        </div>
        <div class="report-trigger-detail" style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.1)">
            <div style="font-size:11px;opacity:0.7;margin-bottom:4px">自律行動の内訳（エネルギー駆動率: ${energyPct}%）</div>
            <div class="report-bar-row"><span>エネルギー</span><div class="report-bar"><div class="report-bar-fill" style="width:${ar.autonomous > 0 ? Math.round(tr.energy / ar.autonomous * 100) : 0}%;background:#f59e0b"></div></div><span>${tr.energy}</span></div>
            <div class="report-bar-row"><span>タイマー</span><div class="report-bar"><div class="report-bar-fill" style="width:${ar.autonomous > 0 ? Math.round(tr.timer / ar.autonomous * 100) : 0}%;background:#6b7280"></div></div><span>${tr.timer}</span></div>
            ${tr.manual > 0 ? `<div class="report-bar-row"><span>手動</span><div class="report-bar"><div class="report-bar-fill" style="width:${ar.autonomous > 0 ? Math.round(tr.manual / ar.autonomous * 100) : 0}%;background:#8b5cf6"></div></div><span>${tr.manual}</span></div>` : ''}
        </div>
    `, "全行動のうち自律行動が占める割合。エネルギー駆動率は自律行動のうちタイマーではなく内発的動機で発火した割合");

    // 2. Tool Diversity
    const td = m.tool_diversity;
    const normEnt = td.max_entropy > 0 ? (td.entropy / td.max_entropy * 100).toFixed(0) : 0;
    const topTools = Object.entries(td.distribution).sort((a, b) => b[1] - a[1]).slice(0, 8);
    const maxCount = topTools.length > 0 ? topTools[0][1] : 1;
    let toolBars = topTools.map(([name, count]) =>
        `<div class="report-tool-row"><span class="report-tool-name">${escapeHtml(name)}</span><div class="report-bar"><div class="report-bar-fill tool" style="width:${Math.round(count / maxCount * 100)}%"></div></div><span>${count}</span></div>`
    ).join("");
    html += renderMetricCard("ツール多様性", `H=${td.entropy.toFixed(2)} (${normEnt}%)`, toolBars, "使用ツールのシャノンエントロピー。高いほど多様なツールを使い分けている。100%は全ツール均等使用");

    // 3. Self-Evolution
    const se = m.self_evolution;
    let evoBars = Object.entries(se.changes_by_key).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([key, count]) =>
        `<div class="report-tool-row"><span class="report-tool-name">${escapeHtml(key)}</span><span>${count}</span></div>`
    ).join("");
    html += renderMetricCard("自己進化", `${se.total_changes}回 / ${se.unique_keys}キー`, evoBars || '<div class="report-metric-empty">データなし</div>', "self_modelの更新回数と変更されたキーの種類数。AIが自己理解をどれだけ更新しているか");

    // 4. Metacognitive Accuracy
    const mc = m.metacognitive_accuracy;
    const mcPct = Math.round(mc.success_rate * 100);
    const avgSim = mc.avg_similarity != null ? `${Math.round(mc.avg_similarity * 100)}%` : "-";
    html += renderMetricCard("メタ認知精度", `${mcPct}%`, `
        <div class="report-stat-row"><span>予測回数</span><span>${mc.predictions_made}</span></div>
        <div class="report-stat-row"><span>的中率（類似度≥0.5）</span><span>${mcPct}%</span></div>
        <div class="report-stat-row"><span>平均類似度</span><span>${avgSim}</span></div>
    `, "予測テキスト(expect=)と実際の結果のベクトル類似度で判定。類似度0.5以上を的中とカウント");

    // 6. Memory Utilization
    const mu = m.memory_utilization;
    const muTotal = mu.memory_search + mu.memory_write + mu.action_search;
    html += renderMetricCard("記憶活用", `${muTotal}回`, `
        <div class="report-stat-row"><span>記憶検索</span><span>${mu.memory_search}</span></div>
        <div class="report-stat-row"><span>日記</span><span>${mu.memory_write}</span></div>
        <div class="report-stat-row"><span>行動検索</span><span>${mu.action_search}</span></div>
    `, "search_memories・write_diary・search_action_logの使用回数。AIが過去の経験をどれだけ活用しているか");

    // 7. Principle Accumulation
    const pa = m.principle_accumulation;
    html += renderMetricCard("原則蒸留", `${pa.current_principles}個`, `
        <div class="report-stat-row"><span>蒸留回数</span><span>${pa.distillation_count}</span></div>
        <div class="report-stat-row"><span>現在の原則数</span><span>${pa.current_principles}</span></div>
    `, "行動の振り返りから抽出されたprinciples（特性・傾向）の数。10件蓄積で二次蒸留により統合・圧縮される");

    // 8. Intent Coherence
    if (m.intent_coherence) {
        const ic = m.intent_coherence;
        const icPct = Math.round(ic.achievement_rate * 100);
        html += renderMetricCard("意図達成度", `${icPct}%`, `
            <div class="report-stat-row"><span>意図宣言数</span><span>${ic.intents_made}</span></div>
            <div class="report-stat-row"><span>達成(≥0.5)</span><span>${Math.round(ic.achievement_rate * ic.intents_made)} / ${ic.intents_made}</span></div>
            <div class="report-stat-row"><span>平均類似度</span><span>${ic.avg_similarity}</span></div>
        `, "intent=テキストと実行結果のベクトル類似度。行動が意図通りの結果を得たかの指標");
    }

    // 9. Tool Entropy Time-Series
    if (m.tool_entropy_ts && m.tool_entropy_ts.days.length > 0) {
        const te = m.tool_entropy_ts;
        const lastE = te.days[te.days.length - 1].entropy;
        html += renderMetricCard("エントロピー推移", `H=${lastE}`, renderSparkBars(te.days, "entropy", "#58a6ff"), "日ごとのツール使用エントロピーの推移。上昇傾向なら行動パターンが多様化している");
    }

    // 10. Prediction Accuracy Time-Series
    if (m.prediction_accuracy_ts && m.prediction_accuracy_ts.days.length > 0) {
        const pa2 = m.prediction_accuracy_ts;
        const lastR = Math.round(pa2.days[pa2.days.length - 1].rate * 100);
        html += renderMetricCard("予測精度推移", `${lastR}%`, renderSparkBars(pa2.days, "rate", "#3fb950"), "日ごとの予測成功率の推移。上昇傾向ならAIの予測能力が改善している");
    }

    // 11. Energy Efficiency
    if (m.energy_efficiency) {
        const ee = m.energy_efficiency;
        const eePct = Math.round(ee.avg_efficiency * 100);
        const eeColor = eePct >= 60 ? "#3fb950" : eePct >= 30 ? "#d29922" : "#f85149";
        html += renderMetricCard("エネルギー効率", `${eePct}%`, `
            <div class="report-stat-row"><span>平均効率</span><span style="color:${eeColor}">${eePct}%</span></div>
            <div class="report-stat-row"><span>セッション数</span><span>${ee.session_count}</span></div>
        `, "1セッション内でのユニークツール使用率。高いほど同じツールの繰り返しが少なく、エネルギーを効率的に使っている");
    }

    // 12. Self-Model Velocity
    if (m.self_model_velocity && m.self_model_velocity.days.length > 0) {
        const sv = m.self_model_velocity;
        html += renderMetricCard("自己モデル変化速度", `${sv.avg_per_day}/日`, renderSparkBars(sv.days, "count", "#d29922"), "日ごとのself_model更新回数。AIがどれだけ頻繁に自己理解を書き換えているか");
    }

    // 13. Session Length Trend
    if (m.session_length_trend && m.session_length_trend.days.length > 0) {
        const sl = m.session_length_trend;
        html += renderMetricCard("セッション長推移", `平均${sl.avg_length}`, renderSparkBars(sl.days, "avg_actions", "#a371f7"), "1セッションあたりの平均ツール実行数の推移。長いほど1回の行動が複雑になっている");
    }

    html += `</div>`;
    reportContent.innerHTML = html;
}

function renderSparkBars(days, valueKey, color = "#8b5cf6") {
    if (!days || days.length === 0) return '<div class="report-metric-empty">データなし</div>';
    const values = days.map(d => d[valueKey] || 0);
    const maxVal = Math.max(...values, 0.001);
    const bars = days.map((d, i) => {
        const h = Math.max(2, Math.round(values[i] / maxVal * 36));
        const tip = `${d.date || ""}: ${values[i]}`;
        return `<div class="sparkline-bar" style="height:${h}px;background:${color}" data-tip="${tip}"></div>`;
    }).join("");
    const label = days.length > 1
        ? `<div class="sparkline-label"><span>${days[0].date || ""}</span><span>${days[days.length - 1].date || ""}</span></div>`
        : "";
    return `<div class="sparkline">${bars}</div>${label}`;
}

function renderMetricCard(title, value, bodyHtml, tooltip) {
    const tip = tooltip ? `<span class="report-card-help" title="${escapeHtml(tooltip)}">?</span>` : "";
    return `
        <div class="report-card">
            <div class="report-card-header">
                <span class="report-card-title">${title}${tip}</span>
                <span class="report-card-value">${value}</span>
            </div>
            <div class="report-card-body">${bodyHtml}</div>
        </div>
    `;
}

// --- 蒸留ログ ---

const distillationContent = document.getElementById("distillation-content");
let distillationLoaded = false;

async function loadDistillationLog() {
    if (distillationLoaded) return;
    distillationContent.innerHTML = '<div class="report-loading">読み込み中...</div>';
    try {
        const resp = await fetch("/api/distillation-log?limit=20&offset=0");
        const data = await resp.json();
        renderDistillationLog(data);
        distillationLoaded = true;
    } catch (e) {
        distillationContent.innerHTML = `<div class="report-error">取得エラー: ${escapeHtml(e.message)}</div>`;
    }
}

function renderDistillationLog(data) {
    let html = "";

    // 現在の原則リスト
    if (data.current_principles && data.current_principles.length > 0) {
        html += '<div class="distillation-principles"><h4>現在の原則</h4><ul>';
        for (const p of data.current_principles) {
            const text = typeof p === "object" && p.text ? p.text : String(p);
            const date = typeof p === "object" && p.created ? p.created.slice(0, 16) : "";
            const consolidated = typeof p === "object" && p.consolidated;
            const badge = consolidated ? '<span class="badge-consolidated">統合</span>' : "";
            html += `<li class="distillation-principle-item${consolidated ? " consolidated" : ""}"><span>${badge}${escapeHtml(text)}</span>${date ? `<span class="distillation-principle-date">${date}</span>` : ""}</li>`;
        }
        html += '</ul></div>';
    }

    // セッション一覧
    html += `<div class="distillation-total">総セッション数: ${data.total}</div>`;
    html += '<div id="distillation-sessions">';
    for (const s of data.sessions) {
        html += buildDistillationSessionHtml(s);
    }
    html += '</div>';

    distillationContent.innerHTML = html;
}

function buildDistillationSessionHtml(s) {
    const sourceBadge = s.source === "autonomous"
        ? '<span class="badge-auto">自律</span>'
        : '<span class="badge-chat">チャット</span>';
    const triggerBadge = s.trigger
        ? `<span class="badge-trigger">${escapeHtml(s.trigger)}</span>`
        : "";
    const predBadge = s.has_predictions
        ? '<span class="badge-pred">予測あり</span>'
        : '<span class="badge-pred none">予測なし</span>';

    let roundsHtml = "";
    for (let i = 0; i < s.rounds.length; i++) {
        const r = s.rounds[i];
        const matchIcon = r.has_prediction
            ? (r.status === "success" ? '<span class="match-ok">○</span>' : '<span class="match-fail">×</span>')
            : '<span class="match-none">-</span>';
        const statusClass = r.status === "error" ? "distillation-round-error" : "";
        const intentLine = r.intent
            ? `<div class="distillation-intent">意図: ${escapeHtml(r.intent)}</div>`
            : "";
        const expectLine = r.expected
            ? `<div class="distillation-expect">予測: ${escapeHtml(r.expected)}</div>`
            : "";
        const rawLine = r.result_raw && r.result_raw !== r.result_summary
            ? `<pre>${escapeHtml(r.result_raw)}</pre>`
            : "";
        const detailContent = intentLine || expectLine || rawLine;
        const detailHtml = detailContent
            ? `<details class="distillation-raw"><summary>詳細</summary>${intentLine}${expectLine}${rawLine}</details>`
            : "";
        roundsHtml += `
            <div class="distillation-round ${statusClass}">
                <span class="distillation-round-num">#${i + 1}</span>
                ${matchIcon}
                <span class="distillation-round-tool">${escapeHtml(r.tool_name)}</span>
                <span class="distillation-round-status">${escapeHtml(r.status)}</span>
                <span class="distillation-round-summary">${escapeHtml(r.result_summary)}</span>
                ${detailHtml}
            </div>`;
    }

    const distillHtml = s.distillation_response
        ? `<details class="distillation-llm-response"><summary class="distillation-llm-label">蒸留応答</summary><pre>${escapeHtml(s.distillation_response)}</pre></details>`
        : "";

    return `
        <div class="distillation-session-wrapper" data-conv-id="${s.conv_id}">
            <details class="distillation-session">
                <summary>
                    <span class="distillation-session-time">${escapeHtml(s.started_at)}</span>
                    ${sourceBadge} ${triggerBadge} ${predBadge}
                    <span class="distillation-session-count">${s.round_count}ツール</span>
                </summary>
                <div class="distillation-session-body">
                    ${roundsHtml || '<div class="report-metric-empty">ツール実行なし</div>'}
                </div>
            </details>
            ${distillHtml}
        </div>`;
}

function prependDistillationSession(session) {
    const container = document.getElementById("distillation-sessions");
    if (!container) {
        // ログがまだ初回読み込みされていない場合は初回読み込みを実行
        distillationLoaded = false;
        loadDistillationLog();
        return;
    }
    const html = buildDistillationSessionHtml(session);
    container.insertAdjacentHTML("afterbegin", html);

    // 総セッション数を更新
    const totalEl = distillationContent.querySelector(".distillation-total");
    if (totalEl) {
        const match = totalEl.textContent.match(/\d+/);
        if (match) {
            totalEl.textContent = `総セッション数: ${parseInt(match[0]) + 1}`;
        }
    }
}

function updateDistillationResponse(convId, response, principle) {
    const wrapper = distillationContent.querySelector(`.distillation-session-wrapper[data-conv-id="${convId}"]`);
    if (!wrapper) return;

    // 既存の蒸留応答を削除（あれば）
    const existing = wrapper.querySelector(".distillation-llm-response");
    if (existing) existing.remove();

    // 新しい蒸留応答を追加
    if (response) {
        const distillHtml = `<details class="distillation-llm-response"><summary class="distillation-llm-label">蒸留応答</summary><pre>${escapeHtml(response)}</pre></details>`;
        wrapper.insertAdjacentHTML("beforeend", distillHtml);
    }
}

// --- ペルソナタブ ---

async function loadPersonaTab(personaId) {
    if (!personaId) return;
    try {
        // 詳細取得
        const resp = await fetch(`/api/personas/${personaId}`);
        const p = await resp.json();

        document.getElementById('persona-title').textContent = p.display_name;

        // 統計
        document.getElementById('persona-stats').innerHTML = `
            <span class="persona-stat"><strong>${p.episode_count}</strong> エピソード</span>
            <span class="persona-stat"><strong>${p.message_count}</strong> メッセージ</span>
            <span class="persona-stat"><strong>${p.diary_count}</strong> 日記</span>
        `;

        // テーマピッカー更新
        document.querySelectorAll('#persona-theme-picker .theme-dot').forEach(dot => {
            dot.classList.toggle('active', dot.dataset.theme === p.color_theme);
        });

        // self_model
        const smResp = await fetch(`/api/personas/${personaId}/self-model`);
        const sm = await smResp.json();
        const smEmpty = !sm || Object.keys(sm).length === 0;
        if (smEmpty) {
            // 空の場合はテンプレートを表示
            document.getElementById('persona-selfmodel').value = JSON.stringify({
                "__free_text__": "",
                "drives": {},
                "strategies": {},
                "motivation_rules": {
                    "weights": {},
                    "action_costs": {},
                    "threshold": null,
                    "decay_per_check": 5
                }
            }, null, 2);
        } else {
            document.getElementById('persona-selfmodel').value = JSON.stringify(sm, null, 2);
        }

        // エピソード一覧
        const epResp = await fetch(`/api/personas/${personaId}/episodes?limit=50`);
        const epData = await epResp.json();
        const epList = document.getElementById('persona-episode-list');
        if (epData.episodes && epData.episodes.length > 0) {
            epList.innerHTML = epData.episodes.map(e =>
                `<div class="persona-episode-item"><span class="role">${e.role}</span> ${e.content}</div>`
            ).join('') + `<div style="color:#8b949e;padding:6px 10px;font-size:11px">全${epData.total}件</div>`;
        } else {
            epList.innerHTML = '<div style="color:#484f58;padding:10px">エピソードなし</div>';
        }
    } catch (e) {
        console.error("ペルソナタブ読み込みエラー:", e);
    }
}

// テーマ切替
document.getElementById('persona-theme-picker').addEventListener('click', async (e) => {
    const dot = e.target.closest('.theme-dot');
    if (!dot || !activePersona) return;
    const theme = dot.dataset.theme;
    await fetch(`/api/personas/${activePersona.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ color_theme: theme }),
    });
    activePersona.color_theme = theme;
    document.body.className = `theme-${theme}`;
    document.querySelectorAll('#persona-theme-picker .theme-dot').forEach(d => {
        d.classList.toggle('active', d.dataset.theme === theme);
    });
});

// self_model保存
document.getElementById('persona-selfmodel-save').addEventListener('click', async () => {
    if (!activePersona) return;
    try {
        const val = document.getElementById('persona-selfmodel').value;
        const content = JSON.parse(val);
        await fetch(`/api/personas/${activePersona.id}/self-model`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });
        alert('保存しました');
    } catch (e) {
        alert('JSONパースエラー: ' + e.message);
    }
});

// エピソードインポート
document.getElementById('persona-episode-import').addEventListener('click', async () => {
    if (!activePersona) return;
    const fileInput = document.getElementById('persona-episode-files');
    if (!fileInput.files.length) return;
    const status = document.getElementById('persona-episode-status');
    status.textContent = 'インポート中...';
    const formData = new FormData();
    for (const f of fileInput.files) formData.append('files', f);
    try {
        const resp = await fetch(`/api/personas/${activePersona.id}/episodes/import`, {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        status.textContent = `${data.count}件インポート完了`;
        fileInput.value = '';
        loadPersonaTab(activePersona.id);
    } catch (e) {
        status.textContent = 'エラー: ' + e.message;
    }
});

// ペルソナ削除
document.getElementById('persona-delete-btn').addEventListener('click', async () => {
    if (!activePersona) return;
    if (!confirm(`ペルソナ「${activePersona.display_name}」と全データを削除しますか？`)) return;
    await fetch(`/api/personas/${activePersona.id}`, { method: 'DELETE' });
    updateModeUI('normal', null);
    updateStatus();
});

// エピソード全削除
document.getElementById('persona-episodes-clear-btn').addEventListener('click', async () => {
    if (!activePersona) return;
    if (!confirm('全エピソードを削除しますか？')) return;
    await fetch(`/api/personas/${activePersona.id}/episodes`, { method: 'DELETE' });
    loadPersonaTab(activePersona.id);
});

// --- 初期化 ---

connect();
updateStatus();
loadModels();
loadMemories();
loadDevSettings();
loadSelfModel();

setInterval(updateStatus, 30000);
