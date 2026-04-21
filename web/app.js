const state = {
  threadId: localStorage.getItem("vaultThreadId") || "",
  health: null,
  latestQuestion: "",
  latestResult: null,
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
  resetSaveState();

  try {
    const response = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        threadId: state.threadId || null,
        reasoningEffort: els.reasoning.value,
        includeWebSearch: els.webSearch.checked,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Query failed");
    }

    state.threadId = data.threadId || "";
    state.latestResult = data;
    if (state.threadId) {
      localStorage.setItem("vaultThreadId", state.threadId);
    }
    hydrateThread();
    renderAnswer(data.answer);
    renderCitations(data.answer.citations || []);
    renderTrace(data.trace || []);
    showSaveButton();
    els.modelName.textContent = data.meta?.model || state.health?.model || "-";
    setRunState("done", `Done in ${formatDuration(data.meta?.durationMs || 0)}`);
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
                <div class="trace-kind">command</div>
                <h3>${escapeHtml(item.command)}</h3>
                <div class="muted">exit ${escapeHtml(String(item.exit_code))}</div>
                <div class="trace-command">${escapeHtml(item.output_preview || "")}</div>
              </article>
            `;
          }
          if (item.type === "web_search") {
            return `
              <article class="trace-card">
                <div class="trace-kind">web search</div>
                <h3>${escapeHtml(item.query)}</h3>
              </article>
            `;
          }
          if (item.type === "mcp_tool_call") {
            return `
              <article class="trace-card">
                <div class="trace-kind">mcp tool</div>
                <h3>${escapeHtml(`${item.server}.${item.tool}`)}</h3>
                <div class="muted">${escapeHtml(item.status || "")}${item.error ? ` • ${escapeHtml(item.error)}` : ""}</div>
              </article>
            `;
          }
          if (item.type === "todo_list") {
            return `
              <article class="trace-card">
                <div class="trace-kind">plan</div>
                <h3>Agent todo list</h3>
                <div class="trace-command">${escapeHtml(
                  item.items.map((todo) => `${todo.completed ? "[x]" : "[ ]"} ${todo.text}`).join("\n"),
                )}</div>
              </article>
            `;
          }
          return `
            <article class="trace-card">
              <div class="trace-kind">${escapeHtml(item.type || "event")}</div>
              <h3>${escapeHtml(item.message || "Agent event")}</h3>
            </article>
          `;
        })
        .join("")
    : `<div class="empty-state">The backend will show shell, search, and tool activity here.</div>`;
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
