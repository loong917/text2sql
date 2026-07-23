const HISTORY_KEY = "text2sql.console.history.v1";
const messagesEl = document.getElementById("messages");
const questionEl = document.getElementById("question");
const statusEl = document.getElementById("status");
const generateBtn = document.getElementById("generateBtn");
const askBtn = document.getElementById("askBtn");
const clearBtn = document.getElementById("clearBtn");
const trainingReportEl = document.getElementById("trainingReport");
const reloadReportBtn = document.getElementById("reloadReportBtn");
let chatHistory = [];

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

function loadHistory() {
    try {
        const raw = localStorage.getItem(HISTORY_KEY);
        chatHistory = raw ? JSON.parse(raw) : [];
        chatHistory = chatHistory.map((entry) => ({
            id: entry.id || generateEntryId(),
            ...entry,
        }));
    } catch (error) {
        chatHistory = [];
    }
}

function saveHistory() {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(chatHistory.slice(-50)));
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
        return '<div class="text-block">无告警</div>';
    }
    return `<div class="warning-list">${warnings
        .slice(0, 3)
        .map((item) => `<div class="warning-item">${escapeHtml(item)}</div>`)
        .join("")}</div>`;
}

function renderBaselineFailures(items) {
    if (!items || !items.length) {
        return '<div class="text-block">无基准 SQL 对比失败 case</div>';
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
        return '<div class="report-empty">尚未发现训练报告。请先执行训练生成 `training_report.json`。</div>';
    }

    const summary = payload.summary || {};
    const report = payload.report || {};
    const evaluationText = summary.evaluation_total
        ? `${summary.evaluation_passed}/${summary.evaluation_total} 通过 (${summary.evaluation_pass_rate}%)`
        : "未配置评测集或尚未执行评测";
    const baselineFailureText = summary.baseline_failed_count
        ? `${summary.baseline_failed_count} 个 case 基准对比失败`
        : "无基准 SQL 对比失败 case";

    return `
        <div class="section report-section">
            <div class="section-title">最近训练</div>
            <div class="text-block subtle">${escapeHtml(formatTime(summary.finished_at) || "未知时间")}</div>
        </div>
        <div class="section report-section">
            <div class="section-title">摘要</div>
            <div class="meta-grid compact">
                <div class="meta-card"><div class="name">表数</div><div class="value">${escapeHtml(summary.table_count ?? 0)}</div></div>
                <div class="meta-card"><div class="name">字段数</div><div class="value">${escapeHtml(summary.column_count ?? 0)}</div></div>
                <div class="meta-card"><div class="name">知识条目</div><div class="value">${escapeHtml(summary.knowledge_records ?? 0)}</div></div>
                <div class="meta-card"><div class="name">反馈样本</div><div class="value">${escapeHtml(summary.feedback_examples ?? 0)}</div></div>
                <div class="meta-card"><div class="name">问答示例</div><div class="value">${escapeHtml(summary.question_sql_examples ?? 0)}</div></div>
                <div class="meta-card"><div class="name">采样训练</div><div class="value">${summary.include_samples ? `开启 (${escapeHtml(summary.sample_rows ?? 0)} 行)` : "关闭"}</div></div>
            </div>
        </div>
        <div class="section report-section">
            <div class="section-title">评测结果</div>
            <div class="text-block">${escapeHtml(evaluationText)}</div>
        </div>
        <div class="section report-section">
            <div class="section-title">基准 SQL 对比失败</div>
            <div class="text-block subtle">${escapeHtml(baselineFailureText)}</div>
            ${renderBaselineFailures(summary.baseline_failed_cases || [])}
        </div>
        <div class="section report-section">
            <div class="section-title">训练类型分布</div>
            ${renderTypeBreakdown(report.knowledge_records_by_type || {})}
        </div>
        <div class="section report-section">
            <div class="section-title">训练告警</div>
            ${renderWarnings(report.warnings || [])}
        </div>
    `;
}

async function loadTrainingReport() {
    if (!trainingReportEl) {
        return;
    }

    trainingReportEl.innerHTML = '<div class="report-empty">正在加载训练报告...</div>';
    try {
        const response = await fetch("/training-report");
        const payload = await response.json();
        trainingReportEl.innerHTML = renderTrainingReport(payload);
    } catch (error) {
        trainingReportEl.innerHTML = `<div class="report-empty">训练报告请求失败：${escapeHtml(error.message)}</div>`;
    }
}

function renderAssistantContent(entry) {
    const payload = entry.payload || {};
    const statusClass = payload.success ? "success" : "error";
    const statusText = payload.success ? "成功" : "失败";
    const sqlBlock = payload.sql
        ? `<div class="section"><div class="section-title">SQL</div><div class="sql-box">${escapeHtml(payload.sql)}</div></div>`
        : "";
    const refusalBlock = payload.refusal_reason
        ? `<div class="section"><div class="section-title">拒答原因</div><div class="reason-box">${escapeHtml(payload.refusal_reason)}</div></div>`
        : "";
    const errorBlock = payload.error
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
    return `${metaBlock}${candidateBlock}${sqlBlock}${refusalBlock}${errorBlock}${resultBlock}${renderValidationActions(entry)}`;
}

function renderEntry(entry) {
    const node = document.createElement("div");
    const isError = entry.kind === "assistant" && entry.payload && !entry.payload.success;
    node.className = `message ${entry.kind === "user" ? "user" : "assistant"} ${isError ? "error" : ""}`.trim();

    const title = entry.kind === "user" ? "用户问题" : (entry.mode === "execute" ? "生成并执行" : "只生成 SQL");
    const content = entry.kind === "user"
        ? `<div class="text-block">${escapeHtml(entry.text || "")}</div>`
        : renderAssistantContent(entry);

    node.innerHTML = `
        <div class="message-header">
            <span class="role">${escapeHtml(title)}</span>
            <span class="timestamp">${escapeHtml(formatTime(entry.timestamp))}</span>
        </div>
        ${content}
    `;
    messagesEl.appendChild(node);
}

function rerenderHistory() {
    messagesEl.innerHTML = "";
    chatHistory.forEach(renderEntry);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function pushEntry(entry) {
    if (!entry.id) {
        entry.id = generateEntryId();
    }
    chatHistory.push(entry);
    saveHistory();
    renderEntry(entry);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setBusy(isBusy, text = "") {
    generateBtn.disabled = isBusy;
    askBtn.disabled = isBusy;
    clearBtn.disabled = isBusy;
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
        const response = await fetch("/feedback-validation", {
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

generateBtn.addEventListener("click", () => submitQuestion(false));
askBtn.addEventListener("click", () => submitQuestion(true));
clearBtn.addEventListener("click", () => {
    chatHistory = [];
    saveHistory();
    messagesEl.innerHTML = "";
    statusEl.textContent = "";
});

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
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        submitQuestion(false);
    }
});

if (reloadReportBtn) {
    reloadReportBtn.addEventListener("click", loadTrainingReport);
}

loadHistory();
if (chatHistory.length) {
    rerenderHistory();
} else {
    pushEntry({
        id: generateEntryId(),
        kind: "assistant",
        mode: "generate",
        payload: {
            success: true,
            sql: "欢迎使用 Text2SQL 控制台。",
            result: null,
            attempts: 0,
            error: null,
            candidate_tables: [],
            candidate_scores: {},
            candidate_score_reasons: {},
            refusal_reason: null,
            result_row_count: 0,
            result_columns: [],
        },
        timestamp: new Date().toISOString(),
    });
}

loadTrainingReport();
