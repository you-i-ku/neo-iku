// neo-iku フロントエンド

const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const connectionStatus = document.getElementById("connection-status");
const memorySearch = document.getElementById("memory-search");
const searchBtn = document.getElementById("search-btn");
const memoryList = document.getElementById("memory-list");
const modeBtn = document.getElementById("mode-btn");
const chatTitle = document.getElementById("chat-title");

let ws = null;
let currentStreamEl = null;
let currentThinkEl = null;
let thinkDotInterval = null;
let currentMode = "normal";
let countdownInterval = null;
let countdownRemaining = 0;

// --- WebSocket ---

function connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws/chat`);

    ws.onopen = () => {
        connectionStatus.className = "status-dot online";
        sendBtn.disabled = false;
    };

    ws.onclose = () => {
        connectionStatus.className = "status-dot offline";
        sendBtn.disabled = true;
        setTimeout(connect, 3000);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        switch (data.type) {
            case "think_start":
                currentThinkEl = addThinkBlock();
                startThinkDots();
                scrollToBottom();
                break;

            case "think":
                if (currentThinkEl) {
                    currentThinkEl.querySelector(".think-content").textContent += data.content;
                    scrollToBottom();
                }
                break;

            case "think_end":
                stopThinkDots();
                currentThinkEl = null;
                break;

            case "stream":
                if (!currentStreamEl) {
                    currentStreamEl = addMessage("assistant", "");
                }
                currentStreamEl.textContent += data.content;
                scrollToBottom();
                break;

            case "stream_end":
                currentStreamEl = null;
                loadMemories(memorySearch.value.trim());
                break;

            case "error":
                addMessage("error", data.content);
                currentStreamEl = null;
                currentThinkEl = null;
                break;

            case "tool_call":
                addMessage("tool_call", data.content);
                break;

            case "tool_result":
                addMessage("tool_result", data.content);
                loadMemories(memorySearch.value.trim());
                break;

            case "write_approval":
                showWriteApproval(data);
                break;

            case "exec_approval":
                showExecApproval(data);
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
                updateAutonomousToolStatus(data.name);
                break;

            case "autonomous_think_start":
                updateCountdownDisplay("行動中...");
                startAutonomousThink();
                break;

            case "autonomous_think_end":
                stopAutonomousThink();
                break;

            case "autonomous":
                addAutonomousMessage(data.content);
                loadMemories(memorySearch.value.trim());
                break;
        }
    };
}

function addThinkBlock() {
    const el = document.createElement("div");
    el.className = "message think-block";
    el.innerHTML = `<details><summary class="think-summary">think.</summary><div class="think-content"></div></details>`;
    chatMessages.appendChild(el);
    scrollToBottom();
    return el;
}

function startThinkDots() {
    stopThinkDots();
    let dotCount = 1;
    thinkDotInterval = setInterval(() => {
        if (!currentThinkEl) { stopThinkDots(); return; }
        dotCount = (dotCount % 3) + 1;
        const summary = currentThinkEl.querySelector(".think-summary");
        if (summary) summary.textContent = "think" + ".".repeat(dotCount);
    }, 400);
}

function stopThinkDots() {
    if (thinkDotInterval) {
        clearInterval(thinkDotInterval);
        thinkDotInterval = null;
    }
    if (currentThinkEl) {
        const summary = currentThinkEl.querySelector(".think-summary");
        if (summary) summary.textContent = "think";
    }
}

function addMessage(type, content) {
    const el = document.createElement("div");
    el.className = `message ${type}`;
    el.textContent = content;
    chatMessages.appendChild(el);
    scrollToBottom();
    return el;
}

let autonomousThinkEl = null;
let autonomousThinkInterval = null;

function startAutonomousThink() {
    // 既存のthinkブロックがあればクリーンアップ
    stopAutonomousThink();

    const el = document.createElement("div");
    el.className = "message think-block autonomous-think";
    el.innerHTML = `<div class="think-summary-static">think.</div>`;
    chatMessages.appendChild(el);
    autonomousThinkEl = el;

    // ドットアニメーション
    let dotCount = 1;
    autonomousThinkInterval = setInterval(() => {
        if (!autonomousThinkEl) { stopAutonomousThink(); return; }
        dotCount = (dotCount % 3) + 1;
        const label = autonomousThinkEl.querySelector(".think-summary-static");
        if (label) label.textContent = "think" + ".".repeat(dotCount);
    }, 400);

    scrollToBottom();
}

function updateAutonomousToolStatus(toolName) {
    // thinkラベルを一時的にツール名に
    if (autonomousThinkEl) {
        const label = autonomousThinkEl.querySelector(".think-summary-static");
        if (label) label.textContent = `⚙ ${toolName} 実行中...`;
    }
    // ツール使用履歴をメッセージとして残す
    addMessage("autonomous-tool", `⚙ ${toolName} を使用しました`);
    scrollToBottom();
}

function stopAutonomousThink() {
    if (autonomousThinkInterval) {
        clearInterval(autonomousThinkInterval);
        autonomousThinkInterval = null;
    }
    // thinkブロックを消す（本文がポンと出るので不要になる）
    if (autonomousThinkEl) {
        autonomousThinkEl.remove();
        autonomousThinkEl = null;
    }
}

function addAutonomousMessage(content) {
    // think終了がまだなら消す
    stopAutonomousThink();

    // <think>タグを除去して本文だけポンと表示
    const clean = content.replace(/<think>[\s\S]*?<\/think>/g, "").trim();
    if (clean) {
        addMessage("autonomous", clean);
    }
    scrollToBottom();
}

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

function sendMessage() {
    const text = chatInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    addMessage("user", text);
    ws.send(JSON.stringify({ message: text }));
    chatInput.value = "";
    chatInput.style.height = "auto";
    userScrolledUp = false;
    scrollToBottom(true);
    // 送信したメッセージに関連する記憶を即表示
    loadMemories(text);
}

// --- イベント ---

sendBtn.addEventListener("click", sendMessage);

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
    chatTitle.textContent = mode === "iku" ? "イク" : "チャット";
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
        // イクモードに切り替えた時にインポートされた場合
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

// --- ダッシュボード ---

async function updateStatus() {
    try {
        const resp = await fetch("/api/status");
        const data = await resp.json();

        document.getElementById("llm-status").textContent =
            data.llm_available ? "✓ 接続中" : "✗ 未接続";
        document.getElementById("llm-status").style.color =
            data.llm_available ? "#3fb950" : "#f08080";
        document.getElementById("message-count").textContent = data.message_count;
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
            // 検索結果はchatとiku_logsに分かれる
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
            memoryList.innerHTML = '<div class="small-text">記憶がありません</div>';
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

// 検索
searchBtn.addEventListener("click", () => {
    loadMemories(memorySearch.value.trim());
});

memorySearch.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
        loadMemories(memorySearch.value.trim());
    }
});

// --- ログパネル ---

const logToggle = document.getElementById("log-toggle");
const logBody = document.getElementById("log-body");
const logArrow = document.getElementById("log-arrow");
const logContent = document.getElementById("log-content");
let logWs = null;
let logOpen = false;

const container = document.querySelector(".container");

logToggle.addEventListener("click", () => {
    logOpen = !logOpen;
    logBody.style.display = logOpen ? "block" : "none";
    logArrow.textContent = logOpen ? "▼" : "▲";
    container.style.height = logOpen ? "calc(100vh - 212px)" : "calc(100vh - 32px)";
    if (logOpen && !logWs) connectLog();
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
            // 最大500行
            while (logContent.children.length > 500) {
                logContent.removeChild(logContent.firstChild);
            }
            logContent.scrollTop = logContent.scrollHeight;
        }
    };

    logWs.onclose = () => {
        logWs = null;
        if (logOpen) setTimeout(connectLog, 3000);
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
        <div class="write-approval-buttons">
            <button class="write-btn approve">承認</button>
            <button class="write-btn reject">拒否</button>
            <button class="write-btn review">検討</button>
        </div>
        <div class="write-review-area" style="display:none">
            <textarea class="write-review-input" placeholder="フィードバックを入力..." rows="3"></textarea>
            <button class="write-btn send-review">送信</button>
        </div>
    `;
    chatMessages.appendChild(el);
    scrollToBottom();

    function disableAll() {
        el.querySelectorAll("button").forEach(b => b.disabled = true);
    }

    el.querySelector(".approve").onclick = () => {
        disableAll();
        ws.send(JSON.stringify({ type: "write_response", action: "approve" }));
    };
    el.querySelector(".reject").onclick = () => {
        disableAll();
        ws.send(JSON.stringify({ type: "write_response", action: "reject" }));
    };
    el.querySelector(".review").onclick = () => {
        el.querySelector(".write-review-area").style.display = "flex";
        el.querySelector(".write-review-input").focus();
    };
    el.querySelector(".send-review").onclick = () => {
        const msg = el.querySelector(".write-review-input").value.trim();
        if (!msg) return;
        disableAll();
        ws.send(JSON.stringify({ type: "write_response", action: "review", message: msg }));
    };
}

// --- コード実行承認UI ---

function showExecApproval(data) {
    const el = document.createElement("div");
    el.className = "message exec-approval";
    el.innerHTML = `
        <div class="exec-approval-header">⚠ コード実行承認</div>
        <details open><summary>実行するコード</summary><pre class="write-preview">${escapeHtml(data.code)}</pre></details>
        <div class="write-approval-buttons">
            <button class="write-btn approve">実行</button>
            <button class="write-btn reject">拒否</button>
            <button class="write-btn review">検討</button>
        </div>
        <div class="write-review-area" style="display:none">
            <textarea class="write-review-input" placeholder="フィードバックを入力..." rows="3"></textarea>
            <button class="write-btn send-review">送信</button>
        </div>
    `;
    chatMessages.appendChild(el);
    scrollToBottom();

    function disableAll() {
        el.querySelectorAll("button").forEach(b => b.disabled = true);
    }

    el.querySelector(".approve").onclick = () => {
        disableAll();
        ws.send(JSON.stringify({ type: "exec_response", action: "approve" }));
    };
    el.querySelector(".reject").onclick = () => {
        disableAll();
        ws.send(JSON.stringify({ type: "exec_response", action: "reject" }));
    };
    el.querySelector(".review").onclick = () => {
        el.querySelector(".write-review-area").style.display = "flex";
        el.querySelector(".write-review-input").focus();
    };
    el.querySelector(".send-review").onclick = () => {
        const msg = el.querySelector(".write-review-input").value.trim();
        if (!msg) return;
        disableAll();
        ws.send(JSON.stringify({ type: "exec_response", action: "review", message: msg }));
    };
}

// --- ターミナルポップアップ ---

let currentTerminalEl = null;

function showExecTerminal(data) {
    // オーバーレイ
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

    // 閉じるボタン（赤丸）
    win.querySelector(".exec-dot-close").onclick = () => {
        overlay.remove();
        currentTerminalEl = null;
    };

    // 最小化ボタン（黄丸）— ポップアップを閉じてチャット内にサマリーを残す
    win.querySelector(".exec-dot-min").onclick = () => {
        overlay.remove();
        currentTerminalEl = null;
    };

    // タイトルバーダブルクリックで最小化/復元
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
    // 出力エリアを自動スクロール
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

    // チャット内にもサマリーを残す
    const icon = data.return_code === 0 ? "✓" : "✗";
    addMessage("tool_result", `${icon} exec_code 完了 (${data.elapsed}秒)`);
}

// --- カウントダウン ---

const countdownEl = document.getElementById("autonomous-countdown");

function startCountdown(seconds) {
    if (countdownInterval) clearInterval(countdownInterval);
    countdownRemaining = seconds;
    updateCountdownDisplay(formatCountdown(countdownRemaining));
    countdownInterval = setInterval(() => {
        countdownRemaining--;
        if (countdownRemaining <= 0) {
            clearInterval(countdownInterval);
            countdownInterval = null;
            updateCountdownDisplay("まもなく...");
        } else {
            updateCountdownDisplay(formatCountdown(countdownRemaining));
        }
    }, 1000);
}

function formatCountdown(sec) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m > 0 ? `${m}分${s.toString().padStart(2, "0")}秒` : `${s}秒`;
}

function updateCountdownDisplay(text) {
    if (countdownEl) countdownEl.textContent = text;
}

// --- 開発用ツール ---

const devIntervalInput = document.getElementById("dev-interval");
const devIntervalBtn = document.getElementById("dev-interval-btn");
const devRoundsInput = document.getElementById("dev-rounds");
const devRoundsBtn = document.getElementById("dev-rounds-btn");
const devTriggerBtn = document.getElementById("dev-trigger-btn");
const devResetBtn = document.getElementById("dev-reset-btn");

async function loadDevSettings() {
    try {
        const resp = await fetch("/api/dev/settings");
        const data = await resp.json();
        devIntervalInput.value = data.autonomous_interval;
        devRoundsInput.value = data.tool_max_rounds;
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

// --- 初期化 ---

connect();
updateStatus();
loadModels();
loadMemories();
loadDevSettings();

setInterval(updateStatus, 30000);
