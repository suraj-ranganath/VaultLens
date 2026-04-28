#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import crypto from "node:crypto";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { Codex } from "@openai/codex-sdk";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const vaultRoot = path.resolve(__dirname, "..");
const webRoot = path.join(vaultRoot, "web");
const chatTraceRoot = path.join(vaultRoot, "outputs", "chat-traces");
const agentEventsPath = path.join(vaultRoot, ".vault", "events", "agent-events.jsonl");
const trajectoryRoot = path.join(vaultRoot, ".vault", "trajectories");
const sessionMemoryRoot = path.join(vaultRoot, "memory");
const webDashboardViewsPath = path.join(vaultRoot, ".vault", "web-dashboard-views.json");
const customDashboardRoot = path.join(vaultRoot, "dashboards", "custom");

loadEnvFile(path.join(vaultRoot, ".env.local"));

const PORT = Number.parseInt(process.env.VAULT_QUERY_PORT || "4318", 10);
const DEFAULT_MODEL = (process.env.VAULT_QUERY_DEFAULT_MODEL || "gpt-5.4").trim();
const codex = new Codex({
  apiKey: (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim(),
});

const MODEL_PRICING = {
  "gpt-5.4": {
    inputPer1M: 2.5,
    cachedInputPer1M: 0.25,
    outputPer1M: 15,
    source: "https://openai.com/api/pricing/",
  },
  "gpt-5.4-mini": {
    inputPer1M: 0.75,
    cachedInputPer1M: 0.075,
    outputPer1M: 4.5,
    source: "https://openai.com/api/pricing/",
  },
  "gpt-5.4-nano": {
    inputPer1M: 0.2,
    cachedInputPer1M: 0.02,
    outputPer1M: 1.25,
    source: "https://openai.com/api/pricing/",
  },
};

const QUERY_SCHEMA = {
  type: "object",
  properties: {
    answer_markdown: { type: "string" },
    concise_answer: { type: "string" },
    confidence: { type: "string", enum: ["high", "medium", "low"] },
    citations: {
      type: "array",
      items: {
        type: "object",
        properties: {
          path: { type: "string" },
          title: { type: "string" },
          note_type: { type: "string" },
          relevance: { type: "string" },
          source_url: { type: ["string", "null"] },
        },
        required: ["path", "title", "note_type", "relevance", "source_url"],
        additionalProperties: false,
      },
    },
    gaps: {
      type: "array",
      items: { type: "string" },
    },
    follow_up_questions: {
      type: "array",
      items: { type: "string" },
    },
  },
  required: [
    "answer_markdown",
    "concise_answer",
    "confidence",
    "citations",
    "gaps",
    "follow_up_questions",
  ],
  additionalProperties: false,
};

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url || "/", `http://${req.headers.host || `localhost:${PORT}`}`);

    if (req.method === "GET" && url.pathname === "/api/health") {
      return json(res, 200, {
        ok: true,
        port: PORT,
        model: DEFAULT_MODEL,
        hasApiKey: Boolean((process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim()),
      });
    }

    if (req.method === "POST" && url.pathname === "/api/query") {
      return handleQuery(req, res);
    }

    if (req.method === "POST" && url.pathname === "/api/query-stream") {
      return handleQueryStream(req, res);
    }

    if (req.method === "POST" && url.pathname === "/api/save-answer") {
      return handleSaveAnswer(req, res);
    }

    if (req.method === "GET" && url.pathname === "/api/chats") {
      return handleListChats(res);
    }

    if (req.method === "GET" && url.pathname === "/api/events") {
      return handleListEvents(url, res);
    }

    if (req.method === "GET" && url.pathname === "/api/kb") {
      return handleKnowledgeBase(url, res);
    }

    if (req.method === "GET" && url.pathname === "/api/kb/views") {
      return handleListDashboardViews(res);
    }

    if (req.method === "POST" && url.pathname === "/api/kb/views") {
      return handleSaveDashboardView(req, res);
    }

    if (req.method === "GET" && url.pathname.startsWith("/api/chats/")) {
      return handleGetChat(decodeURIComponent(url.pathname.replace(/^\/api\/chats\//, "")), res);
    }

    if (req.method === "GET" && url.pathname.startsWith("/vault-raw/")) {
      return serveVaultRawFile(decodeURIComponent(url.pathname.replace(/^\/vault-raw\//, "")), res);
    }

    if (req.method === "GET" && url.pathname.startsWith("/vault/")) {
      return serveVaultFile(decodeURIComponent(url.pathname.replace(/^\/vault\//, "")), url, res);
    }

    if (req.method === "GET") {
      return serveStatic(url.pathname, res);
    }

    return json(res, 404, { error: "Not found" });
  } catch (error) {
    return json(res, 500, { error: formatError(error) });
  }
});

server.listen(PORT, () => {
  process.stdout.write(`Vault web interface listening on http://localhost:${PORT}\n`);
});

async function handleQuery(req, res) {
  const body = await readJson(req);
  const question = String(body.question || "").trim();
  const threadId = String(body.threadId || "").trim() || null;
  const reasoningEffort = normalizeReasoning(body.reasoningEffort);
  const includeWebSearch = Boolean(body.includeWebSearch);
  const model = String(body.model || DEFAULT_MODEL).trim() || DEFAULT_MODEL;

  if (!question) {
    return json(res, 400, { error: "Missing `question`." });
  }

  const apiKey = (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  if (!apiKey) {
    return json(res, 500, { error: "Missing OPENAI_API_KEY or CODEX_API_KEY in .env.local." });
  }

  const startedAt = Date.now();
  const vaultContext = await buildVaultContextPack(question);
  const threadOptions = {
    model,
    workingDirectory: vaultRoot,
    skipGitRepoCheck: true,
    approvalPolicy: "never",
    sandboxMode: "read-only",
    networkAccessEnabled: includeWebSearch,
    webSearchEnabled: includeWebSearch,
    modelReasoningEffort: reasoningEffort,
  };

  const thread = threadId ? codex.resumeThread(threadId, threadOptions) : codex.startThread(threadOptions);
  const turn = await thread.run(buildPrompt(question, includeWebSearch, vaultContext), { outputSchema: QUERY_SCHEMA });
  const answer = await hydrateAnswer(parseJson(turn.finalResponse));
  const cost = calculateCost(model, turn.usage);
  const persisted = await persistChatTurn({
    threadId: thread.id || randomId(),
    question,
    answer,
    trace: summarizeTrace(turn.items),
    usage: turn.usage,
    cost,
    meta: {
      model,
      reasoningEffort,
      includeWebSearch,
      vaultContextBytes: Buffer.byteLength(vaultContext, "utf8"),
      durationMs: Date.now() - startedAt,
    },
    feed: [],
    startedAt: new Date(startedAt).toISOString(),
    completedAt: new Date().toISOString(),
  });
  await appendAgentEvent({
    event: "web.query.completed",
    thread_id: thread.id,
    question,
    model,
    reasoning_effort: reasoningEffort,
    include_web_search: includeWebSearch,
    duration_ms: Date.now() - startedAt,
    cost,
  });

  return json(res, 200, {
    ok: true,
    threadId: thread.id,
    answer,
    trace: summarizeTrace(turn.items),
    usage: turn.usage,
    cost,
    chat: buildChatSummary(persisted),
    meta: {
      model,
      reasoningEffort,
      includeWebSearch,
      durationMs: Date.now() - startedAt,
    },
  });
}

async function handleQueryStream(req, res) {
  const body = await readJson(req);
  const validated = validateQueryRequest(body);
  if ("error" in validated) {
    return json(res, validated.status, { error: validated.error });
  }

  const { question, threadId, reasoningEffort, includeWebSearch, model } = validated;
  const startedAt = Date.now();
  const vaultContext = await buildVaultContextPack(question);
  const threadOptions = buildThreadOptions({ model, reasoningEffort, includeWebSearch });
  const thread = threadId ? codex.resumeThread(threadId, threadOptions) : codex.startThread(threadOptions);
  const { events } = await thread.runStreamed(buildPrompt(question, includeWebSearch, vaultContext), { outputSchema: QUERY_SCHEMA });

  res.writeHead(200, {
    "Content-Type": "application/x-ndjson; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
  });

  const completedItems = [];
  let finalResponse = "";
  let usage = null;
  const feed = [];

  try {
    for await (const event of events) {
      if (event.type === "thread.started") {
        const payload = { type: "thread.started", threadId: event.thread_id };
        feed.push(payload);
        writeNdjson(res, payload);
        continue;
      }
      if (event.type === "turn.started") {
        const payload = { type: "turn.started" };
        feed.push(payload);
        writeNdjson(res, payload);
        continue;
      }
      if (event.type === "turn.completed") {
        usage = event.usage;
        const payload = { type: "turn.completed", usage };
        feed.push(payload);
        writeNdjson(res, payload);
        continue;
      }
      if (event.type === "turn.failed") {
        const payload = { type: "turn.failed", error: event.error.message };
        feed.push(payload);
        writeNdjson(res, payload);
        break;
      }
      if (event.type === "error") {
        if ((event.message || "").includes("[features].collab")) {
          continue;
        }
        const payload = { type: "error", message: event.message };
        feed.push(payload);
        writeNdjson(res, payload);
        continue;
      }
      if (event.type === "item.started" || event.type === "item.updated" || event.type === "item.completed") {
        if (event.type === "item.completed") {
          completedItems.push(event.item);
          if (event.item.type === "agent_message") {
            finalResponse = event.item.text;
          }
        }
        const normalizedItem = normalizeThreadItem(event.item);
        if (normalizedItem) {
          const payload = {
            type: "item",
            phase: event.type.split(".")[1],
            item: normalizedItem,
          };
          feed.push(payload);
          writeNdjson(res, payload);
        }
      }
    }

    if (!finalResponse) {
      throw new Error("Codex did not return a final structured response.");
    }

    const answer = await hydrateAnswer(parseJson(finalResponse));
    const cost = calculateCost(model, usage);
    const persisted = await persistChatTurn({
      threadId: thread.id || randomId(),
      question,
      answer,
      trace: summarizeTrace(completedItems),
      usage,
      cost,
      meta: {
        model,
        reasoningEffort,
        includeWebSearch,
        vaultContextBytes: Buffer.byteLength(vaultContext, "utf8"),
        durationMs: Date.now() - startedAt,
      },
      feed,
      startedAt: new Date(startedAt).toISOString(),
      completedAt: new Date().toISOString(),
    });
    await appendAgentEvent({
      event: "web.query.completed",
      thread_id: thread.id,
      question,
      model,
      reasoning_effort: reasoningEffort,
      include_web_search: includeWebSearch,
      duration_ms: Date.now() - startedAt,
      cost,
    });
    writeNdjson(res, {
      type: "result",
      threadId: thread.id,
      answer,
      trace: summarizeTrace(completedItems),
      usage,
      cost,
      chat: buildChatSummary(persisted),
      meta: {
        model,
        reasoningEffort,
        includeWebSearch,
        durationMs: Date.now() - startedAt,
      },
    });
  } catch (error) {
    writeNdjson(res, {
      type: "error",
      message: formatError(error),
    });
  } finally {
    res.end();
  }
}

async function handleSaveAnswer(req, res) {
  const body = await readJson(req);
  const question = String(body.question || "").trim();
  const answer = body.answer || {};
  const trace = Array.isArray(body.trace) ? body.trace : [];
  const meta = body.meta || {};
  const threadId = String(body.threadId || "").trim();

  if (!question) {
    return json(res, 400, { error: "Missing `question`." });
  }
  if (!String(answer.answer_markdown || answer.concise_answer || "").trim()) {
    return json(res, 400, { error: "Missing answer content to save." });
  }

  const written = await writeSavedAnswer({
    question,
    answer,
    trace,
    meta,
    threadId,
  });

  return json(res, 200, {
    ok: true,
    path: written.relativePath,
    title: written.title,
    url: `/vault/${encodeURI(written.relativePath)}`,
  });
}

async function handleListChats(res) {
  const chats = await listChatTraces();
  return json(res, 200, {
    ok: true,
    chats: chats.map(buildChatSummary),
  });
}

async function handleGetChat(chatId, res) {
  const chat = await readChatTrace(chatId);
  if (!chat) {
    return json(res, 404, { error: "Chat trace not found." });
  }
  return json(res, 200, {
    ok: true,
    chat,
  });
}

async function handleListEvents(url, res) {
  const limit = Math.min(500, Math.max(1, Number.parseInt(url.searchParams.get("limit") || "200", 10)));
  const events = await readRecentJsonl(agentEventsPath, limit);
  return json(res, 200, {
    ok: true,
    events,
  });
}

async function handleKnowledgeBase(url, res) {
  const digest = await readAgentDigest();
  const allPages = Array.isArray(digest.pages) ? digest.pages : [];
  const filters = parseKnowledgeFilters(url.searchParams);
  const filtered = filterKnowledgePages(allPages, filters);
  const sorted = sortKnowledgePages(filtered, filters.sort);
  const limit = Math.min(400, Math.max(1, Number.parseInt(url.searchParams.get("limit") || "120", 10)));
  const selected = sorted.slice(0, limit);
  const [views, tasks] = await Promise.all([readDashboardViews(), readTaskLedgerSummary()]);

  return json(res, 200, {
    ok: true,
    generatedAt: digest.generated_at || "",
    pageCount: digest.page_count || allPages.length,
    claimHealth: digest.claim_health || {},
    browserQueueCount: digest.browser_queue_count || 0,
    sourceCount: digest.source_count || 0,
    filters,
    counts: buildKnowledgeCounts(allPages),
    focus: buildKnowledgeFocus(allPages, tasks),
    pages: await Promise.all(selected.map((page) => hydrateKnowledgePage(page))),
    totalMatches: filtered.length,
    views,
    tasks,
  });
}

async function handleListDashboardViews(res) {
  return json(res, 200, {
    ok: true,
    views: await readDashboardViews(),
  });
}

async function handleSaveDashboardView(req, res) {
  const body = await readJson(req);
  const name = String(body.name || "").trim();
  const filters = normalizeDashboardFilters(body.filters || {});
  if (!name) {
    return json(res, 400, { error: "Missing dashboard name." });
  }

  const digest = await readAgentDigest();
  const pages = sortKnowledgePages(filterKnowledgePages(digest.pages || [], filters), filters.sort).slice(0, 80);
  const view = {
    id: slugify(name).slice(0, 80),
    name,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    filters,
    matchCount: pages.length,
  };
  const views = await readDashboardViews();
  const existingIndex = views.findIndex((entry) => entry.id === view.id);
  if (existingIndex >= 0) {
    view.createdAt = views[existingIndex].createdAt || view.createdAt;
    views[existingIndex] = view;
  } else {
    views.unshift(view);
  }
  await writeDashboardViews(views);
  const markdownPath = await writeCustomDashboardMarkdown(view, pages);
  return json(res, 200, {
    ok: true,
    view,
    path: markdownPath,
    url: `/vault/${encodeURI(markdownPath)}`,
  });
}

function buildPrompt(question, includeWebSearch, vaultContext = "") {
  return `
You are answering questions against a local-first personal vault.

Primary objective:
- answer the user's question from the vault as accurately and efficiently as possible
- you have a guaranteed vault context pack below, generated from the compiled cache plus targeted retrieval
- use the guaranteed context before spending tool calls on broad file scans
- if shell/file tools fail, do not claim the vault is inaccessible when the guaranteed context has relevant evidence

Search discipline:
1. Read \`AGENTS.md\` first if you need the vault contract.
2. Check \`hot.md\` first when it exists.
3. Read the most relevant dashboard or topic/project page next.
4. Use \`rg\` and \`rg --files\` to find candidate notes before opening files.
5. Prefer canonical notes under \`items/\`, \`topics/\`, \`projects/\`, \`outputs/\`, and \`dashboards/\`.
6. If the user asks how something was ingested, stored, classified, or processed, inspect \`imports/telegram-inbox/\`, especially processed update logs and agent decision traces.
7. Only look at \`raw/\` if the canonical notes are insufficient.
8. Do not modify, create, or delete any files.
9. Keep the tool path efficient. Avoid opening lots of irrelevant files.

Answering rules:
- treat the local vault as the source of truth
- for ingestion-history questions, use stored ingest traces and decision logs rather than guessing
- web search is ${includeWebSearch ? "allowed only when the vault is insufficient and external context materially helps" : "disabled for this turn"}
- cite the vault files you actually relied on
- cite paths relative to the vault root
- in \`answer_markdown\`, place inline markdown citations directly next to the claims they support
- user-facing citation links in \`answer_markdown\` should prefer the external primary source URL for each cited note when available
- only fall back to markdown links like \`[note](/vault/relative/path.md)\` when no external primary source exists for that citation
- never use Obsidian wiki-link syntax like \`[[...]]\` inside \`answer_markdown\`; use standard markdown links only
- if the vault does not support part of the answer, say that explicitly in \`gaps\`
- keep the answer high-signal and useful, not verbose for its own sake

Return JSON only.

Guaranteed vault context pack:
${vaultContext || "(No preloaded vault context was found.)"}

User question:
${question}
`.trim();
}

async function buildVaultContextPack(question) {
  const script = path.join(vaultRoot, "tools", "telegram_vault_query.mjs");
  if (!(await fileExists(script))) {
    return "";
  }
  const env = {
    ...process.env,
    VAULT_QUERY_CONTEXT_ONLY: "1",
  };
  try {
    const stdout = await runWithInput({
      command: "node",
      args: [script],
      cwd: vaultRoot,
      env,
      input: JSON.stringify({
          workingDirectory: vaultRoot,
          question,
          includeWebSearch: false,
          model: DEFAULT_MODEL,
          reasoningEffort: "low",
        }),
      timeoutMs: 60_000,
    });
    const parsed = JSON.parse(stdout);
    const memory = await readRecentMemoryContext();
    return [String(parsed.vaultContext || ""), memory ? `### recent session memory\n${memory}` : ""].filter(Boolean).join("\n\n").slice(0, 60_000);
  } catch {
    return await readRecentMemoryContext();
  }
}

async function readRecentMemoryContext() {
  try {
    const files = (await fsp.readdir(sessionMemoryRoot))
      .filter((name) => /^\d{4}-\d{2}-\d{2}\.md$/.test(name) || name === "DREAMS.md")
      .sort()
      .reverse()
      .slice(0, 5);
    const sections = [];
    for (const file of files) {
      const text = await readTextIfExists(path.join(sessionMemoryRoot, file), 40_000);
      if (text) {
        sections.push(`### memory/${file}\n${truncate(text.slice(-6000), 6000)}`);
      }
    }
    return sections.join("\n\n");
  } catch {
    return "";
  }
}

async function readAgentDigest() {
  const digestPath = path.join(vaultRoot, ".vault", "cache", "agent-digest.json");
  if (!(await fileExists(digestPath))) {
    await runCacheCompile();
  }
  try {
    return JSON.parse(await fsp.readFile(digestPath, "utf8"));
  } catch {
    await runCacheCompile();
    return JSON.parse(await fsp.readFile(digestPath, "utf8"));
  }
}

async function runCacheCompile() {
  const script = path.join(vaultRoot, "tools", "vault_compile_cache.py");
  if (!(await fileExists(script))) {
    return;
  }
  await runWithInput({
    command: "python3",
    args: [script, "--vault-root", vaultRoot, "--quiet"],
    cwd: vaultRoot,
    env: process.env,
    input: "",
    timeoutMs: 90_000,
  });
}

function parseKnowledgeFilters(searchParams) {
  return normalizeDashboardFilters({
    search: searchParams.get("search") || "",
    type: searchParams.get("type") || "all",
    types: searchParams.get("types") || "",
    status: searchParams.get("status") || "all",
    priority: searchParams.get("priority") || "all",
    tag: searchParams.get("tag") || "",
    topic: searchParams.get("topic") || "",
    source: searchParams.get("source") || "",
    sort: searchParams.get("sort") || "recent",
  });
}

function normalizeDashboardFilters(raw) {
  const explicitTypes = Array.isArray(raw.types)
    ? raw.types
    : String(raw.types || "").split(",");
  return {
    search: String(raw.search || "").trim(),
    type: String(raw.type || "all").trim() || "all",
    types: explicitTypes.map((item) => String(item || "").trim()).filter(Boolean),
    status: String(raw.status || "all").trim() || "all",
    priority: String(raw.priority || "all").trim() || "all",
    tag: String(raw.tag || "").trim(),
    topic: String(raw.topic || "").trim(),
    source: String(raw.source || "").trim(),
    sort: String(raw.sort || "recent").trim() || "recent",
  };
}

function filterKnowledgePages(pages, filters) {
  const query = normalizeSearchText(filters.search);
  const sourceQuery = normalizeSearchText(filters.source);
  const typeSet = new Set(filters.types?.length ? filters.types : filters.type && filters.type !== "all" ? [filters.type] : []);
  return pages.filter((page) => {
    const pagePath = String(page.path || "");
    if (!pagePath.startsWith("items/") && !pagePath.startsWith("topics/") && !pagePath.startsWith("projects/") && !pagePath.startsWith("outputs/") && !pagePath.startsWith("dashboards/")) {
      return false;
    }
    if (typeSet.size && !typeSet.has(String(page.type || ""))) return false;
    if (filters.status !== "all" && String(page.status || "") !== filters.status) return false;
    if (filters.priority !== "all" && String(page.priority || "") !== filters.priority) return false;
    if (filters.tag && !(page.tags || []).map(String).includes(filters.tag)) return false;
    if (filters.topic && !(page.topics || []).map(String).includes(filters.topic)) return false;
    if (sourceQuery && !normalizeSearchText(`${page.url || ""} ${deriveSourceSite(page.url || "", {})}`).includes(sourceQuery)) return false;
    if (!query) return true;
    const haystack = normalizeSearchText(
      [
        page.title,
        page.path,
        page.type,
        page.url,
        page.summary,
        page.why_saved,
        ...(page.tags || []),
        ...(page.topics || []),
      ].join(" "),
    );
    return haystack.includes(query);
  });
}

function sortKnowledgePages(pages, sort) {
  const cloned = [...pages];
  if (sort === "priority") {
    cloned.sort((a, b) => priorityRank(b.priority) - priorityRank(a.priority) || compareRecent(b, a));
  } else if (sort === "deadline") {
    cloned.sort((a, b) => String(a.deadline || "9999-99-99").localeCompare(String(b.deadline || "9999-99-99")) || compareRecent(b, a));
  } else if (sort === "alpha") {
    cloned.sort((a, b) => String(a.title || "").localeCompare(String(b.title || "")));
  } else {
    cloned.sort(compareRecent);
  }
  return cloned;
}

function compareRecent(a, b) {
  const left = String(a.discovered_on || a.published_on || a.deadline || "");
  const right = String(b.discovered_on || b.published_on || b.deadline || "");
  if (left !== right) return right.localeCompare(left);
  return Number(b.mtime || 0) - Number(a.mtime || 0);
}

function priorityRank(priority) {
  return { critical: 4, high: 3, medium: 2, low: 1 }[String(priority || "").toLowerCase()] || 0;
}

function buildKnowledgeCounts(pages) {
  const byType = countBy(pages, "type");
  const byStatus = countBy(pages, "status");
  const byPriority = countBy(pages, "priority");
  const tags = countArrayValues(pages, "tags").slice(0, 48);
  const topics = countArrayValues(pages, "topics").slice(0, 48);
  const sources = countSources(pages).slice(0, 48);
  return { byType, byStatus, byPriority, tags, topics, sources };
}

function countBy(pages, field) {
  const counts = {};
  for (const page of pages) {
    const key = String(page[field] || "unknown").trim() || "unknown";
    counts[key] = (counts[key] || 0) + 1;
  }
  return Object.entries(counts)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

function countArrayValues(pages, field) {
  const counts = {};
  for (const page of pages) {
    for (const item of page[field] || []) {
      const key = String(item || "").trim();
      if (key) counts[key] = (counts[key] || 0) + 1;
    }
  }
  return Object.entries(counts)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

function countSources(pages) {
  const counts = {};
  for (const page of pages) {
    const source = deriveSourceSite(page.url || "", {});
    if (source) counts[source] = (counts[source] || 0) + 1;
  }
  return Object.entries(counts)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

function buildKnowledgeFocus(pages, tasks) {
  const now = new Date();
  const inSevenDays = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  const openDeadlinePages = pages
    .filter((page) => page.deadline && String(page.status || "open") !== "done" && page.deadline <= inSevenDays)
    .sort((a, b) => String(a.deadline).localeCompare(String(b.deadline)))
    .slice(0, 6);
  const highPriority = pages
    .filter((page) => ["critical", "high"].includes(String(page.priority || "")) && String(page.status || "open") !== "done")
    .sort(compareRecent)
    .slice(0, 8);
  const recentKnowledge = pages
    .filter((page) => ["article", "resource", "tweet", "thought"].includes(String(page.type || "")))
    .sort(compareRecent)
    .slice(0, 8);
  return {
    openDeadlinePages,
    highPriority,
    recentKnowledge,
    openTaskCount: tasks.openCount || 0,
  };
}

async function hydrateKnowledgePage(page) {
  const meta = await readPageFrontmatter(page.path);
  const sourceUrl = String(page.url || meta.url || "").trim();
  return {
    path: page.path,
    title: page.title || meta.title || path.basename(page.path || ""),
    type: page.type || meta.type || "note",
    url: sourceUrl || null,
    status: page.status || meta.status || "",
    priority: page.priority || meta.priority || "",
    published_on: page.published_on || meta.published_on || "",
    discovered_on: page.discovered_on || meta.discovered_on || "",
    deadline: page.deadline || meta.deadline || "",
    tags: page.tags || asArray(meta.tags),
    topics: page.topics || asArray(meta.topics),
    summary: page.summary || "",
    why_saved: page.why_saved || meta.why_saved || "",
    sourceSite: deriveSourceSite(sourceUrl, meta),
    author: firstPresent(meta.author, meta.author_name, meta.source_author, meta.byline),
    authorCredential: firstPresent(meta.author_credential, meta.author_credentials, meta.credential, meta.credentials),
    vaultUrl: buildVaultUrl(page.path),
    rawUrl: buildVaultRawUrl(page.path),
  };
}

async function readPageFrontmatter(relativePath) {
  const safePath = safeJoin(vaultRoot, relativePath || "");
  if (!safePath || !(await fileExists(safePath)) || path.extname(safePath) !== ".md") {
    return {};
  }
  try {
    const handle = await fsp.open(safePath, "r");
    try {
      const buffer = Buffer.alloc(16_384);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      const text = buffer.subarray(0, bytesRead).toString("utf8");
      return parseSimpleFrontmatter(splitFrontmatter(text).frontmatter);
    } finally {
      await handle.close();
    }
  } catch {
    return {};
  }
}

function asArray(value) {
  return Array.isArray(value) ? value : String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function normalizeSearchText(value) {
  return String(value || "").toLowerCase().replace(/\s+/g, " ").trim();
}

async function readDashboardViews() {
  const defaults = defaultDashboardViews();
  try {
    if (!(await fileExists(webDashboardViewsPath))) {
      return defaults;
    }
    const parsed = JSON.parse(await fsp.readFile(webDashboardViewsPath, "utf8"));
    const views = Array.isArray(parsed.views) ? parsed.views : [];
    const customViews = views.filter((view) => !view.builtin && !defaults.some((preset) => preset.id === view.id));
    return [...defaults, ...customViews];
  } catch {
    return defaults;
  }
}

async function writeDashboardViews(views) {
  const persistedViews = views.filter((view) => !view.builtin);
  await fsp.mkdir(path.dirname(webDashboardViewsPath), { recursive: true });
  await fsp.writeFile(
    webDashboardViewsPath,
    JSON.stringify({ schema: "my-vault-web-dashboard-views-v1", updatedAt: new Date().toISOString(), views: persistedViews }, null, 2) + "\n",
    "utf8",
  );
}

function defaultDashboardViews() {
  return [
    { id: "urgent", name: "Urgent / This Week", builtin: true, filters: { type: "all", status: "open", priority: "all", sort: "deadline" } },
    { id: "reading", name: "Reading Queue", builtin: true, filters: { types: ["article", "resource", "tweet"], status: "all", priority: "all", sort: "recent" } },
    { id: "jobs", name: "Jobs + Opportunities", builtin: true, filters: { types: ["job", "opportunity"], status: "all", priority: "all", sort: "recent" } },
    { id: "decisions", name: "Decisions + Systems", builtin: true, filters: { types: ["decision", "system"], status: "all", priority: "all", sort: "recent" } },
  ];
}

async function writeCustomDashboardMarkdown(view, pages) {
  await fsp.mkdir(customDashboardRoot, { recursive: true });
  const relativePath = path.posix.join("dashboards", "custom", `${view.id}.md`);
  const targetPath = path.join(vaultRoot, relativePath);
  const lines = [
    "---",
    yamlDump({
      title: view.name,
      type: "dashboard",
      status: "active",
      created_on: view.createdAt,
      updated_on: view.updatedAt,
      tags: ["custom-dashboard", "web-dashboard"],
      filters: JSON.stringify(view.filters),
    }),
    "---",
    "",
    `# ${view.name}`,
    "",
    `Generated from the web knowledge dashboard on ${view.updatedAt}.`,
    "",
    "## Filters",
    "",
    ...Object.entries(view.filters).map(([key, value]) => `- ${key}: ${value || "all"}`),
    "",
    "## Items",
    "",
    ...(pages.length
      ? pages.map((page) => {
          const bits = [page.type, page.priority, page.status].filter(Boolean).join(", ");
          const source = page.url ? ` - [source](${page.url})` : "";
          return `- [[${page.path}|${page.title || page.path}]]${bits ? ` (${bits})` : ""}${source}`;
        })
      : ["- No matching items at generation time."]),
    "",
  ];
  await fsp.writeFile(targetPath, lines.join("\n"), "utf8");
  return relativePath;
}

async function readTaskLedgerSummary() {
  const tasksPath = path.join(vaultRoot, ".vault", "tasks", "tasks.json");
  try {
    if (!(await fileExists(tasksPath))) {
      return { openCount: 0, totalCount: 0, urgent: [] };
    }
    const parsed = JSON.parse(await fsp.readFile(tasksPath, "utf8"));
    const tasks = Array.isArray(parsed.tasks) ? parsed.tasks : [];
    return {
      openCount: tasks.filter((task) => task.status === "open").length,
      totalCount: tasks.length,
      urgent: tasks
        .filter((task) => task.status === "open")
        .sort((a, b) => String(a.due_on || "9999-99-99").localeCompare(String(b.due_on || "9999-99-99")))
        .slice(0, 8),
    };
  } catch {
    return { openCount: 0, totalCount: 0, urgent: [] };
  }
}

async function readTextIfExists(filePath, maxBytes = 80_000) {
  try {
    const stat = await fsp.stat(filePath);
    if (!stat.isFile()) return "";
    const handle = await fsp.open(filePath, "r");
    try {
      const length = Math.min(stat.size, maxBytes);
      const buffer = Buffer.alloc(length);
      await handle.read(buffer, 0, length, 0);
      return buffer.toString("utf8");
    } finally {
      await handle.close();
    }
  } catch {
    return "";
  }
}

function runWithInput({ command, args, cwd, env, input, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`${command} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (stdout.length > 3 * 1024 * 1024) {
        child.kill("SIGTERM");
        reject(new Error(`${command} stdout exceeded max buffer`));
      }
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) {
        resolve(stdout);
      } else {
        reject(new Error(stderr.trim() || `${command} exited with code ${code}`));
      }
    });
    child.stdin.end(input);
  });
}

function summarizeTrace(items) {
  return items.map(normalizeThreadItem).filter(Boolean).slice(0, 120);
}

function normalizeThreadItem(item) {
  if (item.type === "command_execution") {
    return {
      id: item.id,
      type: "command_execution",
      command: item.command,
      status: item.status,
      exit_code: item.exit_code ?? null,
      output_preview: truncate(item.aggregated_output || "", 700),
    };
  }
  if (item.type === "web_search") {
    return {
      id: item.id,
      type: "web_search",
      query: item.query,
    };
  }
  if (item.type === "mcp_tool_call") {
    return {
      id: item.id,
      type: "mcp_tool_call",
      server: item.server,
      tool: item.tool,
      status: item.status,
      error: item.error?.message || null,
      arguments_preview: truncate(JSON.stringify(item.arguments || {}), 320),
    };
  }
  if (item.type === "todo_list") {
    return {
      id: item.id,
      type: "todo_list",
      items: item.items,
    };
  }
  if (item.type === "reasoning") {
    return {
      id: item.id,
      type: "reasoning",
      text: item.text,
    };
  }
  if (item.type === "agent_message") {
    return {
      id: item.id,
      type: "agent_message",
      text: item.text || "",
    };
  }
  if (item.type === "error") {
    if ((item.message || "").includes("[features].collab")) {
      return null;
    }
    return {
      id: item.id,
      type: "error",
      message: item.message,
    };
  }
  return null;
}

async function writeSavedAnswer({ question, answer, trace, meta, threadId }) {
  const title = buildOutputTitle(question);
  const slug = slugify(title);
  const day = new Date().toISOString().slice(0, 10);
  const outputsDir = path.join(vaultRoot, "outputs");
  await fsp.mkdir(outputsDir, { recursive: true });

  let filename = `${day} ${slug}.md`;
  let targetPath = path.join(outputsDir, filename);
  let counter = 2;
  while (await fileExists(targetPath)) {
    filename = `${day} ${slug} ${counter}.md`;
    targetPath = path.join(outputsDir, filename);
    counter += 1;
  }

  const citations = Array.isArray(answer.citations) ? answer.citations : [];
  const frontmatter = {
    title,
    status: "active",
    priority: "medium",
    created_on: new Date().toISOString(),
    question,
    thread_id: threadId || null,
    model: String(meta.model || DEFAULT_MODEL),
    reasoning_effort: String(meta.reasoningEffort || "medium"),
    web_search_enabled: Boolean(meta.includeWebSearch),
    confidence: String(answer.confidence || "unknown"),
    tags: ["vault-answer", "agentic-query"],
    topics: [],
    linked_items: citations.map((citation) => citation.path).filter(Boolean),
  };

  const lines = [
    "---",
    yamlDump(frontmatter),
    "---",
    "",
    `# ${title}`,
    "",
    "## Question",
    "",
    question,
    "",
    "## Answer",
    "",
    String(answer.answer_markdown || answer.concise_answer || "").trim(),
    "",
  ];

  if (answer.concise_answer && answer.concise_answer !== answer.answer_markdown) {
    lines.push("## Concise Answer", "", String(answer.concise_answer).trim(), "");
  }

  if (citations.length) {
    lines.push("## Citations", "");
    for (const citation of citations) {
      const parts = [
        `- [[${citation.path}|${citation.title || citation.path}]] (${citation.note_type || "note"}): ${citation.relevance || ""}`.trim(),
      ];
      if (citation.source_url) {
        parts.push(`([primary source](${citation.source_url}))`);
      }
      lines.push(parts.join(" "));
    }
    lines.push("");
  }

  if (Array.isArray(answer.gaps) && answer.gaps.length) {
    lines.push("## Gaps", "");
    for (const gap of answer.gaps) {
      lines.push(`- ${gap}`);
    }
    lines.push("");
  }

  if (Array.isArray(answer.follow_up_questions) && answer.follow_up_questions.length) {
    lines.push("## Follow-up Questions", "");
    for (const followUp of answer.follow_up_questions) {
      lines.push(`- ${followUp}`);
    }
    lines.push("");
  }

  const traceSnapshot = trace.slice(0, 12);
  if (traceSnapshot.length) {
    lines.push("## Trace Snapshot", "");
    for (const item of traceSnapshot) {
      if (item.type === "command_execution") {
        lines.push(`- command: \`${item.command}\` (exit ${item.exit_code ?? "?"})`);
      } else if (item.type === "web_search") {
        lines.push(`- web search: ${item.query}`);
      } else if (item.type === "mcp_tool_call") {
        lines.push(`- mcp tool: \`${item.server}.${item.tool}\` (${item.status})`);
      } else if (item.type === "todo_list") {
        lines.push(`- todo list: ${item.items.length} items`);
      } else if (item.type === "error") {
        lines.push(`- error: ${item.message}`);
      }
    }
    lines.push("");
  }

  lines.push("## Query Metadata", "");
  lines.push(`- Saved at: ${new Date().toISOString()}`);
  lines.push(`- Model: ${String(meta.model || DEFAULT_MODEL)}`);
  lines.push(`- Reasoning effort: ${String(meta.reasoningEffort || "medium")}`);
  lines.push(`- Web search enabled: ${Boolean(meta.includeWebSearch)}`);
  if (threadId) {
    lines.push(`- Thread ID: \`${threadId}\``);
  }
  lines.push("");

  await fsp.writeFile(targetPath, lines.join("\n"), "utf8");
  return {
    title,
    relativePath: path.relative(vaultRoot, targetPath).replaceAll(path.sep, "/"),
  };
}

async function persistChatTurn({ threadId, question, answer, trace, usage, cost, meta, feed, startedAt, completedAt }) {
  await fsp.mkdir(chatTraceRoot, { recursive: true });
  const safeId = sanitizeChatId(threadId || randomId());
  const targetPath = path.join(chatTraceRoot, `${safeId}.json`);

  let chat = {
    id: safeId,
    threadId: safeId,
    title: buildOutputTitle(question),
    createdAt: startedAt,
    updatedAt: completedAt,
    model: meta.model,
    turns: [],
  };

  if (await fileExists(targetPath)) {
    try {
      chat = JSON.parse(await fsp.readFile(targetPath, "utf8"));
    } catch {
      // Keep a fresh structure if the prior trace is unreadable.
    }
  }

  chat.id = safeId;
  chat.threadId = safeId;
  chat.title = chat.title || buildOutputTitle(question);
  chat.createdAt = chat.createdAt || startedAt;
  chat.updatedAt = completedAt;
  chat.model = meta.model;
  chat.turns = Array.isArray(chat.turns) ? chat.turns : [];
  chat.turns.push({
    id: randomId(),
    question,
    answer,
    trace,
    usage,
    cost,
    meta,
    feed,
    startedAt,
    completedAt,
  });

  await fsp.writeFile(targetPath, JSON.stringify(chat, null, 2), "utf8");
  await appendTrajectoryEvent(safeId, {
    surface: "web",
    event: "web.query.turn",
    threadId: safeId,
    question,
    answer,
    trace,
    usage,
    cost,
    meta,
    feed,
    startedAt,
    completedAt,
  });
  await appendSessionMemory({
    surface: "web",
    summary: `Vault Lens question: ${question}`,
    rawText: question,
    metadata: {
      threadId: safeId,
      confidence: answer?.confidence || "",
      citationCount: Array.isArray(answer?.citations) ? answer.citations.length : 0,
      cost,
    },
  });
  return chat;
}

async function appendAgentEvent(event) {
  try {
    await fsp.mkdir(path.dirname(agentEventsPath), { recursive: true });
    const runId = String(event.run_id || event.thread_id || event.threadId || "web");
    const payload = {
      logged_at: new Date().toISOString(),
      event_schema: "my-vault-agent-event-v2",
      surface: "web",
      run_id: runId,
      seq: await nextEventSeq(runId),
      stream: event.stream || inferEventStream(event),
      ...event,
    };
    await fsp.appendFile(agentEventsPath, `${JSON.stringify(payload)}\n`, "utf8");
  } catch {
    // Event logging must never break user-facing answers.
  }
}

async function appendSessionMemory({ surface, summary, rawText, metadata }) {
  try {
    const now = new Date();
    const day = now.toISOString().slice(0, 10);
    await fsp.mkdir(sessionMemoryRoot, { recursive: true });
    const target = path.join(sessionMemoryRoot, `${day}.md`);
    if (!(await fileExists(target))) {
      await fsp.writeFile(
        target,
        ["---", "type: session_memory", `date: ${day}`, "status: active", "tags: [session-memory]", "---", "", `# Session Memory - ${day}`, ""].join("\n"),
        "utf8",
      );
    }
    const lines = [
      `## ${now.toISOString()} - ${surface}`,
      "",
      `- Summary: ${truncate(summary || "", 500)}`,
      rawText ? `- Raw: ${truncate(rawText, 700)}` : "",
      metadata ? `- Metadata: \`${truncate(JSON.stringify(metadata), 800)}\`` : "",
      "",
    ].filter(Boolean);
    await fsp.appendFile(target, `${lines.join("\n")}\n`, "utf8");
  } catch {
    // Session memory should never break answering.
  }
}

async function nextEventSeq(runId) {
  try {
    const events = await readRecentJsonl(agentEventsPath, 500);
    for (let index = events.length - 1; index >= 0; index -= 1) {
      if (String(events[index]?.run_id || "") === runId) {
        return Number(events[index].seq || 0) + 1;
      }
    }
  } catch {
    // Fall through to first sequence number.
  }
  return 1;
}

function inferEventStream(event) {
  const name = String(event.event || "");
  if (name.includes("failed") || name.includes("error")) return "error";
  if (name.includes("tool") || name.includes("action")) return "tool";
  if (name.includes("query") || name.includes("answer")) return "assistant";
  return "lifecycle";
}

async function readRecentJsonl(filePath, limit) {
  try {
    if (!(await fileExists(filePath))) {
      return [];
    }
    const text = await fsp.readFile(filePath, "utf8");
    const lines = text.split(/\r?\n/).filter(Boolean).slice(-limit);
    return lines
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}

async function appendTrajectoryEvent(runId, event) {
  try {
    await fsp.mkdir(trajectoryRoot, { recursive: true });
    const safeId = sanitizeChatId(runId || randomId());
    const payload = redactForTrajectory({
      trace_schema: "my-vault-trajectory-v1",
      logged_at: new Date().toISOString(),
      run_id: safeId,
      ...event,
    });
    await fsp.appendFile(path.join(trajectoryRoot, `${safeId}.jsonl`), `${JSON.stringify(payload)}\n`, "utf8");
  } catch {
    // Trajectory capture must not break chat.
  }
}

function redactForTrajectory(value) {
  if (Array.isArray(value)) {
    return value.map(redactForTrajectory);
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      if (/(api[_-]?key|token|secret|credential|authorization|password)/i.test(key)) {
        out[key] = "[redacted]";
      } else {
        out[key] = redactForTrajectory(item);
      }
    }
    return out;
  }
  if (typeof value === "string") {
    const home = process.env.HOME || "";
    return home && home !== "/" ? value.split(home).join("$HOME") : value;
  }
  return value;
}

async function listChatTraces() {
  try {
    await fsp.mkdir(chatTraceRoot, { recursive: true });
    const files = (await fsp.readdir(chatTraceRoot)).filter((name) => name.endsWith(".json"));
    const chats = [];
    for (const file of files) {
      const fullPath = path.join(chatTraceRoot, file);
      try {
        const chat = JSON.parse(await fsp.readFile(fullPath, "utf8"));
        chats.push(chat);
      } catch {
        // Skip unreadable traces.
      }
    }
    chats.sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
    return chats;
  } catch {
    return [];
  }
}

async function readChatTrace(chatId) {
  const safeId = sanitizeChatId(chatId);
  const targetPath = path.join(chatTraceRoot, `${safeId}.json`);
  if (!(await fileExists(targetPath))) {
    return null;
  }
  return JSON.parse(await fsp.readFile(targetPath, "utf8"));
}

function buildChatSummary(chat) {
  const turns = Array.isArray(chat.turns) ? chat.turns : [];
  const lastTurn = turns.at(-1) || null;
  return {
    id: chat.id,
    threadId: chat.threadId,
    title: chat.title,
    updatedAt: chat.updatedAt,
    turnCount: turns.length,
    model: chat.model,
    lastQuestion: lastTurn?.question || "",
    lastAnswer: lastTurn?.answer?.concise_answer || lastTurn?.answer?.answer_markdown || "",
    lastCost: lastTurn?.cost || null,
  };
}

async function serveStatic(requestPath, res) {
  const normalized = requestPath === "/" ? "/index.html" : requestPath;
  const resolved = safeJoin(webRoot, normalized);
  if (!resolved) {
    return json(res, 404, { error: "Not found" });
  }
  const exists = await fileExists(resolved);
  if (!exists) {
    return json(res, 404, { error: "Not found" });
  }
  const ext = path.extname(resolved);
  const contentType =
    ext === ".html"
      ? "text/html; charset=utf-8"
      : ext === ".css"
        ? "text/css; charset=utf-8"
        : ext === ".js"
          ? "application/javascript; charset=utf-8"
          : "text/plain; charset=utf-8";
  res.writeHead(200, { "Content-Type": contentType });
  fs.createReadStream(resolved).pipe(res);
}

async function serveVaultFile(relativePath, requestUrl, res) {
  if (!isAllowedVaultPath(relativePath)) {
    return json(res, 403, { error: "Path is not exposed by the vault web interface." });
  }
  const resolved = safeJoin(vaultRoot, relativePath);
  if (!resolved || !(await fileExists(resolved))) {
    return json(res, 404, { error: "Vault file not found." });
  }

  const ext = path.extname(resolved).toLowerCase();
  if (ext !== ".md") {
    return serveVaultRawFile(relativePath, res);
  }

  const context = parseVaultRequestContext(requestUrl?.searchParams);
  const note = await buildVaultNotePageModel(relativePath, context);
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(renderVaultNotePage(note));
}

async function serveVaultRawFile(relativePath, res) {
  if (!isAllowedVaultPath(relativePath)) {
    return json(res, 403, { error: "Path is not exposed by the vault web interface." });
  }
  const resolved = safeJoin(vaultRoot, relativePath);
  if (!resolved || !(await fileExists(resolved))) {
    return json(res, 404, { error: "Vault file not found." });
  }
  const ext = path.extname(resolved).toLowerCase();
  const contentType =
    ext === ".md"
      ? "text/markdown; charset=utf-8"
      : ext === ".json"
        ? "application/json; charset=utf-8"
        : "text/plain; charset=utf-8";
  res.writeHead(200, { "Content-Type": contentType });
  fs.createReadStream(resolved).pipe(res);
}

async function readVaultNote(relativePath) {
  const safePath = safeJoin(vaultRoot, relativePath);
  if (!safePath || !(await fileExists(safePath))) {
    throw new Error("Vault note not found.");
  }

  const text = await fsp.readFile(safePath, "utf8");
  const { frontmatter, body } = splitFrontmatter(text);
  const meta = parseSimpleFrontmatter(frontmatter);
  const title = String(meta.title || path.basename(relativePath, path.extname(relativePath))).trim();
  const sourceUrl = String(meta.url || "").trim() || null;
  const noteType = String(meta.type || "").trim() || "note";
  const tags = Array.isArray(meta.tags) ? meta.tags : [];
  const topics = Array.isArray(meta.topics) ? meta.topics : [];

  return {
    path: relativePath,
    absolutePath: safePath,
    title,
    body,
    noteType,
    sourceUrl,
    tags,
    topics,
    frontmatter: meta,
  };
}

async function buildVaultNotePageModel(relativePath, context = {}) {
  const note = await readVaultNote(relativePath);
  const [backlinks, chatMentions, citationSet] = await Promise.all([
    findNoteBacklinks(relativePath),
    findChatMentions(relativePath),
    buildCitationSet(relativePath, context),
  ]);

  const publishedOn = String(note.frontmatter.published_on || "").trim();
  const discoveredOn = String(note.frontmatter.discovered_on || "").trim();
  const sourceExport = String(note.frontmatter.source_export || "").trim();
  const sourceSender = String(note.frontmatter.source_sender || "").trim();
  const authorName = firstPresent(
    note.frontmatter.author,
    note.frontmatter.author_name,
    note.frontmatter.source_author,
    note.frontmatter.byline,
  );
  const authorCredential = firstPresent(
    note.frontmatter.author_credential,
    note.frontmatter.author_credentials,
    note.frontmatter.credential,
    note.frontmatter.credentials,
  );

  return {
    ...note,
    context,
    publishedOn,
    discoveredOn,
    deadline: String(note.frontmatter.deadline || "").trim(),
    status: String(note.frontmatter.status || "").trim(),
    priority: String(note.frontmatter.priority || "").trim(),
    timeliness: String(note.frontmatter.timeliness || "").trim(),
    sourceExport,
    sourceSender,
    authorName,
    authorCredential,
    sourceSite: deriveSourceSite(note.sourceUrl, note.frontmatter),
    backlinks,
    chatMentions,
    citationSet,
    backToChatUrl: buildChatUrl(context.chatId, context.turnId),
  };
}

function renderVaultNotePage(note) {
  const subtitleBits = [note.noteType, note.path];
  if (note.publishedOn) {
    subtitleBits.push(`published ${note.publishedOn}`);
  }
  if (note.discoveredOn) {
    subtitleBits.push(`saved ${note.discoveredOn}`);
  }

  const chips = [...note.tags, ...note.topics.map((topic) => `topic:${topic}`)];
  const tagChips = chips.length
    ? `
        <div class="note-chip-row">
          ${chips.map((tag) => `<span class="note-chip">${escapeHtml(String(tag))}</span>`).join("")}
        </div>
      `
    : "";

  const relatedLinks = [
    `<a class="note-action secondary" href="${escapeHtml(note.backToChatUrl)}">Back to chat</a>`,
    `<a class="note-action secondary" href="${escapeHtml(buildObsidianUrl(note.absolutePath))}">Open in Obsidian</a>`,
    `<a class="note-action secondary" href="${escapeHtml(buildVaultRawUrl(note.path))}" target="_blank" rel="noreferrer">Raw markdown</a>`,
    note.sourceUrl
      ? `<a class="note-action primary" href="${escapeHtml(note.sourceUrl)}" target="_blank" rel="noreferrer">Primary source</a>`
      : "",
  ]
    .filter(Boolean)
    .join("");

  const metadataFacts = [
    renderFact("Type", note.noteType),
    renderFact("Path", note.path, "mono"),
    renderFact("Website", note.sourceSite),
    renderFact("Author", note.authorName),
    renderFact("Credential", note.authorCredential),
    renderFact("Published", note.publishedOn),
    renderFact("Added", note.discoveredOn),
    renderFact("Deadline", note.deadline),
    renderFact("Status", note.status),
    renderFact("Priority", note.priority),
    renderFact("Timeliness", note.timeliness),
    note.sourceSender ? renderFact("Captured from", note.sourceSender) : "",
    note.sourceExport
      ? renderFact("Source export", `<a href="${escapeHtml(buildVaultRawUrl(note.sourceExport))}" target="_blank" rel="noreferrer">${escapeHtml(note.sourceExport)}</a>`, "html")
      : "",
  ]
    .filter(Boolean)
    .join("");

  const citationRail = note.citationSet
    ? `
        <section class="rail-card">
          <p class="rail-kicker">Citation set</p>
          <h3>Within this answer</h3>
          <p class="rail-copy">
            Source ${note.citationSet.index} of ${note.citationSet.total} from
            <a href="${escapeHtml(note.citationSet.chatUrl)}">${escapeHtml(note.citationSet.chatTitle)}</a>.
          </p>
          <div class="citation-nav">
            ${
              note.citationSet.previous
                ? `<a class="note-action secondary citation-nav-link" href="${escapeHtml(note.citationSet.previous.url)}">Previous source</a>`
                : `<span class="note-action secondary is-disabled">Previous source</span>`
            }
            ${
              note.citationSet.next
                ? `<a class="note-action secondary citation-nav-link" href="${escapeHtml(note.citationSet.next.url)}">Next source</a>`
                : `<span class="note-action secondary is-disabled">Next source</span>`
            }
          </div>
          <div class="citation-current">
            <div class="citation-current-title">${escapeHtml(note.citationSet.current.title || note.title)}</div>
            ${
              note.citationSet.current.relevance
                ? `<p class="citation-current-copy">${escapeHtml(note.citationSet.current.relevance)}</p>`
                : ""
            }
          </div>
        </section>
      `
    : "";

  const backlinkSection = note.backlinks.length
    ? `
        <section class="related-section">
          <div class="section-heading">
            <p class="section-kicker">Backlinks</p>
            <h2>Mentioned elsewhere in the vault</h2>
          </div>
          <div class="related-grid">
            ${note.backlinks
              .map(
                (entry) => `
                  <article class="related-card">
                    <div class="related-topline">
                      <span class="related-kind">${escapeHtml(entry.noteType || "note")}</span>
                      <span class="related-path">${escapeHtml(entry.path)}</span>
                    </div>
                    <h3><a href="${escapeHtml(entry.url)}">${escapeHtml(entry.title)}</a></h3>
                    ${entry.excerpt ? `<p>${escapeHtml(entry.excerpt)}</p>` : ""}
                  </article>
                `,
              )
              .join("")}
          </div>
        </section>
      `
    : "";

  const chatMentionSection = note.chatMentions.length
    ? `
        <section class="related-section">
          <div class="section-heading">
            <p class="section-kicker">Chat traces</p>
            <h2>Mentioned in chats</h2>
          </div>
          <div class="related-grid">
            ${note.chatMentions
              .map(
                (entry) => `
                  <article class="related-card">
                    <div class="related-topline">
                      <span class="related-kind">${escapeHtml(entry.dateLabel)}</span>
                      <span class="related-path">${escapeHtml(entry.chatTitle)}</span>
                    </div>
                    <h3><a href="${escapeHtml(entry.url)}">${escapeHtml(entry.question)}</a></h3>
                    ${entry.relevance ? `<p>${escapeHtml(entry.relevance)}</p>` : ""}
                    ${entry.snippet ? `<p class="muted-copy">${escapeHtml(entry.snippet)}</p>` : ""}
                  </article>
                `,
              )
              .join("")}
          </div>
        </section>
      `
    : "";

  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>${escapeHtml(note.title)} • Vault Lens</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Sans:wght@400;500;600;700&display=swap"
      rel="stylesheet"
    />
    <style>
      :root {
        --bg: #1b1c1f;
        --bg-soft: #222428;
        --panel: rgba(255, 255, 255, 0.045);
        --panel-strong: rgba(255, 255, 255, 0.07);
        --line: rgba(255, 255, 255, 0.1);
        --text: #ece7de;
        --muted: #a79f94;
        --accent: #7de2c2;
        --accent-strong: #38c598;
        --shadow: 0 24px 80px rgba(0, 0, 0, 0.34);
      }
      * { box-sizing: border-box; }
      html, body {
        margin: 0;
        min-height: 100%;
        background:
          radial-gradient(circle at top left, rgba(125, 226, 194, 0.12), transparent 22%),
          radial-gradient(circle at top right, rgba(244, 198, 131, 0.08), transparent 18%),
          var(--bg);
        color: var(--text);
        font-family: "Instrument Sans", sans-serif;
      }
      body::before {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        opacity: 0.2;
        background-image:
          linear-gradient(rgba(255, 255, 255, 0.025) 1px, transparent 1px),
          linear-gradient(90deg, rgba(255, 255, 255, 0.025) 1px, transparent 1px);
        background-size: 26px 26px;
        mask-image: linear-gradient(180deg, black, transparent 80%);
      }
      .note-shell {
        position: relative;
        width: min(1320px, calc(100% - 32px));
        margin: 32px auto;
        border: 1px solid var(--line);
        border-radius: 30px;
        background: var(--panel);
        box-shadow: var(--shadow);
        overflow: hidden;
        backdrop-filter: blur(18px);
      }
      .note-header {
        padding: 30px 32px 24px;
        border-bottom: 1px solid var(--line);
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.02));
      }
      .note-kicker,
      .note-subtitle,
      .note-chip,
      .note-action {
        font-family: "IBM Plex Mono", monospace;
        text-transform: uppercase;
        letter-spacing: 0.06em;
      }
      .note-kicker {
        margin: 0 0 8px;
        color: var(--muted);
        font-size: 0.72rem;
      }
      .note-title {
        margin: 0;
        max-width: 15ch;
        font-size: clamp(2.2rem, 4.2vw, 3.6rem);
        line-height: 1.05;
      }
      .note-subtitle {
        margin: 12px 0 0;
        color: var(--muted);
        font-size: 0.72rem;
        line-height: 1.7;
      }
      .note-chip-row {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 16px;
      }
      .note-chip {
        display: inline-flex;
        align-items: center;
        min-height: 30px;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
        color: var(--muted);
        font-size: 0.68rem;
      }
      .note-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 18px;
      }
      .note-action {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 40px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid var(--line);
        text-decoration: none;
        font-size: 0.7rem;
      }
      .note-action.primary {
        background: rgba(56, 197, 152, 0.16);
        border-color: rgba(56, 197, 152, 0.32);
        color: #dbfff5;
      }
      .note-action.secondary {
        background: rgba(255, 255, 255, 0.03);
        color: var(--text);
      }
      .note-action.is-disabled {
        opacity: 0.45;
        pointer-events: none;
      }
      .note-layout {
        display: grid;
        grid-template-columns: minmax(0, 1.4fr) minmax(280px, 380px);
        gap: 24px;
        padding: 26px;
      }
      .note-main {
        min-width: 0;
      }
      .note-rail {
        position: relative;
      }
      .note-content,
      .rail-card,
      .related-card {
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.035);
        border-radius: 24px;
      }
      .note-content {
        padding: 30px 32px;
      }
      .note-body {
        line-height: 1.75;
        font-size: 1.02rem;
      }
      .note-body p,
      .note-body ul,
      .note-body ol,
      .note-body h1,
      .note-body h2,
      .note-body h3,
      .note-body pre {
        margin-top: 0;
      }
      .note-body h1,
      .note-body h2,
      .note-body h3 {
        margin: 26px 0 12px;
        line-height: 1.15;
      }
      .note-body a {
        color: var(--accent);
      }
      .note-body code {
        font-family: "IBM Plex Mono", monospace;
        background: rgba(255, 255, 255, 0.06);
        padding: 1px 6px;
        border-radius: 8px;
      }
      .note-body pre {
        padding: 16px;
        border-radius: 18px;
        background: rgba(0, 0, 0, 0.2);
        border: 1px solid var(--line);
        overflow: auto;
      }
      .note-body pre code {
        background: transparent;
        padding: 0;
      }
      .note-rail-stack {
        position: sticky;
        top: 18px;
        display: grid;
        gap: 16px;
      }
      .rail-card {
        padding: 18px 18px 20px;
      }
      .rail-kicker,
      .section-kicker,
      .related-kind,
      .related-path,
      .fact-label {
        font-family: "IBM Plex Mono", monospace;
        text-transform: uppercase;
        letter-spacing: 0.06em;
      }
      .rail-kicker,
      .section-kicker,
      .fact-label,
      .related-kind,
      .related-path {
        margin: 0 0 8px;
        color: var(--muted);
        font-size: 0.68rem;
      }
      .rail-card h3,
      .section-heading h2,
      .related-card h3 {
        margin: 0;
        line-height: 1.15;
      }
      .rail-copy,
      .citation-current-copy,
      .related-card p,
      .muted-copy {
        margin: 10px 0 0;
        color: #d7d0c7;
        line-height: 1.6;
      }
      .citation-nav {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
        margin-top: 16px;
      }
      .citation-nav-link,
      .citation-nav .note-action {
        width: 100%;
      }
      .citation-current {
        margin-top: 16px;
        padding-top: 16px;
        border-top: 1px solid var(--line);
      }
      .citation-current-title {
        font-weight: 700;
        line-height: 1.35;
      }
      .fact-list {
        display: grid;
        gap: 12px;
      }
      .fact-row {
        display: grid;
        gap: 6px;
        padding-bottom: 12px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      }
      .fact-row:last-child {
        padding-bottom: 0;
        border-bottom: 0;
      }
      .fact-value {
        color: var(--text);
        line-height: 1.45;
        word-break: break-word;
      }
      .fact-value.mono {
        font-family: "IBM Plex Mono", monospace;
        font-size: 0.88rem;
      }
      .related-sections {
        display: grid;
        gap: 20px;
        padding: 0 26px 26px;
      }
      .related-section {
        display: grid;
        gap: 14px;
      }
      .section-heading {
        display: grid;
        gap: 6px;
      }
      .related-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 14px;
      }
      .related-card {
        padding: 18px;
      }
      .related-card h3 a,
      .rail-card a {
        color: var(--accent);
        text-decoration: none;
      }
      .related-topline {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 10px;
      }
      .related-path {
        text-align: right;
        max-width: 50%;
        word-break: break-word;
      }
      @media (max-width: 720px) {
        .note-shell {
          width: min(100%, calc(100% - 16px));
          margin: 8px auto;
          border-radius: 20px;
        }
        .note-header,
        .note-content {
          padding: 20px 18px;
        }
        .note-layout,
        .related-sections {
          padding: 18px;
        }
      }
      @media (max-width: 1080px) {
        .note-layout {
          grid-template-columns: minmax(0, 1fr);
        }
        .note-rail-stack {
          position: static;
        }
      }
    </style>
  </head>
  <body>
    <article class="note-shell">
      <header class="note-header">
        <p class="note-kicker">Vault note</p>
        <h1 class="note-title">${escapeHtml(note.title)}</h1>
        <p class="note-subtitle">${escapeHtml(subtitleBits.join(" • "))}</p>
        ${tagChips}
        <div class="note-actions">${relatedLinks}</div>
      </header>
      <div class="note-layout">
        <main class="note-main">
          <section class="note-content">
            <div class="note-body">${renderMarkdownDocument(stripDuplicateLeadingTitle(note.body, note.title), { context: note.context })}</div>
          </section>
        </main>
        <aside class="note-rail">
          <div class="note-rail-stack">
            <section class="rail-card">
              <p class="rail-kicker">Metadata</p>
              <h3>Source context</h3>
              <div class="fact-list">${metadataFacts}</div>
            </section>
            ${citationRail}
          </div>
        </aside>
      </div>
      <div class="related-sections">
        ${backlinkSection}
        ${chatMentionSection}
      </div>
    </article>
  </body>
</html>`;
}

function renderFact(label, value, kind = "text") {
  const normalized = String(value || "").trim();
  if (!normalized) {
    return "";
  }
  const renderedValue =
    kind === "html"
      ? value
      : `<span class="fact-value${kind === "mono" ? " mono" : ""}">${escapeHtml(normalized)}</span>`;
  if (kind === "html") {
    return `
      <div class="fact-row">
        <div class="fact-label">${escapeHtml(label)}</div>
        <div class="fact-value">${value}</div>
      </div>
    `;
  }
  return `
    <div class="fact-row">
      <div class="fact-label">${escapeHtml(label)}</div>
      ${renderedValue}
    </div>
  `;
}

function parseVaultRequestContext(searchParams) {
  return {
    chatId: String(searchParams?.get("chat") || "").trim(),
    turnId: String(searchParams?.get("turn") || "").trim(),
    sourceIndex: Number.parseInt(String(searchParams?.get("source") || ""), 10) || 0,
  };
}

async function buildCitationSet(relativePath, context = {}) {
  if (!context.chatId || !context.turnId) {
    return null;
  }
  const chat = await readChatTrace(context.chatId);
  if (!chat) {
    return null;
  }
  const turn = Array.isArray(chat.turns) ? chat.turns.find((entry) => entry.id === context.turnId) : null;
  if (!turn) {
    return null;
  }
  const citations = Array.isArray(turn.answer?.citations) ? turn.answer.citations : [];
  if (!citations.length) {
    return null;
  }
  let index = context.sourceIndex > 0 ? context.sourceIndex - 1 : citations.findIndex((citation) => citation.path === relativePath);
  if (index < 0 || index >= citations.length) {
    index = citations.findIndex((citation) => citation.path === relativePath);
  }
  if (index < 0) {
    return null;
  }

  const entries = citations.map((citation, citationIndex) => ({
    ...citation,
    url: buildContextualVaultUrl(citation.path, {
      chatId: chat.id,
      turnId: turn.id,
      sourceIndex: citationIndex + 1,
    }),
  }));

  return {
    index: index + 1,
    total: entries.length,
    current: entries[index],
    previous: index > 0 ? entries[index - 1] : null,
    next: index < entries.length - 1 ? entries[index + 1] : null,
    chatTitle: chat.title || buildOutputTitle(turn.question || "Chat"),
    chatUrl: buildChatUrl(chat.id, turn.id),
  };
}

async function findNoteBacklinks(relativePath) {
  const markdownFiles = await listVaultMarkdownFiles();
  const normalizedPath = String(relativePath || "").replace(/^\/+/, "");
  const pathWithoutExt = normalizedPath.replace(/\.md$/i, "");
  const vaultUrl = buildVaultUrl(normalizedPath);
  const candidates = [
    normalizedPath,
    pathWithoutExt,
    vaultUrl,
    `[[${pathWithoutExt}]]`,
    `[[${pathWithoutExt}|`,
    `[[${normalizedPath}]]`,
    `[[${normalizedPath}|`,
  ];

  const matches = [];
  for (const file of markdownFiles) {
    if (file === normalizedPath) {
      continue;
    }
    const absolutePath = safeJoin(vaultRoot, file);
    if (!absolutePath) {
      continue;
    }
    const text = await fsp.readFile(absolutePath, "utf8");
    const matchedToken = candidates.find((token) => text.includes(token));
    if (!matchedToken) {
      continue;
    }
    const note = await readVaultNote(file);
    matches.push({
      path: file,
      title: note.title,
      noteType: note.noteType,
      url: buildVaultUrl(file),
      excerpt: extractMatchExcerpt(text, matchedToken),
    });
    if (matches.length >= 16) {
      break;
    }
  }

  return matches;
}

async function findChatMentions(relativePath) {
  const chats = await listChatTraces();
  const mentions = [];

  for (const chat of chats) {
    const turns = Array.isArray(chat.turns) ? [...chat.turns].reverse() : [];
    for (const turn of turns) {
      const citations = Array.isArray(turn.answer?.citations) ? turn.answer.citations : [];
      const matchedCitation = citations.find((citation) => citation.path === relativePath);
      if (!matchedCitation) {
        continue;
      }
      mentions.push({
        chatId: chat.id,
        turnId: turn.id,
        chatTitle: chat.title || buildOutputTitle(turn.question || "Chat"),
        question: turn.question || "Open chat turn",
        relevance: matchedCitation.relevance || "",
        snippet: truncate(stripMarkdown(turn.answer?.concise_answer || turn.answer?.answer_markdown || ""), 180),
        dateLabel: formatChatDateLabel(turn.completedAt || turn.startedAt || chat.updatedAt),
        url: buildChatUrl(chat.id, turn.id),
      });
      if (mentions.length >= 12) {
        return mentions;
      }
    }
  }

  return mentions;
}

let markdownFileIndexCache = {
  expiresAt: 0,
  files: [],
};

async function listVaultMarkdownFiles() {
  const now = Date.now();
  if (markdownFileIndexCache.expiresAt > now && markdownFileIndexCache.files.length) {
    return markdownFileIndexCache.files;
  }

  const roots = ["items", "topics", "projects", "dashboards", "outputs"];
  const files = [];
  for (const root of roots) {
    const resolved = path.join(vaultRoot, root);
    if (await dirExists(resolved)) {
      files.push(...(await walkMarkdownFiles(resolved, root)));
    }
  }

  for (const rootFile of ["index.md", "hot.md", "AGENTS.md", "WIKI.md", "README.md"]) {
    const resolved = path.join(vaultRoot, rootFile);
    if (await fileExists(resolved)) {
      files.push(rootFile);
    }
  }

  markdownFileIndexCache = {
    expiresAt: now + 30_000,
    files: [...new Set(files)].sort(),
  };
  return markdownFileIndexCache.files;
}

async function walkMarkdownFiles(absoluteDir, relativeDir) {
  const entries = await fsp.readdir(absoluteDir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const absolutePath = path.join(absoluteDir, entry.name);
    const relativePath = path.posix.join(relativeDir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await walkMarkdownFiles(absolutePath, relativePath)));
      continue;
    }
    if (entry.isFile() && entry.name.endsWith(".md")) {
      files.push(relativePath);
    }
  }
  return files;
}

function extractMatchExcerpt(text, token) {
  const lines = String(text || "").split(/\r?\n/);
  const line = lines.find((entry) => entry.includes(token)) || "";
  return truncate(line.replace(/^#+\s*/, "").replace(/^\s*-\s*/, "").trim(), 180);
}

function deriveSourceSite(sourceUrl, frontmatter = {}) {
  const explicit = firstPresent(frontmatter.site_name, frontmatter.site, frontmatter.publisher);
  if (explicit) {
    return explicit;
  }
  if (!sourceUrl) {
    return "";
  }
  try {
    const hostname = new URL(sourceUrl).hostname.replace(/^www\./, "");
    const labels = hostname.split(".");
    const domain = labels.length > 1 ? labels[labels.length - 2] : labels[0];
    return domain
      .split(/[-_]/g)
      .filter(Boolean)
      .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
      .join(" ");
  } catch {
    return "";
  }
}

function firstPresent(...values) {
  for (const value of values) {
    const normalized = String(value || "").trim();
    if (normalized) {
      return normalized;
    }
  }
  return "";
}

function splitFrontmatter(text) {
  const source = String(text || "");
  if (!source.startsWith("---\n")) {
    return { frontmatter: "", body: source };
  }
  const end = source.indexOf("\n---\n", 4);
  if (end === -1) {
    return { frontmatter: "", body: source };
  }
  return {
    frontmatter: source.slice(4, end),
    body: source.slice(end + 5).trimStart(),
  };
}

function parseSimpleFrontmatter(frontmatter) {
  const result = {};
  const lines = String(frontmatter || "").split(/\r?\n/);
  let currentArrayKey = "";

  for (const rawLine of lines) {
    const line = rawLine.replace(/\t/g, "  ");
    if (!line.trim()) {
      continue;
    }

    const arrayItem = line.match(/^\s*-\s+(.*)$/);
    if (arrayItem && currentArrayKey) {
      if (!Array.isArray(result[currentArrayKey])) {
        result[currentArrayKey] = [];
      }
      result[currentArrayKey].push(parseFrontmatterValue(arrayItem[1]));
      continue;
    }

    const keyValue = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!keyValue) {
      currentArrayKey = "";
      continue;
    }

    const [, key, rawValue] = keyValue;
    const value = rawValue.trim();
    if (!value) {
      result[key] = [];
      currentArrayKey = key;
      continue;
    }

    result[key] = parseFrontmatterValue(value);
    currentArrayKey = "";
  }

  return result;
}

function parseFrontmatterValue(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) {
    return "";
  }
  if (trimmed === "true") {
    return true;
  }
  if (trimmed === "false") {
    return false;
  }
  if (trimmed === "null") {
    return null;
  }
  if ((trimmed.startsWith("\"") && trimmed.endsWith("\"")) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1);
  }
  if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
    return trimmed
      .slice(1, -1)
      .split(",")
      .map((entry) => parseFrontmatterValue(entry))
      .filter(Boolean);
  }
  return trimmed;
}

function renderMarkdownDocument(markdown, options = {}) {
  const lines = String(markdown || "").split("\n");
  const html = [];
  let listType = "";
  let inCodeBlock = false;
  let codeLines = [];

  for (const rawLine of lines) {
    const line = rawLine.replace(/\r$/, "");
    const trimmed = line.trim();

    if (trimmed.startsWith("```")) {
      if (inCodeBlock) {
        html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        inCodeBlock = false;
        codeLines = [];
      } else {
        if (listType) {
          html.push(`</${listType}>`);
          listType = "";
        }
        inCodeBlock = true;
      }
      continue;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }

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
      html.push(`<h3>${renderMarkdownInline(trimmed.slice(4), options)}</h3>`);
      continue;
    }
    if (trimmed.startsWith("## ")) {
      if (listType) {
        html.push(`</${listType}>`);
        listType = "";
      }
      html.push(`<h2>${renderMarkdownInline(trimmed.slice(3), options)}</h2>`);
      continue;
    }
    if (trimmed.startsWith("# ")) {
      if (listType) {
        html.push(`</${listType}>`);
        listType = "";
      }
      html.push(`<h1>${renderMarkdownInline(trimmed.slice(2), options)}</h1>`);
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
      html.push(`<li>${renderMarkdownInline(bulletMatch[1], options)}</li>`);
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
      html.push(`<li>${renderMarkdownInline(orderedMatch[1], options)}</li>`);
      continue;
    }

    if (listType) {
      html.push(`</${listType}>`);
      listType = "";
    }

    html.push(`<p>${renderMarkdownInline(trimmed, options)}</p>`);
  }

  if (inCodeBlock) {
    html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  }
  if (listType) {
    html.push(`</${listType}>`);
  }

  return html.join("");
}

function stripDuplicateLeadingTitle(markdown, title) {
  const source = String(markdown || "").trimStart();
  const normalizedTitle = String(title || "").trim();
  const titlePattern = new RegExp(`^#\\s+${escapeRegExp(normalizedTitle)}\\s*(\\n|$)`, "i");
  return source.replace(titlePattern, "");
}

function renderMarkdownInline(text, options = {}) {
  const context = options.context || {};
  let normalized = String(text || "");
  normalized = normalized.replace(/\[\[([^|\]]+)\|([^\]]+)\]\]/g, (_full, target, label) => `[${label}](${buildContextualVaultUrl(target, context)})`);
  normalized = normalized.replace(/\[\[([^\]]+)\]\]/g, (_full, target) => `[${target}](${buildContextualVaultUrl(target, context)})`);

  const tokenPattern = /`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)/g;
  let html = "";
  let lastIndex = 0;
  let match;

  while ((match = tokenPattern.exec(normalized))) {
    html += formatRenderedText(normalized.slice(lastIndex, match.index));

    if (match[1] != null) {
      html += `<code>${escapeHtml(match[1])}</code>`;
    } else {
      const label = match[2];
      const href = match[3];
      const resolvedHref =
        href.startsWith("http://") || href.startsWith("https://") || href.startsWith("/vault/") || href.startsWith("/vault-raw/")
          ? href
          : href.endsWith(".md")
            ? buildContextualVaultUrl(href, context)
            : buildVaultRawUrl(href);
      html += `<a href="${escapeHtml(resolvedHref)}" target="_blank" rel="noreferrer">${formatRenderedText(label)}</a>`;
    }

    lastIndex = tokenPattern.lastIndex;
  }

  html += formatRenderedText(normalized.slice(lastIndex));
  return html;
}

function formatRenderedText(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function isAllowedVaultPath(relativePath) {
  const clean = relativePath.replace(/^\/+/, "");
  if (!clean || clean.includes("..")) {
    return false;
  }
  return [
    "raw/",
    "items/",
    "topics/",
    "projects/",
    "outputs/",
    "dashboards/",
    "hot.md",
    "index.md",
    "AGENTS.md",
    "WIKI.md",
    "README.md",
  ].some((prefix) => clean === prefix || clean.startsWith(prefix));
}

function safeJoin(root, requestPath) {
  const target = path.resolve(root, `.${requestPath.startsWith("/") ? requestPath : `/${requestPath}`}`);
  return target.startsWith(root) ? target : null;
}

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) {
    return;
  }
  const text = fs.readFileSync(filePath, "utf8");
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const eq = trimmed.indexOf("=");
    if (eq === -1) {
      continue;
    }
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if ((value.startsWith("\"") && value.endsWith("\"")) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) {
      process.env[key] = value;
    }
  }
}

function buildOutputTitle(question) {
  const text = question.replace(/\s+/g, " ").trim();
  if (!text) {
    return "Vault answer";
  }
  const clipped = text.length > 96 ? `${text.slice(0, 93).trim()}...` : text;
  return clipped.endsWith("?") ? clipped.slice(0, -1) : clipped;
}

function slugify(text) {
  const base = String(text || "")
    .normalize("NFKD")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .toLowerCase()
    .replace(/[\s_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return base || crypto.randomBytes(4).toString("hex");
}

function yamlDump(data) {
  return Object.entries(data)
    .map(([key, value]) => `${key}: ${yamlValue(value)}`)
    .join("\n");
}

function yamlValue(value) {
  if (value === null || value === undefined) {
    return "null";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.length ? `[${value.map((entry) => yamlValue(entry)).join(", ")}]` : "[]";
  }
  return JSON.stringify(String(value));
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  return text ? JSON.parse(text) : {};
}

function validateQueryRequest(body) {
  const question = String(body.question || "").trim();
  const threadId = String(body.threadId || "").trim() || null;
  const reasoningEffort = normalizeReasoning(body.reasoningEffort);
  const includeWebSearch = Boolean(body.includeWebSearch);
  const model = String(body.model || DEFAULT_MODEL).trim() || DEFAULT_MODEL;

  if (!question) {
    return { error: "Missing `question`.", status: 400 };
  }

  const apiKey = (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  if (!apiKey) {
    return { error: "Missing OPENAI_API_KEY or CODEX_API_KEY in .env.local.", status: 500 };
  }

  return {
    question,
    threadId,
    reasoningEffort,
    includeWebSearch,
    model,
  };
}

function buildThreadOptions({ model, reasoningEffort, includeWebSearch }) {
  return {
    model,
    workingDirectory: vaultRoot,
    skipGitRepoCheck: true,
    approvalPolicy: "never",
    sandboxMode: "read-only",
    networkAccessEnabled: includeWebSearch,
    webSearchEnabled: includeWebSearch,
    modelReasoningEffort: reasoningEffort,
  };
}

function calculateCost(model, usage) {
  const pricing = MODEL_PRICING[String(model || "").toLowerCase()];
  if (!pricing || !usage) {
    return null;
  }

  const cachedInputTokens = Number(usage.cached_input_tokens || 0);
  const totalInputTokens = Number(usage.input_tokens || 0);
  const uncachedInputTokens = Math.max(0, totalInputTokens - cachedInputTokens);
  const outputTokens = Number(usage.output_tokens || 0);

  const inputCost = (uncachedInputTokens / 1_000_000) * pricing.inputPer1M;
  const cachedInputCost = (cachedInputTokens / 1_000_000) * pricing.cachedInputPer1M;
  const outputCost = (outputTokens / 1_000_000) * pricing.outputPer1M;
  const totalCost = inputCost + cachedInputCost + outputCost;

  return {
    currency: "USD",
    assumption: "standard pricing under 272K context window",
    source: pricing.source,
    inputTokens: uncachedInputTokens,
    cachedInputTokens,
    outputTokens,
    inputUsd: inputCost,
    cachedInputUsd: cachedInputCost,
    outputUsd: outputCost,
    totalUsd: totalCost,
  };
}

async function hydrateAnswer(answer) {
  const citations = [];
  for (const citation of Array.isArray(answer.citations) ? answer.citations : []) {
    const sourceUrl = citation.source_url || (await readPrimaryUrlForCitation(citation.path));
    const vaultUrl = buildVaultUrl(citation.path);
    citations.push({
      ...citation,
      vault_url: vaultUrl,
      source_url: sourceUrl || null,
      citation_url: sourceUrl || vaultUrl,
    });
  }

  return {
    ...answer,
    citations,
    answer_markdown: rewriteAnswerMarkdownLinks(String(answer.answer_markdown || ""), citations),
  };
}

async function readPrimaryUrlForCitation(relativePath) {
  const safePath = safeJoin(vaultRoot, relativePath);
  if (!safePath || !(await fileExists(safePath))) {
    return null;
  }
  const text = await fsp.readFile(safePath, "utf8");
  if (!text.startsWith("---\n")) {
    return null;
  }
  const end = text.indexOf("\n---\n", 4);
  if (end === -1) {
    return null;
  }
  const frontmatter = text.slice(4, end);
  const match = frontmatter.match(/^url:\s*["']?(.+?)["']?\s*$/m);
  return match ? match[1].trim() : null;
}

function rewriteAnswerMarkdownLinks(markdown, citations) {
  let normalized = markdown;
  normalized = normalized.replace(/\[\[([^\]]+)\]\(([^)]+)\)\]/g, "[$1]($2)");

  const byPath = new Map(
    citations
      .filter((citation) => citation.path && citation.citation_url)
      .map((citation) => [citation.path, citation.citation_url]),
  );

  const byVaultUrl = new Map(
    citations
      .filter((citation) => citation.vault_url && citation.citation_url)
      .map((citation) => [citation.vault_url, citation.citation_url]),
  );

  normalized = normalized.replace(/\[\[([^|\]]+)\|([^\]]+)\]\]/g, (_full, target, label) => {
    const citationUrl = byPath.get(target) || buildVaultUrl(target);
    return `[${label}](${citationUrl})`;
  });

  normalized = normalized.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (full, label, href) => {
    const trimmedHref = String(href || "").trim();
    if (!trimmedHref) {
      return full;
    }
    if (trimmedHref.startsWith("http://") || trimmedHref.startsWith("https://")) {
      return full;
    }
    if (byPath.has(trimmedHref)) {
      return `[${label}](${byPath.get(trimmedHref)})`;
    }
    if (byVaultUrl.has(trimmedHref)) {
      return `[${label}](${byVaultUrl.get(trimmedHref)})`;
    }
    if (trimmedHref.endsWith(".md")) {
      return `[${label}](${byPath.get(trimmedHref) || buildVaultUrl(trimmedHref)})`;
    }
    return full;
  });

  return normalized;
}

function buildVaultUrl(relativePath) {
  return `/vault/${encodeURI(String(relativePath || "").replace(/^\/+/, ""))}`;
}

function buildContextualVaultUrl(relativePath, context = {}) {
  const base = buildVaultUrl(relativePath);
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

function buildVaultRawUrl(relativePath) {
  return `/vault-raw/${encodeURI(String(relativePath || "").replace(/^\/+/, ""))}`;
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

function buildObsidianUrl(absolutePath) {
  return `obsidian://open?path=${encodeURIComponent(String(absolutePath || ""))}`;
}

function escapeRegExp(text) {
  return String(text || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function sanitizeChatId(value) {
  return String(value || "")
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "") || randomId();
}

function randomId() {
  return crypto.randomBytes(8).toString("hex");
}

function parseJson(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) {
    throw new Error("Codex returned an empty response");
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    const match = trimmed.match(/\{[\s\S]*\}$/);
    if (!match) {
      throw new Error(`Codex did not return valid JSON: ${trimmed.slice(0, 400)}`);
    }
    return JSON.parse(match[0]);
  }
}

function normalizeReasoning(value) {
  const candidate = String(value || "medium").trim();
  return ["minimal", "low", "medium", "high", "xhigh"].includes(candidate) ? candidate : "medium";
}

async function fileExists(target) {
  try {
    const stat = await fsp.stat(target);
    return stat.isFile();
  } catch {
    return false;
  }
}

async function dirExists(target) {
  try {
    const stat = await fsp.stat(target);
    return stat.isDirectory();
  } catch {
    return false;
  }
}

function truncate(text, limit) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= limit) {
    return clean;
  }
  return `${clean.slice(0, limit - 3).trim()}...`;
}

function formatChatDateLabel(value) {
  if (!value) {
    return "Unknown date";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Unknown date";
  }
  return date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function stripMarkdown(text) {
  return String(text || "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1")
    .replace(/[*_>#-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function json(res, status, payload) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(payload, null, 2));
}

function writeNdjson(res, payload) {
  res.write(`${JSON.stringify(payload)}\n`);
}

function formatError(error) {
  return error instanceof Error ? error.stack || error.message : String(error);
}
