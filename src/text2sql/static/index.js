const messagesEl = document.getElementById("messages");
const questionEl = document.getElementById("question");
const statusEl = document.getElementById("status");
const askBtn = document.getElementById("askBtn");
const clearBtn = document.getElementById("clearBtn");
const trainingReportEl = document.getElementById("trainingReport");
const systemStatusEl = document.getElementById("systemStatus");
const statusUpdatedAtEl = document.getElementById("statusUpdatedAt");
const reloadReportBtn = document.getElementById("reloadReportBtn");
const newChatBtn = document.getElementById("newChatBtn");
const executeModeBtn = document.getElementById("executeModeBtn");
const generateModeBtn = document.getElementById("generateModeBtn");
const menuBtn = document.getElementById("menuBtn");
const sidebar = document.getElementById("sidebar");
const sidebarBackdrop = document.getElementById("sidebarBackdrop");
const conversationPreview = document.getElementById("conversationPreview");
const reportBtn = document.getElementById("reportBtn");
const reportDrawer = document.getElementById("reportDrawer");
const reportBackdrop = document.getElementById("reportBackdrop");
const closeReportBtn = document.getElementById("closeReportBtn");
const authDialog = document.getElementById("authDialog");
const authForm = document.getElementById("authForm");
const authKey = document.getElementById("authKey");
const authError = document.getElementById("authError");
const authCancelBtn = document.getElementById("authCancelBtn");
let chatHistory = [];
let executeMode = true;

function requestAuthentication() {
    if (!authDialog.open) {
        authError.textContent = "";
        authKey.value = "";
        authDialog.showModal();
        setTimeout(() => authKey.focus(), 0);
    }
}

async function createBrowserSession(apiKey) {
    const response = await fetch("/auth/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey }),
    });
    if (!response.ok) {
        throw new Error(response.status === 401 ? "访问密钥无效" : "浏览器会话功能尚未配置");
    }
    return response.json();
}

function generateEntryId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
        return globalThis.crypto.randomUUID();
    }
    return `entry-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function formatTime(isoString) {
    const date = new Date(isoString);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

class ApiRequestError extends Error {
    constructor(message, detail = "", status = 0) {
        super(message);
        this.detail = detail;
        this.status = status;
    }
}

async function fetchJson(url, options = {}) {
    let response;
    try {
        response = await fetch(url, options);
    } catch (error) {
        throw new ApiRequestError(
            "无法连接后端服务",
            "请确认 Text2SQL 服务已经启动且网络连接正常。",
        );
    }

    const raw = await response.text();
    if (!raw.trim()) {
        throw new ApiRequestError(
            "服务暂时没有返回数据",
            `接口 ${url} 返回了空响应（HTTP ${response.status}），请稍后刷新。`,
            response.status,
        );
    }

    let payload;
    try {
        payload = JSON.parse(raw);
    } catch (error) {
        throw new ApiRequestError(
            "服务响应格式异常",
            `接口 ${url} 未返回有效的数据格式（HTTP ${response.status}）。`,
            response.status,
        );
    }

    if (!response.ok) {
        if (response.status === 401) {
            requestAuthentication();
        }
        const message = response.status === 401
            ? "没有权限读取状态"
            : response.status >= 500
                ? "后端服务处理失败"
                : "状态请求失败";
        throw new ApiRequestError(message, payload.error || `HTTP ${response.status}`, response.status);
    }
    return payload;
}

function renderStatusError(error, compact = false) {
    const title = error instanceof ApiRequestError ? error.message : "状态读取失败";
    const detail = error instanceof ApiRequestError
        ? error.detail
        : "发生了未预期的问题，请稍后刷新或检查后端日志。";
    return `<div class="status-error ${compact ? "compact-error" : ""}">
        <div class="status-error-head"><span class="health-indicator error"></span><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div></div>
        <div class="status-error-actions">可尝试点击上方“刷新”重新检查</div>
    </div>`;
}

function loadHistory() {
    // 查询结果可能包含业务数据，仅保留在当前页面内存中。
    chatHistory = [];
}

function saveHistory() {
    // 有意不写入浏览器持久化存储。
}

function scoreLabel(tableName, scores) {
    const score = scores && tableName in scores ? scores[tableName] : null;
    return score === null ? "" : `<span style="opacity:.7">(${escapeHtml(score)})</span>`;
}

function renderChips(tables, scores) {
    if (!tables || !tables.length) {
        return '<div class="text-block">未命中候选表</div>';
    }
    return `<div class="chips">${tables.map((table) =>
        `<span class="chip">${escapeHtml(table)} ${scoreLabel(table, scores)}</span>`
    ).join("")}</div>`;
}

function renderResultTable(rows, columns) {
    if (!rows || !rows.length || !columns || !columns.length) {
        return '<div class="text-block">无执行结果</div>';
    }
    const header = columns.map((name) => `<th>${escapeHtml(name)}</th>`).join("");
    const body = rows.map((row) =>
        `<tr>${columns.map((name) => `<td>${escapeHtml(row[name] ?? "")}</td>`).join("")}</tr>`
    ).join("");
    return `<div class="table-wrap"><table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderInlineChips(items) {
    if (!items || !items.length) {
        return '<div class="text-block">无</div>';
    }
    return `<div class="chips">${items.map((item) =>
        `<span class="chip">${escapeHtml(item)}</span>`
    ).join("")}</div>`;
}

function feedbackStatusLabel(feedback) {
    if (!feedback || feedback.status !== "submitted") {
        return "";
    }
    return feedback.label === "correct" ? "已标记为正确" : "已标记为错误";
}

function renderValidationActions(entry) {
    const payload = entry.payload || {};
    if (!payload.sql) {
        return "";
    }
    const feedback = payload.validation_feedback || null;
    const disabledAttr = feedback && feedback.status === "submitted" ? "disabled" : "";
    const feedbackMessage = feedback && feedback.status === "submitted"
        ? `<div class="feedback-status ${feedback.label === "correct" ? "success" : "error"}">${escapeHtml(feedbackStatusLabel(feedback))}${feedback.comment ? `：${escapeHtml(feedback.comment)}` : ""}</div>`
        : '<div class="feedback-status subtle">可在线标记当前 SQL 是否正确。</div>';
    return `
        <div class="section">
            <div class="section-title">在线验证反馈</div>
            <div class="feedback-actions" data-entry-id="${escapeHtml(entry.id)}">
                <button type="button" class="secondary feedback-btn" data-label="correct" ${disabledAttr}>标记正确</button>
                <button type="button" class="secondary feedback-btn" data-label="incorrect" ${disabledAttr}>标记错误</button>
            </div>
            ${feedbackMessage}
        </div>
    `;
}

function renderTypeBreakdown(recordTypes) {
    const entries = Object.entries(recordTypes || {});
    if (!entries.length) {
        return '<div class="text-block">暂无训练记录分类</div>';
    }
    return `<div class="kv-list">${entries
        .sort((left, right) => right[1] - left[1])
        .slice(0, 8)
        .map(([name, count]) => `<div class="kv-row"><span>${escapeHtml(name)}</span><strong>${escapeHtml(count)}</strong></div>`)
        .join("")}</div>`;
}

function renderWarnings(warnings) {
    if (!warnings || !warnings.length) {
        return `<div class="warning-empty"><span aria-hidden="true">✓</span><div><strong>未发现训练告警</strong><small>最近一次训练数据检查正常</small></div></div>`;
    }
    const sourceLabels = {
        gold_sql: "Gold SQL",
        knowledge: "知识库",
        schema: "Schema",
        evaluation: "评测集",
    };
    const items = warnings
        .slice(0, 3)
        .map((item) => {
            const text = String(item || "");
            const match = text.match(/^([^:]+):(\d+):\s*(.*)$/);
            const source = match ? (sourceLabels[match[1]] || match[1]) : "训练检查";
            const record = match ? `记录 ${match[2]}` : "需要关注";
            const detail = match ? match[3] : text;
            return `<div class="warning-item">
                <span class="warning-icon" aria-hidden="true">!</span>
                <div class="warning-body"><div class="warning-meta"><strong>${escapeHtml(source)}</strong><span>${escapeHtml(record)}</span></div><p>${escapeHtml(detail)}</p></div>
            </div>`;
        })
        .join("");
    const remaining = Math.max(0, warnings.length - 3);
    return `<div class="warning-summary"><span><strong>${escapeHtml(warnings.length)}</strong> 项需要处理</span><small>不会阻止服务运行</small></div>
        <div class="warning-list">${items}</div>
        ${remaining ? `<div class="warning-more">另有 ${escapeHtml(remaining)} 项，请查看完整训练报告</div>` : ""}`;
}

function renderBaselineFailures(items) {
    if (!items || !items.length) {
        return "";
    }
    return `<div class="baseline-failure-list">${items.map((item) => {
        const failedChecks = (item.failed_checks || []).map((name) =>
            `<span class="chip baseline-chip">${escapeHtml(name)}</span>`
        ).join("");
        const sqlBlock = item.actual_sql
            ? `<div class="baseline-sql">${escapeHtml(item.actual_sql)}</div>`
            : '<div class="text-block subtle">未返回生成 SQL</div>';
        const errorBlock = item.baseline_error || item.error
            ? `<div class="baseline-error">${escapeHtml(item.baseline_error || item.error)}</div>`
            : "";
        return `
            <div class="baseline-failure-item">
                <div class="baseline-failure-head">
                    <strong>Case ${escapeHtml(item.case_index ?? "")}</strong>
                    <span class="subtle">结果行数：${escapeHtml(item.result_row_count ?? 0)}</span>
                </div>
                <div class="baseline-question">${escapeHtml(item.question || "")}</div>
                <div class="chips">${failedChecks || '<span class="chip baseline-chip">baseline_result_match</span>'}</div>
                ${sqlBlock}
                ${errorBlock}
            </div>
        `;
    }).join("")}</div>`;
}

function renderTrainingReport(payload) {
    if (!payload || !payload.success) {
        return `<div class="report-empty">${escapeHtml(payload?.error || "训练报告读取失败。")}</div>`;
    }

    if (!payload.available) {
        return '<div class="report-empty">尚未发布活动知识版本。请先执行 `text2sql-train`。</div>';
    }

    const summary = payload.summary || {};
    const report = payload.report || {};
    const evaluationText = summary.evaluation_total
        ? `${summary.evaluation_passed}/${summary.evaluation_total} 通过 (${summary.evaluation_pass_rate}%)`
        : "未配置评测集或尚未执行评测";
    const baselineFailureText = summary.baseline_failed_count
        ? `发现 ${summary.baseline_failed_count} 个失败案例，请检查生成结果。`
        : "全部基准案例对比通过";

    const passRate = Math.max(0, Math.min(100, Number(summary.evaluation_pass_rate) || 0));
    return `<div class="training-card">
        <div class="training-head">
            <div><strong>${escapeHtml(formatTime(summary.finished_at) || "训练时间未知")}</strong><span>最近完成时间</span></div>
            <span class="health-badge ${summary.baseline_failed_count ? "warning" : "healthy"}">${summary.baseline_failed_count ? "需关注" : "状态良好"}</span>
        </div>
        <div class="training-metrics">
            <div><strong>${escapeHtml(summary.table_count ?? 0)}</strong><span>数据表</span></div>
            <div><strong>${escapeHtml(summary.column_count ?? 0)}</strong><span>字段</span></div>
            <div><strong>${escapeHtml(summary.knowledge_records ?? 0)}</strong><span>知识条目</span></div>
        </div>
        <div class="evaluation-block">
            <div class="evaluation-label"><span>评测通过率</span><strong>${summary.evaluation_total ? `${escapeHtml(summary.evaluation_pass_rate)}%` : "未评测"}</strong></div>
            <div class="progress-track"><span style="width:${passRate}%"></span></div>
            <small>${escapeHtml(evaluationText)}</small>
        </div>
    </div>
    <details class="report-details">
        <summary>
            <span class="report-details-title"><span class="report-details-icon" aria-hidden="true">≡</span><span><strong>训练详情</strong><small>样本、基准对比与质量告警</small></span></span>
            <span class="report-details-toggle"><span class="toggle-label"></span><span class="toggle-chevron" aria-hidden="true"></span></span>
        </summary>
        <div class="report-detail-body">
            <div class="detail-stats">
                <div><span>反馈样本</span><strong>${escapeHtml(summary.feedback_examples ?? 0)}</strong></div>
                <div><span>问答示例</span><strong>${escapeHtml(summary.question_sql_examples ?? 0)}</strong></div>
                <div><span>采样训练</span><strong>${summary.include_samples ? `开启 · ${escapeHtml(summary.sample_rows ?? 0)} 行` : "关闭"}</strong></div>
            </div>
            <div class="report-subsection baseline-subsection"><div class="report-subsection-head"><strong>基准 SQL 对比</strong><small>结果一致性</small></div><span class="report-subsection-summary">${escapeHtml(baselineFailureText)}</span>${renderBaselineFailures(summary.baseline_failed_cases || [])}</div>
            <div class="report-subsection"><div class="report-subsection-head"><strong>训练类型分布</strong><small>知识构成</small></div>${renderTypeBreakdown(report.knowledge_records_by_type || {})}</div>
            <div class="report-subsection"><div class="report-subsection-head"><strong>训练告警</strong><small>质量检查</small></div>${renderWarnings(report.warnings || [])}</div>
        </div>
    </details>`;
}

function renderSystemStatus(payload) {
    const healthy = payload?.status === "healthy";
    const runtime = payload?.runtime || {};
    if (!healthy) {
        return `<div class="system-health-head"><span class="health-indicator error"></span><div><strong>服务不可用</strong><span>${escapeHtml(payload?.error || "无法连接本地运行环境")}</span></div></div>`;
    }
    return `<div class="system-health-head"><span class="health-indicator"></span><div><strong>服务运行正常</strong><span>所有核心组件已就绪</span></div><span class="health-badge healthy">在线</span></div>
        <div class="runtime-list">
            <div><span>语言模型</span><strong>${escapeHtml(runtime.llm_model || "未配置")}</strong></div>
            <div><span>SQL 执行器</span><strong>${escapeHtml(runtime.sql_runner || "未就绪")}</strong></div>
            <div><span>知识库</span><strong>${escapeHtml(runtime.knowledge_memory || "未就绪")}</strong></div>
            <div><span>查询能力</span><strong>语义计划 · 只读校验</strong></div>
        </div>`;
}

async function loadSystemStatus() {
    systemStatusEl.className = "system-status loading";
    systemStatusEl.textContent = "正在检查本地服务...";
    try {
        const payload = await fetchJson("/readyz");
        systemStatusEl.className = "system-status healthy";
        systemStatusEl.innerHTML = renderSystemStatus(payload);
    } catch (error) {
        systemStatusEl.className = "system-status unhealthy";
        systemStatusEl.innerHTML = renderStatusError(error);
    }
}

async function loadStatusDashboard() {
    reloadReportBtn.disabled = true;
    statusUpdatedAtEl.textContent = "正在刷新...";
    await Promise.all([loadSystemStatus(), loadTrainingReport()]);
    statusUpdatedAtEl.textContent = `更新于 ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    reloadReportBtn.disabled = false;
}

async function loadTrainingReport() {
    if (!trainingReportEl) {
        return;
    }

    trainingReportEl.innerHTML = '<div class="report-empty">正在加载训练报告...</div>';
    try {
        const payload = await fetchJson("/training-report");
        trainingReportEl.innerHTML = renderTrainingReport(payload);
    } catch (error) {
        trainingReportEl.innerHTML = renderStatusError(error, true);
    }
}

function renderAssistantContent(entry) {
    const payload = entry.payload || {};
    const statusClass = payload.success ? "success" : "error";
    const statusText = ({
        success: "成功",
        refused: "无法支持",
        clarification_required: "需要补充信息",
        infrastructure_error: "服务暂不可用",
        validation_failed: "SQL 未通过校验",
        generation_failed: "生成未完成",
    })[payload.outcome] || (payload.success ? "成功" : "失败");
    const sqlBlock = payload.sql
        ? `<div class="section"><div class="section-title">SQL</div><div class="sql-box">${escapeHtml(payload.sql)}</div></div>`
        : "";
    const refusalBlock = payload.refusal_reason
        ? `<div class="section"><div class="section-title">${payload.outcome === "clarification_required" ? "需要补充的信息" : "无法支持的原因"}</div><div class="reason-box">${escapeHtml(payload.refusal_reason)}</div></div>`
        : "";
    const errorBlock = payload.error && payload.error !== payload.refusal_reason
        ? `<div class="section"><div class="section-title">错误信息</div><div class="reason-box">${escapeHtml(payload.error)}</div></div>`
        : "";
    const candidateBlock = `<div class="section"><div class="section-title">候选表</div>${renderChips(payload.candidate_tables || [], payload.candidate_scores || {})}</div>`;
    const resultBlock = payload.result_row_count
        ? `<div class="section"><div class="section-title">执行结果</div>${renderResultTable(payload.result || [], payload.result_columns || [])}</div>`
        : "";
    const metaBlock = `<div class="section"><div class="section-title">摘要</div><div class="meta-grid">
        <div class="meta-card"><div class="name">状态</div><div class="value"><span class="pill ${statusClass}">${statusText}</span></div></div>
        <div class="meta-card"><div class="name">尝试次数</div><div class="value">${escapeHtml(payload.attempts ?? "")}</div></div>
        <div class="meta-card"><div class="name">结果行数</div><div class="value">${escapeHtml(payload.result_row_count ?? 0)}</div></div>
        <div class="meta-card"><div class="name">结果列数</div><div class="value">${escapeHtml((payload.result_columns || []).length)}</div></div>
    </div></div>`;
    let summary = "SQL 已生成。你可以展开查看查询语句和命中的数据表。";
    if (payload.success && entry.mode === "execute") {
        summary = payload.result_row_count
            ? `查询已完成，共返回 ${escapeHtml(payload.result_row_count)} 行结果。`
            : "查询已完成，但没有返回符合条件的数据。";
    } else if (!payload.success) {
        summary = escapeHtml(payload.refusal_reason || payload.error || "抱歉，本次查询未能完成。");
    }
    const detailTitle = payload.success ? "查看 SQL 与查询详情" : "查看原因与处理信息";
    return `<div class="answer-summary ${payload.success ? "" : "error"}">${summary}</div>
        <details class="details" ${payload.success ? "" : "open"}>
            <summary>${detailTitle}</summary>
            <div class="details-content">${metaBlock}${candidateBlock}${sqlBlock}${refusalBlock}${errorBlock}${resultBlock}${renderValidationActions(entry)}</div>
        </details>`;
}

function renderEntry(entry) {
    const node = document.createElement("div");
    const isError = entry.kind === "assistant" && entry.payload && !entry.payload.success;
    node.className = `message ${entry.kind === "user" ? "user" : "assistant"} ${isError ? "error" : ""}`.trim();

    const title = entry.kind === "user" ? "用户问题" : (entry.mode === "execute" ? "生成并执行" : "只生成 SQL");
    const content = entry.kind === "user"
        ? `<div class="text-block">${escapeHtml(entry.text || "")}</div>`
        : renderAssistantContent(entry);

    node.innerHTML = entry.kind === "user"
        ? `<div class="message-body">${content}</div>`
        : `<div class="avatar" aria-hidden="true">AI</div><div class="message-body">
            <div class="message-header"><span class="role">${escapeHtml(title)}</span><span class="timestamp">${escapeHtml(formatTime(entry.timestamp))}</span></div>
            ${content}</div>`;
    messagesEl.appendChild(node);
}

function renderEmptyState() {
    messagesEl.innerHTML = `<div class="empty-state">
        <div class="agent-orb" aria-hidden="true">AI</div>
        <h1>今天想了解哪些数据？</h1>
        <p>用自然语言描述你的业务问题。我会检索相关数据表、生成安全的 SQL，并在你允许时执行查询。</p>
        <div class="suggestions">
            <button class="suggestion" type="button">统计今年各机构的业务量趋势</button>
            <button class="suggestion" type="button">查询最近一个月排名前十的机构</button>
            <button class="suggestion" type="button">按城市对比本季度核心指标</button>
            <button class="suggestion" type="button">分析本月与上月的数据变化</button>
        </div>
    </div>`;
}

function updateConversationPreview() {
    const firstQuestion = chatHistory.find((entry) => entry.kind === "user");
    if (firstQuestion) {
        const count = chatHistory.filter((entry) => entry.kind === "user").length;
        conversationPreview.innerHTML = `<div class="conversation-item"><span class="conversation-icon" aria-hidden="true">◫</span><div><strong>${escapeHtml(firstQuestion.text)}</strong><small>${escapeHtml(count)} 条提问 · ${escapeHtml(formatTime(firstQuestion.timestamp))}</small></div><span class="conversation-current">当前</span></div>`;
    } else {
        conversationPreview.innerHTML = `<div class="conversation-empty"><span aria-hidden="true">◇</span><strong>暂无对话</strong><small>提出问题后会显示在这里</small></div>`;
    }
    conversationPreview.classList.toggle("active", Boolean(firstQuestion));
}

function rerenderHistory() {
    messagesEl.innerHTML = "";
    if (chatHistory.length) {
        chatHistory.forEach(renderEntry);
    } else {
        renderEmptyState();
    }
    updateConversationPreview();
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function pushEntry(entry) {
    if (!entry.id) {
        entry.id = generateEntryId();
    }
    chatHistory.push(entry);
    saveHistory();
    if (messagesEl.querySelector(".empty-state")) {
        messagesEl.innerHTML = "";
    }
    renderEntry(entry);
    updateConversationPreview();
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setBusy(isBusy, text = "") {
    askBtn.disabled = isBusy;
    clearBtn.disabled = isBusy;
    executeModeBtn.disabled = isBusy;
    generateModeBtn.disabled = isBusy;
    newChatBtn.disabled = isBusy;
    statusEl.textContent = text;
}

async function submitQuestion(executeSql) {
    const question = questionEl.value.trim();
    if (!question) {
        statusEl.textContent = "请输入问题。";
        questionEl.focus();
        return;
    }

    pushEntry({
        id: generateEntryId(),
        kind: "user",
        text: question,
        timestamp: new Date().toISOString(),
    });
    questionEl.value = "";
    resizeComposer();
    setBusy(true, executeSql ? "正在生成并执行..." : "正在生成 SQL...");

    try {
        const response = await fetch("/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                question,
                execute_sql: executeSql,
                max_retries: executeSql ? 2 : 1,
            }),
        });
        if (response.status === 401) {
            requestAuthentication();
            throw new Error("请先完成身份验证，然后重新发送问题");
        }
        const payload = await response.json();
        pushEntry({
            id: generateEntryId(),
            kind: "assistant",
            mode: executeSql ? "execute" : "generate",
            payload,
            timestamp: new Date().toISOString(),
        });
        statusEl.textContent = payload.success ? "完成。" : "已返回失败原因。";
    } catch (error) {
        pushEntry({
            id: generateEntryId(),
            kind: "assistant",
            mode: executeSql ? "execute" : "generate",
            payload: {
                success: false,
                sql: null,
                result: null,
                attempts: 0,
                error: `请求失败：${error.message}`,
                candidate_tables: [],
                candidate_scores: {},
                candidate_score_reasons: {},
                refusal_reason: null,
                result_row_count: 0,
                result_columns: [],
            },
            timestamp: new Date().toISOString(),
        });
        statusEl.textContent = "请求失败。";
    } finally {
        setBusy(false, statusEl.textContent);
    }
}

function updateEntryFeedback(entryId, feedback) {
    const entry = chatHistory.find((item) => item.id === entryId);
    if (!entry || !entry.payload) {
        return;
    }
    entry.payload.validation_feedback = feedback;
    saveHistory();
    rerenderHistory();
}

async function submitValidationFeedback(entryId, label) {
    const entry = chatHistory.find((item) => item.id === entryId);
    if (!entry || !entry.payload || !entry.payload.sql) {
        statusEl.textContent = "未找到可反馈的 SQL 结果。";
        return;
    }
    const comment = label === "incorrect"
        ? window.prompt("可选：请补充错误原因，便于后续优化。", "") || ""
        : window.prompt("可选：请补充正确原因或备注。", "") || "";
    statusEl.textContent = "正在提交在线反馈...";
    try {
        const response = await fetch("/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                question: entry.payload.question || "",
                sql: entry.payload.sql || "",
                candidate_tables: entry.payload.candidate_tables || [],
                candidate_score_reasons: entry.payload.candidate_score_reasons || {},
                validation_label: label,
                comment,
                result_row_count: entry.payload.result_row_count || 0,
                had_execution_result: (entry.payload.result_row_count || 0) > 0,
            }),
        });
        if (response.status === 401) {
            requestAuthentication();
            throw new Error("请先完成身份验证");
        }
        const payload = await response.json();
        if (!payload.success) {
            throw new Error(payload.error || "在线反馈提交失败");
        }
        updateEntryFeedback(entryId, {
            status: "submitted",
            label,
            comment,
            submitted_at: payload.submitted_at,
        });
        statusEl.textContent = label === "correct" ? "已标记为正确。" : "已标记为错误。";
    } catch (error) {
        statusEl.textContent = `在线反馈提交失败：${error.message}`;
    }
}

askBtn.addEventListener("click", () => submitQuestion(executeMode));

messagesEl.addEventListener("click", (event) => {
    const target = event.target.closest(".feedback-btn");
    if (!target) {
        return;
    }
    const actionRoot = target.closest(".feedback-actions");
    const entryId = actionRoot?.dataset?.entryId;
    const label = target.dataset.label;
    if (!entryId || !label) {
        return;
    }
    submitValidationFeedback(entryId, label);
});

questionEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submitQuestion(executeMode);
    }
});

function resizeComposer() {
    questionEl.style.height = "auto";
    questionEl.style.height = `${Math.min(questionEl.scrollHeight, 180)}px`;
}

function clearConversation() {
    chatHistory = [];
    saveHistory();
    statusEl.textContent = "Enter 发送 · Shift + Enter 换行";
    rerenderHistory();
    questionEl.focus();
}

function toggleSidebar(open) {
    sidebar.classList.toggle("open", open);
    sidebarBackdrop.hidden = !open;
}

function toggleReport(open) {
    reportDrawer.classList.toggle("open", open);
    reportDrawer.setAttribute("aria-hidden", String(!open));
    reportBackdrop.hidden = !open;
}

questionEl.addEventListener("input", resizeComposer);
function setExecuteMode(shouldExecute) {
    executeMode = shouldExecute;
    executeModeBtn.classList.toggle("active", shouldExecute);
    executeModeBtn.setAttribute("aria-checked", String(shouldExecute));
    generateModeBtn.classList.toggle("active", !shouldExecute);
    generateModeBtn.setAttribute("aria-checked", String(!shouldExecute));
}
executeModeBtn.addEventListener("click", () => setExecuteMode(true));
generateModeBtn.addEventListener("click", () => setExecuteMode(false));
newChatBtn.addEventListener("click", clearConversation);
document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        clearConversation();
    }
});
clearBtn.addEventListener("click", clearConversation);
menuBtn.addEventListener("click", () => toggleSidebar(true));
sidebarBackdrop.addEventListener("click", () => toggleSidebar(false));
reportBtn.addEventListener("click", () => { toggleSidebar(false); toggleReport(true); loadStatusDashboard(); });
closeReportBtn.addEventListener("click", () => toggleReport(false));
reportBackdrop.addEventListener("click", () => toggleReport(false));
messagesEl.addEventListener("click", (event) => {
    const suggestion = event.target.closest(".suggestion");
    if (suggestion) {
        questionEl.value = suggestion.textContent.trim();
        resizeComposer();
        questionEl.focus();
    }
});

if (reloadReportBtn) {
    reloadReportBtn.addEventListener("click", loadStatusDashboard);
}

authForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    authError.textContent = "";
    try {
        await createBrowserSession(authKey.value);
        authKey.value = "";
        authDialog.close();
        statusEl.textContent = "身份验证成功，请重新发送问题。";
    } catch (error) {
        authError.textContent = error.message;
    }
});
authCancelBtn.addEventListener("click", () => authDialog.close());

loadHistory();
rerenderHistory();
