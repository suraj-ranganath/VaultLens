const state = {
  threadId: localStorage.getItem("vaultThreadId") || "",
  health: null,
  latestQuestion: "",
  latestResult: null,
  feed: [],
  chats: [],
  currentChat: null,
  pendingTurn: null,
  isStreaming: false,
  routeChatId: "",
  routeTurnId: "",
  workspace: localStorage.getItem("vaultWorkspace") || "chat",
  kb: {
    loaded: false,
    loading: false,
    data: null,
    filters: {
      search: "",
      type: "all",
      status: "all",
      priority: "all",
      tag: "",
      topic: "",
      source: "",
      sort: "recent",
    },
    selectedPath: "",
  },
};

const els = {
  form: document.querySelector("#query-form"),
  question: document.querySelector("#question-input"),
  reasoning: document.querySelector("#reasoning-select"),
  webSearch: document.querySelector("#web-search-toggle"),
  submit: document.querySelector("#submit-button"),
  runState: document.querySelector("#run-state"),
  healthStatus: document.querySelector("#health-status"),
  modelName: document.querySelector("#model-name"),
  threadId: document.querySelector("#thread-id"),
  chatTitle: document.querySelector("#chat-title"),
  chatSubtitle: document.querySelector("#chat-subtitle"),
  saveAnswerButton: document.querySelector("#save-answer-button"),
  saveStatus: document.querySelector("#save-status"),
  newThreadButton: document.querySelector("#new-thread-button"),
  chatCount: document.querySelector("#chat-count"),
  chatList: document.querySelector("#chat-list"),
  conversationViewport: document.querySelector("#conversation-viewport"),
  conversation: document.querySelector("#conversation"),
  conversationEmpty: document.querySelector("#conversation-empty"),
  presets: [...document.querySelectorAll(".preset")],
  workspaceTabs: [...document.querySelectorAll("[data-workspace]")],
  workspacePanels: [...document.querySelectorAll("[data-workspace-panel]")],
  kbFreshness: document.querySelector("#kb-freshness"),
  kbSearch: document.querySelector("#kb-search"),
  kbSaveView: document.querySelector("#kb-save-view"),
  kbRefresh: document.querySelector("#kb-refresh"),
  kbStats: document.querySelector("#kb-stats"),
  kbViews: document.querySelector("#kb-views"),
  kbType: document.querySelector("#kb-type"),
  kbStatus: document.querySelector("#kb-status"),
  kbPriority: document.querySelector("#kb-priority"),
  kbSort: document.querySelector("#kb-sort"),
  kbTopicCloud: document.querySelector("#kb-topic-cloud"),
  kbTagCloud: document.querySelector("#kb-tag-cloud"),
  kbFocusGrid: document.querySelector("#kb-focus-grid"),
  kbMatchCount: document.querySelector("#kb-match-count"),
  kbItems: document.querySelector("#kb-items"),
  kbPreview: document.querySelector("#kb-preview"),
};

Object.assign(state, readRouteFromLocation());

boot();

async function boot() {
  bindEvents();
  switchWorkspace(state.workspace, { persist: false });
  hydrateThread();
  renderHeader();
  await refreshHealth();
  await refreshChats();
  if (state.workspace === "knowledge") {
    await refreshKnowledgeBase();
  }
  renderConversation({ scroll: true });
}

function bindEvents() {
  els.form.addEventListener("submit", onSubmit);
  els.saveAnswerButton.addEventListener("click", onSaveAnswer);
  els.newThreadButton.addEventListener("click", () => {
    resetComposerState();
  });

  els.presets.forEach((button) => {
    button.addEventListener("click", () => {
      els.question.value = button.dataset.question || "";
      els.question.focus();
    });
  });

  els.workspaceTabs.forEach((button) => {
    button.addEventListener("click", () => {
      switchWorkspace(button.dataset.workspace || "chat");
    });
  });

  els.kbRefresh?.addEventListener("click", () => {
    void refreshKnowledgeBase();
  });
  els.kbSaveView?.addEventListener("click", onSaveKnowledgeView);
  els.kbSearch?.addEventListener("input", debounce(() => {
    state.kb.filters.search = els.kbSearch.value.trim();
    void refreshKnowledgeBase();
  }, 220));
  [els.kbType, els.kbStatus, els.kbPriority, els.kbSort].forEach((select) => {
    select?.addEventListener("change", () => {
      state.kb.filters.type = els.kbType.value;
      state.kb.filters.types = [];
      state.kb.filters.status = els.kbStatus.value;
      state.kb.filters.priority = els.kbPriority.value;
      state.kb.filters.sort = els.kbSort.value;
      void refreshKnowledgeBase();
    });
  });
  els.kbTopicCloud?.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-topic]");
    if (!chip) return;
    state.kb.filters.topic = state.kb.filters.topic === chip.dataset.topic ? "" : chip.dataset.topic;
    void refreshKnowledgeBase();
  });
  els.kbTagCloud?.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-tag]");
    if (!chip) return;
    state.kb.filters.tag = state.kb.filters.tag === chip.dataset.tag ? "" : chip.dataset.tag;
    void refreshKnowledgeBase();
  });
  els.kbViews?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-view-id]");
    if (!button || !state.kb.data) return;
    const view = (state.kb.data.views || []).find((entry) => entry.id === button.dataset.viewId);
    if (!view) return;
    state.kb.filters = { ...state.kb.filters, ...normalizeClientFilters(view.filters || {}) };
    syncKnowledgeControls();
    void refreshKnowledgeBase();
  });
  els.kbItems?.addEventListener("click", (event) => {
    const previewButton = event.target.closest("[data-preview-path]");
    if (previewButton) {
      event.preventDefault();
      selectKnowledgeItem(previewButton.dataset.previewPath || "");
    }
  });

  els.conversation.addEventListener("click", (event) => {
    const followup = event.target.closest("[data-followup-question]");
    if (!followup) {
      return;
    }
    els.question.value = followup.dataset.followupQuestion || "";
    els.question.focus();
  });

  window.addEventListener("popstate", () => {
    const route = readRouteFromLocation();
    state.routeChatId = route.routeChatId;
    state.routeTurnId = route.routeTurnId;
    if (state.routeChatId) {
      void loadChat(state.routeChatId, {
        turnId: state.routeTurnId || "",
        updateRoute: false,
        preserveSaveState: true,
      });
      return;
    }
    resetComposerState({ clearRouteState: false });
  });
}

function switchWorkspace(workspace, options = {}) {
  const target = workspace === "knowledge" ? "knowledge" : "chat";
  state.workspace = target;
  if (options.persist !== false) {
    localStorage.setItem("vaultWorkspace", target);
  }
  els.workspaceTabs.forEach((button) => {
    button.classList.toggle("active", button.dataset.workspace === target);
  });
  els.workspacePanels.forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.workspacePanel !== target);
  });
  if (target === "knowledge" && !state.kb.loaded && !state.kb.loading) {
    void refreshKnowledgeBase();
  }
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
  renderHeader();
}

async function refreshChats() {
  try {
    const response = await fetch("/api/chats");
    const data = await response.json();
    state.chats = data.chats || [];
    renderChatList();
    if (!state.currentChat && !state.pendingTurn && state.chats.length) {
      const targetChatId = state.routeChatId || state.chats[0].id;
      await loadChat(targetChatId, {
        turnId: state.routeChatId === targetChatId ? state.routeTurnId : "",
        updateRoute: false,
      });
      return;
    }
  } catch {
    state.chats = [];
    renderChatList();
  }
  renderHeader();
}

async function onSubmit(event) {
  event.preventDefault();
  const question = els.question.value.trim();
  if (!question) {
    els.question.focus();
    return;
  }

  state.latestQuestion = question;
  state.latestResult = null;
  state.feed = [];
  state.isStreaming = true;
  state.pendingTurn = {
    id: `pending-${Date.now()}`,
    question,
    answer: null,
    trace: [],
    feed: [],
    usage: null,
    billing: null,
    meta: {
      model: state.health?.model || els.modelName.textContent || "-",
      reasoningEffort: els.reasoning.value,
      includeWebSearch: els.webSearch.checked,
    },
    startedAt: new Date().toISOString(),
    completedAt: null,
    live: true,
  };

  setRunState("running", "Working");
  resetSaveState();
  renderHeader();
  renderConversation({ scroll: true });
  els.submit.disabled = true;

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
    await refreshChats();
  } catch (error) {
    state.isStreaming = false;
    setRunState("error", "Error");
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
    renderHeader();
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
    if (state.pendingTurn) {
      state.pendingTurn.usage = event.usage;
    }
    return;
  }

  if (event.type === "thread.token_usage.updated") {
    appendFeed({
      type: "system",
      phase: "updated",
      title: "Token usage updated",
      body: formatUsage(event.usage),
    });
    if (state.pendingTurn) {
      state.pendingTurn.usage = event.usage;
    }
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
    state.isStreaming = false;
    hydrateThread();

    if (state.pendingTurn) {
      state.pendingTurn.answer = event.answer;
      state.pendingTurn.usage = event.usage;
      state.pendingTurn.billing = event.billing;
      state.pendingTurn.meta = event.meta;
      state.pendingTurn.completedAt = new Date().toISOString();
      state.pendingTurn.feed = state.feed.slice();
    }

    showSaveButton();
    els.modelName.textContent = event.meta?.model || state.health?.model || "-";
    setRunState("done", `Done in ${formatDuration(event.meta?.durationMs || 0)}`);
    renderHeader();
    renderConversation({ scroll: true });

    if (event.chat?.id) {
      void loadChat(event.chat.id, { preserveSaveState: true });
    }
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
    return {
      type: "agent_message",
      phase,
      title: "Agent message",
      body: extractAgentMessageBody(item.text),
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
  state.feed.push(entry);
  state.feed = state.feed.slice(-120);
  if (state.pendingTurn) {
    state.pendingTurn.feed = state.feed.slice();
  }
  renderConversation({ scroll: true });
}

function renderHeader() {
  const titleSource =
    state.currentChat?.title ||
    state.pendingTurn?.question ||
    state.currentChat?.turns?.at(-1)?.question ||
    "New chat";

  const turnCount = state.currentChat?.turns?.length || 0;
  const subtitleBits = [];
  if (turnCount) {
    subtitleBits.push(`${turnCount} saved turns`);
  } else if (state.pendingTurn) {
    subtitleBits.push("New thread");
  } else {
    subtitleBits.push("Search your vault, recent links, jobs, ideas, and decisions.");
  }

  if (state.health?.model || els.modelName.textContent !== "-") {
    subtitleBits.push(els.modelName.textContent || state.health?.model || "-");
  }
  subtitleBits.push(els.webSearch.checked ? "web search on" : "web search off");
  subtitleBits.push(state.threadId ? `thread ${state.threadId.slice(0, 8)}…` : "no thread yet");

  els.chatTitle.textContent = truncate(titleSource, 96);
  els.chatSubtitle.textContent = subtitleBits.join(" • ");
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
    els.saveStatus.innerHTML = `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(message)}</a>`;
  } else {
    els.saveStatus.textContent = message;
  }
}

function renderFailure(message) {
  if (state.pendingTurn) {
    state.pendingTurn.error = message;
    state.pendingTurn.completedAt = new Date().toISOString();
    state.pendingTurn.feed = state.feed.slice();
  }
  renderConversation({ scroll: true });
}

function renderChatList() {
  const chats = [...state.chats].sort((left, right) => {
    const leftTime = getTimestamp(left.updatedAt || left.createdAt);
    const rightTime = getTimestamp(right.updatedAt || right.createdAt);
    return rightTime - leftTime;
  });

  els.chatCount.textContent = String(chats.length);
  els.chatList.innerHTML = chats.length
    ? groupChatsByRecency(chats)
        .map(
          ({ label, chats: group }) => `
            <section class="chat-history-group">
              <div class="chat-history-label">${escapeHtml(label)}</div>
              ${group
                .map((chat) => {
                  const title = chat.title || chat.lastQuestion || "Untitled chat";
                  const preview = summarizeText(chat.lastAnswer || chat.lastQuestion || "Open this chat", 120);
                  return `
                    <button
                      class="chat-button ${chat.id === state.currentChat?.id ? "active" : ""}"
                      type="button"
                      data-chat-id="${escapeHtml(chat.id)}"
                      title="${escapeHtml(title)}"
                    >
                      <div class="chat-date-row">
                        <span class="chat-date-pill">${escapeHtml(formatChatTimestamp(chat.updatedAt || chat.createdAt))}</span>
                      </div>
                      <div class="chat-title">${escapeHtml(title)}</div>
                      <div class="chat-preview">${escapeHtml(preview)}</div>
                    </button>
                  `;
                })
                .join("")}
            </section>
          `,
        )
        .join("")
    : `<div class="empty-state">No saved chats yet.</div>`;

  [...els.chatList.querySelectorAll(".chat-button")].forEach((button) => {
    button.addEventListener("click", () => {
      void loadChat(button.dataset.chatId || "");
    });
  });
}

async function loadChat(chatId, options = {}) {
  if (!chatId) {
    return;
  }
  const response = await fetch(`/api/chats/${encodeURIComponent(chatId)}`);
  const data = await response.json();
  if (!response.ok) {
    return;
  }

  state.currentChat = data.chat;
  state.pendingTurn = null;
  state.feed = [];
  state.threadId = data.chat.threadId || state.threadId;
  state.isStreaming = false;
  state.routeChatId = data.chat.id || "";
  state.routeTurnId = options.turnId || "";

  if (state.threadId) {
    localStorage.setItem("vaultThreadId", state.threadId);
  }

  if (options.updateRoute !== false) {
    updateChatRoute(state.routeChatId, state.routeTurnId);
  }

  hydrateThread();
  renderChatList();
  renderHeader();
  renderConversation({ scroll: !options.turnId, turnId: options.turnId || "" });
  setRunState("idle", "Loaded");

  if (!options.preserveSaveState) {
    resetSaveState();
  }
}

function resetComposerState(options = {}) {
  state.threadId = "";
  state.latestQuestion = "";
  state.latestResult = null;
  state.feed = [];
  state.currentChat = null;
  state.pendingTurn = null;
  state.isStreaming = false;
  state.routeChatId = "";
  state.routeTurnId = "";

  localStorage.removeItem("vaultThreadId");
  if (options.clearRouteState !== false) {
    clearRoute();
  }
  hydrateThread();
  renderHeader();
  setRunState("idle", "Idle");
  resetSaveState();
  renderChatList();
  renderConversation();
  els.question.focus();
}

async function refreshKnowledgeBase() {
  if (!els.kbItems) return;
  state.kb.loading = true;
  renderKnowledgeLoading();
  try {
    const params = new URLSearchParams();
    const filters = state.kb.filters;
    Object.entries(filters).forEach(([key, value]) => {
      if (Array.isArray(value)) {
        if (value.length) params.set(key, value.join(","));
      } else if (value && value !== "all") {
        params.set(key, value);
      }
    });
    params.set("limit", "160");
    const response = await fetch(`/api/kb?${params.toString()}`);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Failed to load knowledge base.");
    }
    state.kb.data = data;
    state.kb.loaded = true;
    state.kb.loading = false;
    syncKnowledgeControls();
    renderKnowledgeBase();
  } catch (error) {
    state.kb.loading = false;
    els.kbItems.innerHTML = `<div class="kb-empty">Could not load the knowledge dashboard: ${escapeHtml(error instanceof Error ? error.message : String(error))}</div>`;
  }
}

function renderKnowledgeLoading() {
  if (!state.kb.loaded) {
    els.kbStats.innerHTML = renderStatSkeletons();
    els.kbItems.innerHTML = `<div class="kb-empty">Reading the compiled vault digest…</div>`;
  }
}

function renderKnowledgeBase() {
  const data = state.kb.data;
  if (!data) return;
  els.kbFreshness.textContent = data.generatedAt ? `Digest generated ${formatKnowledgeDate(data.generatedAt)}` : "Digest loaded";
  els.kbStats.innerHTML = renderKnowledgeStats(data);
  populateKnowledgeSelects(data);
  renderKnowledgeViews(data.views || []);
  renderKnowledgeClouds(data);
  renderKnowledgeFocus(data);
  els.kbMatchCount.textContent = `${data.totalMatches || 0} matches`;
  els.kbItems.innerHTML = data.pages?.length
    ? data.pages.map((page) => renderKnowledgeItem(page)).join("")
    : `<div class="kb-empty">No notes match this dashboard. Try clearing a filter.</div>`;
  if (state.kb.selectedPath) {
    renderKnowledgePreview(state.kb.selectedPath);
  }
}

function renderKnowledgeStats(data) {
  const typeCount = sumCounts(data.counts?.byType || []);
  const urgentCount = data.focus?.openDeadlinePages?.length || 0;
  const highCount = data.focus?.highPriority?.length || 0;
  return [
    statCard("Notes indexed", formatNumber(typeCount || data.pageCount || 0), "compiled markdown files"),
    statCard("Open tasks", formatNumber(data.tasks?.openCount || 0), "from task ledger"),
    statCard("Urgent", formatNumber(urgentCount), "deadlines within a week"),
    statCard("High signal", formatNumber(highCount), "high / critical priority"),
  ].join("");
}

function statCard(label, value, sublabel) {
  return `
    <article class="kb-stat-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <p>${escapeHtml(sublabel)}</p>
    </article>
  `;
}

function renderStatSkeletons() {
  return [1, 2, 3, 4].map(() => `<div class="kb-stat-card skeleton"></div>`).join("");
}

function populateKnowledgeSelects(data) {
  fillSelect(els.kbType, [{ name: "all", count: data.pageCount }, ...(data.counts?.byType || [])], state.kb.filters.type);
  fillSelect(els.kbStatus, [{ name: "all", count: data.pageCount }, ...(data.counts?.byStatus || [])], state.kb.filters.status);
  fillSelect(els.kbPriority, [{ name: "all", count: data.pageCount }, ...(data.counts?.byPriority || [])], state.kb.filters.priority);
}

function fillSelect(select, entries, selected) {
  if (!select) return;
  const prior = select.value || selected;
  select.innerHTML = entries
    .map((entry) => `<option value="${escapeHtml(entry.name)}">${escapeHtml(labelize(entry.name))}${entry.count != null ? ` (${entry.count})` : ""}</option>`)
    .join("");
  select.value = entries.some((entry) => entry.name === prior) ? prior : "all";
}

function renderKnowledgeViews(views) {
  els.kbViews.innerHTML = views.length
    ? views
        .map(
          (view) => `
            <button class="view-button" type="button" data-view-id="${escapeHtml(view.id)}">
              <span>${escapeHtml(view.name)}</span>
              <small>${escapeHtml(view.builtin ? "preset" : `${view.matchCount || 0} items`)}</small>
            </button>
          `,
        )
        .join("")
    : `<div class="kb-muted">No saved dashboards yet.</div>`;
}

function renderKnowledgeClouds(data) {
  els.kbTopicCloud.innerHTML = renderChipCloud(data.counts?.topics || [], "topic", state.kb.filters.topic);
  els.kbTagCloud.innerHTML = renderChipCloud(data.counts?.tags || [], "tag", state.kb.filters.tag);
}

function renderChipCloud(entries, kind, active) {
  return entries.slice(0, 24).map((entry) => {
    const attr = kind === "topic" ? "data-topic" : "data-tag";
    return `
      <button class="kb-chip ${entry.name === active ? "active" : ""}" type="button" ${attr}="${escapeHtml(entry.name)}">
        ${escapeHtml(entry.name)} <span>${entry.count}</span>
      </button>
    `;
  }).join("") || `<div class="kb-muted">No ${kind}s yet.</div>`;
}

function renderKnowledgeFocus(data) {
  const focus = data.focus || {};
  const groups = [
    { title: "Urgent", kicker: "this week", pages: focus.openDeadlinePages || [] },
    { title: "High signal", kicker: "priority", pages: focus.highPriority || [] },
    { title: "Recent knowledge", kicker: "fresh saves", pages: focus.recentKnowledge || [] },
  ];
  els.kbFocusGrid.innerHTML = groups
    .map(
      (group) => `
        <article class="focus-card">
          <div class="focus-topline"><span>${escapeHtml(group.kicker)}</span><strong>${group.pages.length}</strong></div>
          <h4>${escapeHtml(group.title)}</h4>
          ${
            group.pages.length
              ? `<ul>${group.pages
                  .slice(0, 4)
                  .map((page) => `<li><a href="${escapeHtml(buildVaultPath(page.path))}" target="_blank" rel="noreferrer">${escapeHtml(page.title || page.path)}</a></li>`)
                  .join("")}</ul>`
              : `<p>No active items in this slice.</p>`
          }
        </article>
      `,
    )
    .join("");
}

function renderKnowledgeItem(page) {
  const date = page.discovered_on || page.published_on || page.deadline || "";
  const chips = [
    page.type,
    page.priority,
    page.status,
    page.sourceSite,
    ...(page.topics || []).slice(0, 2),
  ].filter(Boolean);
  return `
    <article class="kb-item ${page.path === state.kb.selectedPath ? "selected" : ""}">
      <button class="kb-item-main" type="button" data-preview-path="${escapeHtml(page.path)}">
        <span class="kb-item-type">${escapeHtml(page.type || "note")}</span>
        <h4>${escapeHtml(page.title || page.path)}</h4>
        <p>${escapeHtml(page.summary || page.why_saved || "No summary yet.")}</p>
        <div class="kb-item-chips">${chips.map((chip) => `<span>${escapeHtml(chip)}</span>`).join("")}</div>
      </button>
      <div class="kb-item-actions">
        ${date ? `<time>${escapeHtml(date)}</time>` : ""}
        ${page.url ? `<a href="${escapeHtml(page.url)}" target="_blank" rel="noreferrer">Source</a>` : ""}
        <a href="${escapeHtml(page.vaultUrl || buildVaultPath(page.path))}" target="_blank" rel="noreferrer">Vault</a>
      </div>
    </article>
  `;
}

function selectKnowledgeItem(path) {
  state.kb.selectedPath = path;
  renderKnowledgeBase();
}

function renderKnowledgePreview(relativePath) {
  const page = (state.kb.data?.pages || []).find((entry) => entry.path === relativePath);
  if (!page) return;
  els.kbPreview.innerHTML = `
    <p class="knowledge-kicker">${escapeHtml(page.type || "note")}</p>
    <h3>${escapeHtml(page.title || page.path)}</h3>
    <p>${escapeHtml(page.summary || page.why_saved || "No summary yet.")}</p>
    <div class="preview-facts">
      ${previewFact("Path", page.path)}
      ${previewFact("Website", page.sourceSite)}
      ${previewFact("Author", [page.author, page.authorCredential].filter(Boolean).join(" - "))}
      ${previewFact("Added", page.discovered_on)}
      ${previewFact("Published", page.published_on)}
      ${previewFact("Priority", page.priority)}
    </div>
    <div class="preview-actions">
      ${page.url ? `<a href="${escapeHtml(page.url)}" target="_blank" rel="noreferrer">Primary source</a>` : ""}
      <a href="${escapeHtml(page.vaultUrl || buildVaultPath(page.path))}" target="_blank" rel="noreferrer">Open rendered note</a>
      <a href="${escapeHtml(page.rawUrl || `/vault-raw/${encodeURI(page.path)}`)}" target="_blank" rel="noreferrer">Raw markdown</a>
    </div>
  `;
}

function previewFact(label, value) {
  return value ? `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>` : "";
}

async function onSaveKnowledgeView() {
  const fallback = buildViewNameFromFilters(state.kb.filters);
  const name = window.prompt("Dashboard name", fallback);
  if (!name) return;
  els.kbSaveView.disabled = true;
  try {
    const response = await fetch("/api/kb/views", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, filters: state.kb.filters }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save dashboard.");
    await refreshKnowledgeBase();
    window.open(data.url, "_blank", "noreferrer");
  } catch (error) {
    window.alert(error instanceof Error ? error.message : String(error));
  } finally {
    els.kbSaveView.disabled = false;
  }
}

function syncKnowledgeControls() {
  if (els.kbSearch) els.kbSearch.value = state.kb.filters.search || "";
  if (els.kbType) els.kbType.value = state.kb.filters.types?.length ? "all" : state.kb.filters.type || "all";
  if (els.kbStatus) els.kbStatus.value = state.kb.filters.status || "all";
  if (els.kbPriority) els.kbPriority.value = state.kb.filters.priority || "all";
  if (els.kbSort) els.kbSort.value = state.kb.filters.sort || "recent";
}

function normalizeClientFilters(filters) {
  return {
    search: filters.search || "",
    type: filters.type || "all",
    types: Array.isArray(filters.types) ? filters.types : String(filters.types || "").split(",").map((item) => item.trim()).filter(Boolean),
    status: filters.status || "all",
    priority: filters.priority || "all",
    tag: filters.tag || "",
    topic: filters.topic || "",
    source: filters.source || "",
    sort: filters.sort || "recent",
  };
}

function buildViewNameFromFilters(filters) {
  const typeLabel = filters.types?.length ? filters.types.map(labelize).join(" + ") : filters.type !== "all" ? labelize(filters.type) : "Vault";
  const parts = [typeLabel, filters.topic || filters.tag || "", filters.priority !== "all" ? labelize(filters.priority) : ""].filter(Boolean);
  return `${parts.join(" ")} Dashboard`;
}

function renderConversation({ scroll = false, turnId = "" } = {}) {
  const turns = [];
  if (state.currentChat?.turns?.length) {
    turns.push(
      ...state.currentChat.turns.map((turn) => ({
        ...turn,
        live: false,
      })),
    );
  }
  if (state.pendingTurn) {
    turns.push(state.pendingTurn);
  }

  const hasConversation = turns.length > 0;
  els.conversationEmpty.classList.toggle("hidden", hasConversation);
  els.conversation.classList.toggle("hidden", !hasConversation);

  if (!hasConversation) {
    els.conversation.innerHTML = "";
    return;
  }

  els.conversation.innerHTML = turns.map((turn, index) => renderTurnBlock(turn, index === turns.length - 1)).join("");
  renderHeader();

  if (scroll) {
    requestAnimationFrame(() => {
      els.conversationViewport.scrollTop = els.conversationViewport.scrollHeight;
    });
    return;
  }

  if (turnId) {
    requestAnimationFrame(() => {
      scrollToTurn(turnId);
    });
  }
}

function renderTurnBlock(turn, isLast) {
  return `
    <section class="turn-block" id="turn-${escapeHtml(turn.id || "")}">
      ${renderUserMessage(turn)}
      ${renderAssistantMessage(turn, isLast)}
    </section>
  `;
}

function renderUserMessage(turn) {
  return `
    <article class="message user-message">
      <div class="message-body">
        <div class="message-header">
          <div class="message-role">You</div>
          <time class="message-time">${escapeHtml(formatMessageTime(turn.startedAt || turn.completedAt))}</time>
        </div>
        <div class="message-bubble user-bubble">
          <div class="assistant-copy">${renderMarkdown(turn.question || "")}</div>
        </div>
      </div>
    </article>
  `;
}

function renderAssistantMessage(turn, isLast) {
  const answer = turn.answer || {};
  const citations = normalizeCitations(answer.citations || [], {
    chatId: state.currentChat?.id || "",
    turnId: turn.id || "",
  });
  const citationLookup = buildCitationLookup(citations);
  const trace = turn.live ? turn.feed || [] : normalizeSavedFeed(turn.feed || turn.trace || []);
  const body =
    turn.error
      ? `<p>${escapeHtml(turn.error)}</p>`
      : answer.answer_markdown || answer.concise_answer
        ? renderMarkdown(answer.answer_markdown || answer.concise_answer || "", { citationLookup })
        : renderThinkingState();

  const meta = renderAssistantMeta(turn, answer);
  const gaps = renderGaps(answer.gaps || []);
  const followups = renderFollowups(answer.follow_up_questions || []);
  const sourcesDetail = citations.length ? renderSourcesDetail(citations, turn) : "";
  const traceDetail = trace.length || turn.live ? renderTraceDetail(trace, Boolean(turn.live && state.isStreaming)) : "";

  return `
    <article class="message assistant-message">
      <div class="message-avatar">V</div>
      <div class="message-body">
        <div class="message-header">
          <div class="message-role">Vault</div>
          <time class="message-time">${escapeHtml(formatMessageTime(turn.completedAt || turn.startedAt))}</time>
        </div>

        <div class="message-bubble assistant-bubble">
          <div class="assistant-copy">${body}</div>
          ${meta}
          <div class="message-extras">
            ${gaps}
            ${followups}
            ${sourcesDetail}
            ${traceDetail}
          </div>
        </div>
      </div>
    </article>
  `;
}

function renderAssistantMeta(turn, answer) {
  const pills = [];
  if (answer.confidence) {
    pills.push(
      `<div class="meta-pill confidence-${escapeHtml(answer.confidence)}">${escapeHtml(answer.confidence)} confidence</div>`,
    );
  }
  if (turn.usage) {
    pills.push(`<div class="meta-pill">${escapeHtml(formatUsage(turn.usage))}</div>`);
  }
  if (turn.billing?.billingMode) {
    pills.push(`<div class="meta-pill">${escapeHtml(formatBillingMode(turn.billing.billingMode))}</div>`);
  }
  if (turn.meta?.model) {
    pills.push(`<div class="meta-pill">${escapeHtml(turn.meta.model)}</div>`);
  }
  return pills.length ? `<div class="assistant-meta">${pills.join("")}</div>` : "";
}

function formatBillingMode(value) {
  if (value === "codex_subscription") {
    return "Codex limits";
  }
  return String(value || "").replaceAll("_", " ");
}

function renderGaps(gaps) {
  if (!gaps.length) {
    return "";
  }
  return `
    <section class="gap-box">
      <h4>Known gaps</h4>
      <ul>${gaps.map((gap) => `<li>${escapeHtml(gap)}</li>`).join("")}</ul>
    </section>
  `;
}

function renderFollowups(followUps) {
  if (!followUps.length) {
    return "";
  }
  return `
    <div class="followup-row">
      ${followUps
        .map(
          (question) =>
            `<button class="followup-chip" type="button" data-followup-question="${escapeHtml(question)}">${escapeHtml(question)}</button>`,
        )
        .join("")}
    </div>
  `;
}

function renderSourcesDetail(citations, turn) {
  return `
    <details class="message-detail">
      <summary class="detail-summary">
        <span>Sources</span>
        <span>${citations.length}</span>
      </summary>
      <div class="detail-panel">
        <div class="sources-grid">
          ${citations
            .map(
              (citation) => `
                <article class="source-card">
                  <div class="source-topline">
                    <span class="source-number">${citation.number}</span>
                    <span class="source-kind">${escapeHtml(citation.note_type || "note")}</span>
                  </div>
                  <div class="source-title">
                    <a href="${escapeHtml(citation.source_url || citation.contextual_vault_url || citation.vault_url || `/vault/${encodeURI(citation.path)}`)}" target="_blank" rel="noreferrer">
                      ${escapeHtml(citation.title || citation.path)}
                    </a>
                  </div>
                  <p class="source-relevance">${escapeHtml(citation.relevance || "")}</p>
                  <div class="source-links">
                    ${isUsableHref(citation.source_url) ? `<a href="${escapeHtml(citation.source_url)}" target="_blank" rel="noreferrer">Primary source</a>` : ""}
                    <a href="${escapeHtml(citation.contextual_vault_url || citation.vault_url || `/vault/${encodeURI(citation.path)}`)}" target="_blank" rel="noreferrer">Vault note</a>
                    ${
                      state.currentChat?.id && turn?.id
                        ? `<a href="${escapeHtml(buildChatUrl(state.currentChat.id, turn.id))}">Chat turn</a>`
                        : ""
                    }
                  </div>
                </article>
              `,
            )
            .join("")}
        </div>
      </div>
    </details>
  `;
}

function renderTraceDetail(trace, open) {
  const detailOpen = open ? "open" : "";
  return `
    <details class="message-detail" ${detailOpen}>
      <summary class="detail-summary">
        <span>Agent run</span>
        <span>${trace.length}</span>
      </summary>
      <div class="detail-panel">
        ${
          trace.length
            ? `<div class="trace-list">${trace.map((item) => renderTraceItem(item)).join("")}</div>`
            : `<div class="empty-state">Waiting for reasoning, tool calls, and trace events.</div>`
        }
      </div>
    </details>
  `;
}

function renderTraceItem(item) {
  const kind = item.type || "event";
  const heading = item.title || item.message || "Agent event";
  const meta = item.meta || "";
  const body = item.body || "";

  if (item.type === "command_execution") {
    return `
      <article class="trace-card">
        <div class="trace-kind">command • ${escapeHtml(item.phase || "")}</div>
        <h4>${escapeHtml(heading)}</h4>
        <div class="trace-meta">${escapeHtml(meta)}</div>
        <div class="trace-command">${escapeHtml(body)}</div>
      </article>
    `;
  }

  if (item.type === "reasoning" || item.type === "agent_message" || item.type === "system") {
    return `
      <article class="trace-card">
        <div class="trace-kind">${escapeHtml(kind.replaceAll("_", " "))} • ${escapeHtml(item.phase || "")}</div>
        <h4>${escapeHtml(heading)}</h4>
        <div class="trace-body">${renderMarkdown(body)}</div>
      </article>
    `;
  }

  if (item.type === "todo_list") {
    return `
      <article class="trace-card">
        <div class="trace-kind">plan • ${escapeHtml(item.phase || "")}</div>
        <h4>${escapeHtml(heading)}</h4>
        <div class="trace-command">${escapeHtml(body)}</div>
      </article>
    `;
  }

  if (item.type === "web_search" || item.type === "mcp_tool_call") {
    return `
      <article class="trace-card">
        <div class="trace-kind">${escapeHtml(kind.replaceAll("_", " "))} • ${escapeHtml(item.phase || "")}</div>
        <h4>${escapeHtml(heading)}</h4>
        ${meta ? `<div class="trace-meta">${escapeHtml(meta)}</div>` : ""}
        ${body ? `<div class="trace-command">${escapeHtml(body)}</div>` : ""}
      </article>
    `;
  }

  return `
    <article class="trace-card">
      <div class="trace-kind">${escapeHtml(kind.replaceAll("_", " "))}${item.phase ? ` • ${escapeHtml(item.phase)}` : ""}</div>
      <h4>${escapeHtml(heading)}</h4>
      ${meta ? `<div class="trace-meta">${escapeHtml(meta)}</div>` : ""}
      ${body ? `<div class="trace-body">${renderMarkdown(body)}</div>` : ""}
    </article>
  `;
}

function renderThinkingState() {
  return `
    <div class="thinking-block">
      <div class="thinking-dots" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
      </div>
      <div>Searching your vault, recent context, and sources…</div>
    </div>
  `;
}

function normalizeCitations(citations, context = {}) {
  return citations.map((citation, index) => ({
    ...citation,
    number: index + 1,
    contextual_vault_url: citation.path
      ? buildContextualVaultUrl(citation.path, {
          chatId: context.chatId,
          turnId: context.turnId,
          sourceIndex: index + 1,
        })
      : citation.vault_url || "",
  }));
}

function buildCitationLookup(citations) {
  const lookup = new Map();
  citations.forEach((citation) => {
    const keys = [
      citation.path,
      citation.source_url,
      citation.vault_url,
      `/vault/${encodeURI(citation.path || "")}`,
    ]
      .filter(Boolean)
      .map(normalizeCitationKey);

    keys.forEach((key) => {
      if (!lookup.has(key)) {
        lookup.set(key, {
          number: citation.number,
          href: citation.source_url || citation.contextual_vault_url || citation.vault_url || "#",
          label: citation.title || citation.path || `Source ${citation.number}`,
        });
      }
    });
  });
  return lookup;
}

function normalizeCitationKey(value) {
  const trimmed = String(value || "").trim();
  if (/^https?:\/\//i.test(trimmed)) {
    try {
      const url = new URL(trimmed);
      url.hash = "";
      return url.toString().replace(/\/+$/, "");
    } catch {
      return trimmed.replace(/\/+$/, "");
    }
  }
  return trimmed.replace(/^https?:\/\/[^/]+/i, "").replace(/\/+$/, "");
}

function normalizeSavedFeed(feed) {
  return Array.isArray(feed)
    ? feed
        .map((entry) => {
          if (entry?.type === "item" && entry.item) {
            return formatFeedEntry(entry.phase, entry.item);
          }
          if (entry?.type === "thread.started") {
            return { type: "system", phase: "started", title: "Thread started", body: entry.threadId || "" };
          }
          if (entry?.type === "turn.started") {
            return { type: "system", phase: "started", title: "Turn started", body: "Agent is planning the vault search path." };
          }
          if (entry?.type === "turn.completed") {
            return { type: "system", phase: "completed", title: "Turn completed", body: `usage: ${formatUsage(entry.usage)}` };
          }
          if (entry?.type === "thread.token_usage.updated") {
            return { type: "system", phase: "updated", title: "Token usage updated", body: formatUsage(entry.usage) };
          }
          if (entry?.type === "error" || entry?.type === "turn.failed") {
            return { type: "error", phase: "failed", title: "Agent error", body: entry.message || entry.error || "" };
          }
          if (entry?.type) {
            return entry;
          }
          if (entry?.command || entry?.type === "command_execution") {
            return formatFeedEntry("completed", entry);
          }
          return null;
        })
        .filter(Boolean)
    : [];
}

function renderMarkdown(source, options = {}) {
  const lines = String(source || "").split("\n");
  const html = [];
  let listType = "";

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();

    if (!trimmed) {
      if (listType) {
        html.push(`</${listType}>`);
        listType = "";
      }
      continue;
    }

    if (trimmed.startsWith("### ")) {
      if (listType) {
        html.push(`</${listType}>`);
        listType = "";
      }
      html.push(`<h3>${renderInline(trimmed.slice(4), options)}</h3>`);
      continue;
    }
    if (trimmed.startsWith("## ")) {
      if (listType) {
        html.push(`</${listType}>`);
        listType = "";
      }
      html.push(`<h2>${renderInline(trimmed.slice(3), options)}</h2>`);
      continue;
    }
    if (trimmed.startsWith("# ")) {
      if (listType) {
        html.push(`</${listType}>`);
        listType = "";
      }
      html.push(`<h1>${renderInline(trimmed.slice(2), options)}</h1>`);
      continue;
    }

    const bulletMatch = trimmed.match(/^- (.+)$/);
    if (bulletMatch) {
      if (listType && listType !== "ul") {
        html.push(`</${listType}>`);
        listType = "";
      }
      if (!listType) {
        html.push("<ul>");
        listType = "ul";
      }
      html.push(`<li>${renderInline(bulletMatch[1], options)}</li>`);
      continue;
    }

    const orderedMatch = trimmed.match(/^\d+\.\s+(.+)$/);
    if (orderedMatch) {
      if (listType && listType !== "ol") {
        html.push(`</${listType}>`);
        listType = "";
      }
      if (!listType) {
        html.push("<ol>");
        listType = "ol";
      }
      html.push(`<li>${renderInline(orderedMatch[1], options)}</li>`);
      continue;
    }

    if (listType) {
      html.push(`</${listType}>`);
      listType = "";
    }
    html.push(`<p>${renderInline(trimmed, options)}</p>`);
  }

  if (listType) {
    html.push(`</${listType}>`);
  }
  return html.join("");
}

function renderInline(text, options = {}) {
  const citationLookup = options.citationLookup || new Map();
  let html = "";
  let cursor = 0;
  const source = String(text || "");

  while (cursor < source.length) {
    if (source[cursor] === "`") {
      const codeEnd = source.indexOf("`", cursor + 1);
      if (codeEnd !== -1) {
        html += `<code>${escapeHtml(source.slice(cursor + 1, codeEnd))}</code>`;
        cursor = codeEnd + 1;
        continue;
      }
    }

    if (source[cursor] === "[") {
      const closeBracket = findMarkdownLabelClose(source, cursor + 1);
      if (closeBracket !== -1) {
        const closeParen = source.indexOf(")", closeBracket + 2);
        if (closeParen !== -1) {
          const label = source.slice(cursor + 1, closeBracket);
          const href = source.slice(closeBracket + 2, closeParen);
          const normalizedHref = String(href || "").trim();
          if (!normalizedHref || normalizedHref === "\"" || normalizedHref === "'" || normalizedHref === "#") {
            html += formatInlinePlain(label);
            cursor = closeParen + 1;
            continue;
          }
          const citation = citationLookup.get(normalizeCitationKey(normalizedHref));
          if (citation) {
            html += `<a class="inline-citation" href="${escapeHtml(citation.href)}" target="_blank" rel="noreferrer" title="${escapeHtml(citation.label || label)}">${citation.number}</a>`;
          } else {
            html += `<a class="text-link" href="${escapeHtml(normalizedHref)}" target="_blank" rel="noreferrer">${formatInlinePlain(label)}</a>`;
          }
          cursor = closeParen + 1;
          continue;
        }
      }
    }

    const nextSpecial = findNextSpecialToken(source, cursor);
    html += formatInlinePlain(source.slice(cursor, nextSpecial));
    cursor = nextSpecial;
  }
  return html;
}

function formatInlinePlain(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function findNextSpecialToken(text, start) {
  const nextCode = text.indexOf("`", start);
  const nextLink = text.indexOf("[", start);
  const candidates = [nextCode, nextLink].filter((value) => value !== -1);
  return candidates.length ? Math.min(...candidates) : text.length;
}

function findMarkdownLabelClose(text, start) {
  for (let index = start; index < text.length - 1; index += 1) {
    if (text[index] === "]" && text[index + 1] === "(") {
      return index;
    }
  }
  return -1;
}

function isUsableHref(value) {
  const normalized = String(value || "").trim();
  if (!normalized || normalized === "\"" || normalized === "'" || normalized === "#") {
    return false;
  }
  return true;
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
    // fall through
  }
  return raw;
}

function formatUsage(usage) {
  if (!usage) {
    return "usage unavailable";
  }
  const bucket = usage.total || usage.last || usage;
  const cached = bucket.cached_input_tokens || 0;
  return `${bucket.input_tokens || 0} in • ${cached} cached • ${bucket.output_tokens || 0} out`;
}

function groupChatsByRecency(chats) {
  const buckets = [
    { label: "Today", chats: [] },
    { label: "Yesterday", chats: [] },
    { label: "Previous 7 Days", chats: [] },
    { label: "Older", chats: [] },
  ];

  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterdayStart = todayStart - 24 * 60 * 60 * 1000;
  const weekStart = todayStart - 7 * 24 * 60 * 60 * 1000;

  chats.forEach((chat) => {
    const timestamp = getTimestamp(chat.updatedAt || chat.createdAt);
    if (timestamp >= todayStart) {
      buckets[0].chats.push(chat);
      return;
    }
    if (timestamp >= yesterdayStart) {
      buckets[1].chats.push(chat);
      return;
    }
    if (timestamp >= weekStart) {
      buckets[2].chats.push(chat);
      return;
    }
    buckets[3].chats.push(chat);
  });

  return buckets.filter((bucket) => bucket.chats.length);
}

function getTimestamp(value) {
  if (!value) {
    return 0;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function formatChatTimestamp(value) {
  if (!value) {
    return "Unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

function buildContextualVaultUrl(relativePath, context = {}) {
  const base = `/vault/${encodeURI(String(relativePath || "").replace(/^\/+/, ""))}`;
  const params = new URLSearchParams();
  if (context.chatId) {
    params.set("chat", context.chatId);
  }
  if (context.turnId) {
    params.set("turn", context.turnId);
  }
  if (context.sourceIndex) {
    params.set("source", String(context.sourceIndex));
  }
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function buildChatUrl(chatId, turnId = "") {
  const params = new URLSearchParams();
  if (chatId) {
    params.set("chat", chatId);
  }
  if (turnId) {
    params.set("turn", turnId);
  }
  const query = params.toString();
  return query ? `/?${query}` : "/";
}

function readRouteFromLocation() {
  const params = new URLSearchParams(window.location.search);
  return {
    routeChatId: params.get("chat") || "",
    routeTurnId: params.get("turn") || "",
  };
}

function updateChatRoute(chatId, turnId = "") {
  const nextUrl = buildChatUrl(chatId, turnId);
  window.history.replaceState({}, "", nextUrl);
}

function clearRoute() {
  window.history.replaceState({}, "", "/");
}

function scrollToTurn(turnId) {
  if (!turnId) {
    return;
  }
  const target = document.getElementById(`turn-${turnId}`);
  if (!target) {
    return;
  }
  target.scrollIntoView({ behavior: "smooth", block: "start" });
}

function formatMessageTime(value) {
  if (!value) {
    return "now";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatDuration(ms) {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatKnowledgeDate(value) {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function formatNumber(value) {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function sumCounts(entries) {
  return (entries || []).reduce((total, entry) => total + Number(entry.count || 0), 0);
}

function labelize(value) {
  const text = String(value || "unknown");
  if (text === "all") return "All";
  return text.replace(/[_-]/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function buildVaultPath(relativePath) {
  return `/vault/${encodeURI(String(relativePath || "").replace(/^\/+/, ""))}`;
}

function debounce(fn, wait) {
  let timer = 0;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

function truncate(text, limit) {
  const value = String(text || "");
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function summarizeText(text, limit) {
  return truncate(
    String(text || "")
      .replace(/\[[^\]]+\]\([^)]+\)/g, "")
      .replace(/\s+/g, " ")
      .trim(),
    limit,
  );
}

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
