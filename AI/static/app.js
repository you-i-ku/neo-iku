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
            case "processing_start":
                showProcessingIndicator();
                break;

            case "processing_end":
                hideProcessingIndicator();
                break;

            case "output":
                hideProcessingIndicator();
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

            case "write_approval":
                showWriteApproval(data);
                break;

            case "exec_approval":
                showExecApproval(data);
                break;

            case "create_tool_approval":
                showCreateToolApproval(data);
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
                updateMotivationEnergy(data.energy, data.threshold);
                break;

            case "user_interrupt_ack":
                addMessage("system", `💬 割り込みメッセージを受け付けました: ${data.content}`);
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
    thoughtLog.scrollTop = thoughtLog.scrollHeight;
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
    thoughtLog.scrollTop = thoughtLog.scrollHeight;
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
    thoughtLog.scrollTop = thoughtLog.scrollHeight;
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
    thoughtLog.scrollTop = thoughtLog.scrollHeight;
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
    thoughtLog.scrollTop = thoughtLog.scrollHeight;
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

// --- モード切替 ---

function updateModeUI(mode) {
    currentMode = mode;
    modeBtn.textContent = mode === "iku" ? "イク" : "ノーマル";
    modeBtn.className = `mode-btn ${mode}`;
    updateStatusBarMode(mode);
}

modeBtn.addEventListener("click", async () => {
    const newMode = currentMode === "iku" ? "normal" : "iku";
    modeBtn.disabled = true;
    try {
        const resp = await fetch("/api/mode", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: newMode }),
        });
        const data = await resp.json();
        updateModeUI(data.mode);
        if (data.import) {
            updateStatus();
        }
    } catch (e) {
        console.error("モード切替エラー:", e);
    } finally {
        modeBtn.disabled = false;
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

function updateStatusBarMode(mode) {
    statusMode.textContent = `モード: ${mode === "iku" ? "イク" : "ノーマル"}`;
    statusMode.style.color = mode === "iku" ? "#a78bfa" : "#8b949e";
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
        if (data.mode) updateModeUI(data.mode);
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

function updateMotivationEnergy(energy, threshold) {
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
const devRoundsInput = document.getElementById("dev-rounds");
const devRoundsBtn = document.getElementById("dev-rounds-btn");
const devTriggerBtn = document.getElementById("dev-trigger-btn");
const devResetBtn = document.getElementById("dev-reset-btn");
const devClearSelfModelBtn = document.getElementById("dev-clear-selfmodel-btn");

async function loadDevSettings() {
    try {
        const resp = await fetch("/api/dev/settings");
        const data = await resp.json();
        devIntervalInput.value = data.autonomous_interval;
        devRoundsInput.value = data.tool_max_rounds;
        if (data.concurrent_mode !== undefined) {
            devConcurrentToggle.checked = data.concurrent_mode;
        }
        if (data.motivation_energy !== undefined) {
            statusEnergy.textContent = `⚡ ${data.motivation_energy}`;
        }
    } catch (e) {
        console.error("開発設定取得エラー:", e);
    }
}

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

devRoundsBtn.addEventListener("click", async () => {
    const rounds = parseInt(devRoundsInput.value);
    if (isNaN(rounds) || rounds < 1) return;
    devRoundsBtn.disabled = true;
    try {
        await fetch("/api/dev/tool-max-rounds", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rounds }),
        });
        devRoundsBtn.textContent = "✓";
        setTimeout(() => { devRoundsBtn.textContent = "設定"; devRoundsBtn.disabled = false; }, 1000);
    } catch (e) {
        devRoundsBtn.disabled = false;
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

// --- 初期化 ---

connect();
updateStatus();
loadModels();
loadMemories();
loadDevSettings();
loadSelfModel();

setInterval(updateStatus, 30000);
