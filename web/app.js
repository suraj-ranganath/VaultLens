const state = {
  threadId: localStorage.getItem("vaultThreadId") || "",
  health: null,
  latestQuestion: "",
  latestResult: null,
  feed: [],
};

const els = {
  form: document.querySelector("#query-form"),
  question: document.querySelector("#question-input"),
  reasoning: document.querySelector("#reasoning-select"),
  webSearch: document.querySelector("#web-search-toggle"),
  submit: document.querySelector("#submit-button"),
  answerEmpty: document.querySelector("#answer-empty"),
  answerBody: document.querySelector("#answer-body"),
  gapsBlock: document.querySelector("#gaps-block"),
  gapsList: document.querySelector("#gaps-list"),
  followupsBlock: document.querySelector("#followups-block"),
  followupsList: document.querySelector("#followups-list"),
  citationsList: document.querySelector("#citations-list"),
  citationCount: document.querySelector("#citation-count"),
  traceList: document.querySelector("#trace-list"),
  traceCount: document.querySelector("#trace-count"),
  threadId: document.querySelector("#thread-id"),
  modelName: document.querySelector("#model-name"),
  healthStatus: document.querySelector("#health-status"),
  runState: document.querySelector("#run-state"),
  confidenceBadge: document.querySelector("#confidence-badge"),
  saveAnswerButton: document.querySelector("#save-answer-button"),
  saveStatus: document.querySelector("#save-status"),
  newThreadButton: document.querySelector("#new-thread-button"),
  presets: [...document.querySelectorAll(".preset")],
};

boot();

async function boot() {
  hydrateThread();
  bindEvents();
  await refreshHealth();
}

function bindEvents() {
  els.form.addEventListener("submit", onSubmit);
  els.saveAnswerButton.addEventListener("click", onSaveAnswer);
  els.newThreadButton.addEventListener("click", () => {
    state.threadId = "";
    localStorage.removeItem("vaultThreadId");
    hydrateThread();
    setRunState("idle", "Idle");
    resetSaveState();
  });

  els.presets.forEach((button) => {
    button.addEventListener("click", () => {
      els.question.value = button.dataset.question || "";
      els.question.focus();
    });
  });
}

async function refreshHealth() {
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    state.health = data;
    els.healthStatus.textContent = data.ok ? "Ready" : "Unavailable";
    els.modelName.textContent = data.model || "-";
  } catch {
    els.healthStatus.textContent = "Offline";
    els.modelName.textContent = "-";
  }
}

async function onSubmit(event) {
  event.preventDefault();
  const question = els.question.value.trim();
  if (!question) {
    els.question.focus();
    return;
  }

  setRunState("running", "Working");
  els.submit.disabled = true;
  state.latestQuestion = question;
  state.latestResult = null;
  state.feed = [];
  resetSaveState();
  renderTrace([]);
  renderCitations([]);
  clearAnswerShell();

  try {
    const response = await fetch("/api/query-stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        threadId: state.threadId || null,
        reasoningEffort: els.reasoning.value,
        includeWebSearch: els.webSearch.checked,
      }),
    });
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || "Query failed");
    }
    await consumeQueryStream(response);
    if (!state.latestResult) {
      throw new Error("Query completed without a final result.");
    }
  } catch (error) {
    setRunState("idle", "Error");
    renderFailure(error instanceof Error ? error.message : String(error));
  } finally {
    els.submit.disabled = false;
  }
}

async function onSaveAnswer() {
  if (!state.latestResult || !state.latestQuestion) {
    return;
  }

  setSaveState("Saving...");
  els.saveAnswerButton.disabled = true;

  try {
    const response = await fetch("/api/save-answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: state.latestQuestion,
        answer: state.latestResult.answer,
        trace: state.latestResult.trace || [],
        meta: state.latestResult.meta || {},
        threadId: state.threadId || null,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Failed to save answer");
    }
    setSaveState(`Saved to ${data.path}`, data.url);
  } catch (error) {
    setSaveState(error instanceof Error ? error.message : String(error));
  } finally {
    els.saveAnswerButton.disabled = false;
  }
}

async function consumeQueryStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

    let newlineIndex = buffer.indexOf("\n");
    while (newlineIndex >= 0) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (line) {
        handleStreamEvent(JSON.parse(line));
      }
      newlineIndex = buffer.indexOf("\n");
    }

    if (done) {
      const tail = buffer.trim();
      if (tail) {
        handleStreamEvent(JSON.parse(tail));
      }
      break;
    }
  }
}

function handleStreamEvent(event) {
  if (event.type === "thread.started") {
    state.threadId = event.threadId || "";
    if (state.threadId) {
      localStorage.setItem("vaultThreadId", state.threadId);
    }
    hydrateThread();
    appendFeed({
      type: "system",
      phase: "started",
      title: "Thread started",
      body: state.threadId,
    });
    return;
  }

  if (event.type === "turn.started") {
    appendFeed({
      type: "system",
      phase: "started",
      title: "Turn started",
      body: "Agent is planning the vault search path.",
    });
    return;
  }

  if (event.type === "turn.completed") {
    appendFeed({
      type: "system",
      phase: "completed",
      title: "Turn completed",
      body: `usage: ${formatUsage(event.usage)}`,
    });
    return;
  }

  if (event.type === "turn.failed") {
    appendFeed({
      type: "error",
      phase: "failed",
      title: "Turn failed",
      body: event.error || "Unknown failure",
    });
    return;
  }

  if (event.type === "error") {
    appendFeed({
      type: "error",
      phase: "failed",
      title: "Agent error",
      body: event.message || "Unknown error",
    });
    return;
  }

  if (event.type === "item") {
    appendFeed(formatFeedEntry(event.phase, event.item));
    return;
  }

  if (event.type === "result") {
    state.threadId = event.threadId || state.threadId;
    state.latestResult = event;
    hydrateThread();
    renderAnswer(event.answer);
    renderCitations(event.answer.citations || []);
    showSaveButton();
    els.modelName.textContent = event.meta?.model || state.health?.model || "-";
    setRunState("done", `Done in ${formatDuration(event.meta?.durationMs || 0)}`);
  }
}

function formatFeedEntry(phase, item) {
  if (item.type === "reasoning") {
    return {
      type: "reasoning",
      phase,
      title: "Reasoning summary",
      body: item.text,
    };
  }
  if (item.type === "todo_list") {
    return {
      type: "todo_list",
      phase,
      title: "Plan update",
      body: item.items.map((todo) => `${todo.completed ? "[x]" : "[ ]"} ${todo.text}`).join("\n"),
    };
  }
  if (item.type === "command_execution") {
    return {
      type: "command_execution",
      phase,
      title: item.command,
      body: item.output_preview || "",
      meta: item.exit_code === null ? item.status : `exit ${item.exit_code}`,
    };
  }
  if (item.type === "web_search") {
    return {
      type: "web_search",
      phase,
      title: item.query,
      body: "",
    };
  }
  if (item.type === "mcp_tool_call") {
    return {
      type: "mcp_tool_call",
      phase,
      title: `${item.server}.${item.tool}`,
      body: item.arguments_preview || "",
      meta: item.error ? `${item.status} • ${item.error}` : item.status,
    };
  }
  if (item.type === "agent_message") {
    const body = extractAgentMessageBody(item.text);
    return {
      type: "agent_message",
      phase,
      title: "Agent message",
      body,
    };
  }
  if (item.type === "error") {
    return {
      type: "error",
      phase,
      title: "Agent item error",
      body: item.message,
    };
  }
  return {
    type: item.type || "event",
    phase,
    title: item.type || "event",
    body: "",
  };
}

function appendFeed(entry) {
  state.feed.unshift(entry);
  state.feed = state.feed.slice(0, 120);
  renderTrace(state.feed);
}

function renderAnswer(answer) {
  els.answerEmpty.classList.add("hidden");
  els.answerBody.classList.remove("hidden");
  els.answerBody.innerHTML = renderMarkdown(answer.answer_markdown || answer.concise_answer || "");

  const confidence = String(answer.confidence || "").trim();
  els.confidenceBadge.className = `confidence-badge ${confidence || ""}`;
  els.confidenceBadge.textContent = confidence ? `${confidence} confidence` : "";
  els.confidenceBadge.classList.toggle("hidden", !confidence);

  const gaps = answer.gaps || [];
  els.gapsList.innerHTML = gaps.map((gap) => `<li>${escapeHtml(gap)}</li>`).join("");
  els.gapsBlock.classList.toggle("hidden", gaps.length === 0);

  const followUps = answer.follow_up_questions || [];
  els.followupsList.innerHTML = followUps
    .map((question) => `<button class="chip" type="button">${escapeHtml(question)}</button>`)
    .join("");
  els.followupsBlock.classList.toggle("hidden", followUps.length === 0);
  [...els.followupsList.querySelectorAll(".chip")].forEach((chip) => {
    chip.addEventListener("click", () => {
      els.question.value = chip.textContent || "";
      els.question.focus();
    });
  });
}

function renderCitations(citations) {
  els.citationCount.textContent = String(citations.length);
  els.citationsList.innerHTML = citations.length
    ? citations
        .map(
          (citation) => `
            <article class="citation-card">
              <div class="citation-kind">${escapeHtml(citation.note_type || "note")}</div>
              <h3><a href="/vault/${encodeURI(citation.path)}" target="_blank" rel="noreferrer">${escapeHtml(citation.title || citation.path)}</a></h3>
              <div class="muted">${escapeHtml(citation.path)}</div>
              <p>${escapeHtml(citation.relevance || "")}</p>
            </article>
          `,
        )
        .join("")
    : `<div class="empty-state">No citations returned yet.</div>`;
}

function renderTrace(trace) {
  els.traceCount.textContent = String(trace.length);
  els.traceList.innerHTML = trace.length
    ? trace
        .map((item) => {
          if (item.type === "command_execution") {
            return `
              <article class="trace-card">
                <div class="trace-kind">command • ${escapeHtml(item.phase || "")}</div>
                <h3>${escapeHtml(item.title || item.command || "")}</h3>
                <div class="muted">${escapeHtml(item.meta || (item.exit_code === null ? "" : `exit ${item.exit_code}`))}</div>
                <div class="trace-command">${escapeHtml(item.body || item.output_preview || "")}</div>
              </article>
            `;
          }
          if (item.type === "web_search") {
            return `
              <article class="trace-card">
                <div class="trace-kind">web search • ${escapeHtml(item.phase || "")}</div>
                <h3>${escapeHtml(item.query || item.title || "")}</h3>
              </article>
            `;
          }
          if (item.type === "mcp_tool_call") {
            return `
              <article class="trace-card">
                <div class="trace-kind">mcp tool • ${escapeHtml(item.phase || "")}</div>
                <h3>${escapeHtml(item.title || `${item.server}.${item.tool}`)}</h3>
                <div class="muted">${escapeHtml(item.meta || item.status || "")}</div>
                ${item.body ? `<div class="trace-command">${escapeHtml(item.body)}</div>` : ""}
              </article>
            `;
          }
          if (item.type === "todo_list") {
            return `
              <article class="trace-card">
                <div class="trace-kind">plan • ${escapeHtml(item.phase || "")}</div>
                <h3>${escapeHtml(item.title || "Agent todo list")}</h3>
                <div class="trace-command">${escapeHtml(item.body || "")}</div>
              </article>
            `;
          }
          if (item.type === "reasoning") {
            return `
              <article class="trace-card">
                <div class="trace-kind">reasoning summary • ${escapeHtml(item.phase || "")}</div>
                <h3>${escapeHtml(item.title || "Reasoning summary")}</h3>
                <div class="trace-body">${renderMarkdown(item.body || "")}</div>
              </article>
            `;
          }
          if (item.type === "system") {
            return `
              <article class="trace-card">
                <div class="trace-kind">system • ${escapeHtml(item.phase || "")}</div>
                <h3>${escapeHtml(item.title || "System event")}</h3>
                <div class="trace-body">${renderMarkdown(item.body || "")}</div>
              </article>
            `;
          }
          if (item.type === "agent_message") {
            return `
              <article class="trace-card">
                <div class="trace-kind">agent message • ${escapeHtml(item.phase || "")}</div>
                <h3>${escapeHtml(item.title || "Agent message")}</h3>
                <div class="trace-body">${renderMarkdown(item.body || "")}</div>
              </article>
            `;
          }
          return `
            <article class="trace-card">
              <div class="trace-kind">${escapeHtml(item.type || "event")}${item.phase ? ` • ${escapeHtml(item.phase)}` : ""}</div>
              <h3>${escapeHtml(item.title || item.message || "Agent event")}</h3>
              ${item.body ? `<div class="trace-body">${renderMarkdown(item.body)}</div>` : ""}
            </article>
          `;
        })
        .join("")
    : `<div class="empty-state">The live feed will show reasoning summaries, plan updates, commands, searches, tool calls, and surfaced agent messages here.</div>`;
}

function renderFailure(message) {
  els.answerEmpty.classList.add("hidden");
  els.answerBody.classList.remove("hidden");
  els.answerBody.innerHTML = `<p>${escapeHtml(message)}</p>`;
  els.citationsList.innerHTML = `<div class="empty-state">No citations.</div>`;
  els.traceList.innerHTML = `<div class="empty-state">No trace.</div>`;
  els.citationCount.textContent = "0";
  els.traceCount.textContent = "0";
  els.gapsBlock.classList.add("hidden");
  els.followupsBlock.classList.add("hidden");
  els.confidenceBadge.classList.add("hidden");
  resetSaveState();
}

function hydrateThread() {
  els.threadId.textContent = state.threadId ? `${state.threadId.slice(0, 8)}…` : "New";
}

function setRunState(kind, label) {
  els.runState.className = `run-state ${kind}`;
  els.runState.textContent = label;
}

function clearAnswerShell() {
  els.answerEmpty.classList.remove("hidden");
  els.answerBody.classList.add("hidden");
  els.answerBody.innerHTML = "";
  els.gapsBlock.classList.add("hidden");
  els.followupsBlock.classList.add("hidden");
  els.confidenceBadge.classList.add("hidden");
}

function showSaveButton() {
  els.saveAnswerButton.classList.remove("hidden");
}

function resetSaveState() {
  els.saveStatus.classList.add("hidden");
  els.saveStatus.textContent = "";
  els.saveStatus.innerHTML = "";
  els.saveAnswerButton.classList.add("hidden");
}

function setSaveState(message, href = "") {
  els.saveStatus.classList.remove("hidden");
  if (href) {
    els.saveStatus.innerHTML = `<a href="${href}" target="_blank" rel="noreferrer">${escapeHtml(message)}</a>`;
  } else {
    els.saveStatus.textContent = message;
  }
}

function formatUsage(usage) {
  if (!usage) {
    return "usage unavailable";
  }
  return `${usage.input_tokens || 0} in / ${usage.output_tokens || 0} out`;
}

function renderMarkdown(source) {
  const lines = String(source || "").split("\n");
  const html = [];
  let inList = false;

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      if (inList) {
        html.push("</ul>");
        inList = false;
      }
      continue;
    }
    if (line.startsWith("### ")) {
      if (inList) {
        html.push("</ul>");
        inList = false;
      }
      html.push(`<h3>${renderInline(line.slice(4))}</h3>`);
      continue;
    }
    if (line.startsWith("## ")) {
      if (inList) {
        html.push("</ul>");
        inList = false;
      }
      html.push(`<h2>${renderInline(line.slice(3))}</h2>`);
      continue;
    }
    if (line.startsWith("# ")) {
      if (inList) {
        html.push("</ul>");
        inList = false;
      }
      html.push(`<h1>${renderInline(line.slice(2))}</h1>`);
      continue;
    }
    if (line.startsWith("- ")) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${renderInline(line.slice(2))}</li>`);
      continue;
    }
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
    html.push(`<p>${renderInline(line)}</p>`);
  }

  if (inList) {
    html.push("</ul>");
  }
  return html.join("");
}

function extractAgentMessageBody(text) {
  const raw = String(text || "").trim();
  if (!raw) {
    return "";
  }
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.answer_markdown === "string" && parsed.answer_markdown.trim()) {
        return parsed.answer_markdown;
      }
      if (typeof parsed.concise_answer === "string" && parsed.concise_answer.trim()) {
        return parsed.concise_answer;
      }
    }
  } catch {
    // fall through to raw text
  }
  return raw;
}

function renderInline(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDuration(ms) {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}
