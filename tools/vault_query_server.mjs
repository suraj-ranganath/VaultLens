#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import crypto from "node:crypto";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { Codex } from "@openai/codex-sdk";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const vaultRoot = path.resolve(__dirname, "..");
const webRoot = path.join(vaultRoot, "web");
const chatTraceRoot = path.join(vaultRoot, "outputs", "chat-traces");

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

    if (req.method === "GET" && url.pathname.startsWith("/api/chats/")) {
      return handleGetChat(decodeURIComponent(url.pathname.replace(/^\/api\/chats\//, "")), res);
    }

    if (req.method === "GET" && url.pathname.startsWith("/vault/")) {
      return serveVaultFile(decodeURIComponent(url.pathname.replace(/^\/vault\//, "")), res);
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
  const turn = await thread.run(buildPrompt(question, includeWebSearch), { outputSchema: QUERY_SCHEMA });
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
      durationMs: Date.now() - startedAt,
    },
    feed: [],
    startedAt: new Date(startedAt).toISOString(),
    completedAt: new Date().toISOString(),
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
  const threadOptions = buildThreadOptions({ model, reasoningEffort, includeWebSearch });
  const thread = threadId ? codex.resumeThread(threadId, threadOptions) : codex.startThread(threadOptions);
  const { events } = await thread.runStreamed(buildPrompt(question, includeWebSearch), { outputSchema: QUERY_SCHEMA });

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
        durationMs: Date.now() - startedAt,
      },
      feed,
      startedAt: new Date(startedAt).toISOString(),
      completedAt: new Date().toISOString(),
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

function buildPrompt(question, includeWebSearch) {
  return `
You are answering questions against a local-first personal vault.

Primary objective:
- answer the user's question from the vault as accurately and efficiently as possible

Search discipline:
1. Read \`AGENTS.md\` first if you need the vault contract.
2. Check \`hot.md\` first when it exists.
3. Read the most relevant dashboard or topic/project page next.
4. Use \`rg\` and \`rg --files\` to find candidate notes before opening files.
5. Prefer canonical notes under \`items/\`, \`topics/\`, \`projects/\`, \`outputs/\`, and \`dashboards/\`.
6. Only look at \`raw/\` if the canonical notes are insufficient.
7. Do not modify, create, or delete any files.
8. Keep the tool path efficient. Avoid opening lots of irrelevant files.

Answering rules:
- treat the local vault as the source of truth
- web search is ${includeWebSearch ? "allowed only when the vault is insufficient and external context materially helps" : "disabled for this turn"}
- cite the vault files you actually relied on
- cite paths relative to the vault root
- in \`answer_markdown\`, place inline markdown citations directly next to the claims they support
- every inline vault citation must use markdown links like \`[note](/vault/relative/path.md)\`
- when a cited note has an external primary source, include it inline too as \`[primary](https://...)\` when relevant
- never use Obsidian wiki-link syntax like \`[[...]]\` inside \`answer_markdown\`; use standard markdown links only
- if the vault does not support part of the answer, say that explicitly in \`gaps\`
- keep the answer high-signal and useful, not verbose for its own sake

Return JSON only.

User question:
${question}
`.trim();
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
  return chat;
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

async function serveVaultFile(relativePath, res) {
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

function isAllowedVaultPath(relativePath) {
  const clean = relativePath.replace(/^\/+/, "");
  if (!clean || clean.includes("..")) {
    return false;
  }
  return [
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
    citations.push({
      ...citation,
      vault_url: buildVaultUrl(citation.path),
      source_url: sourceUrl || null,
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
  normalized = normalized.replace(/\[\[([^|\]]+)\|([^\]]+)\]\]/g, (_full, target, label) => `[${label}](${buildVaultUrl(target)})`);

  const byPath = new Map(
    citations
      .filter((citation) => citation.path && citation.vault_url)
      .map((citation) => [citation.path, citation.vault_url]),
  );

  normalized = normalized.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (full, label, href) => {
    const trimmedHref = String(href || "").trim();
    if (!trimmedHref) {
      return full;
    }
    if (trimmedHref.startsWith("/vault/") || trimmedHref.startsWith("http://") || trimmedHref.startsWith("https://")) {
      return full;
    }
    if (byPath.has(trimmedHref)) {
      return `[${label}](${byPath.get(trimmedHref)})`;
    }
    if (trimmedHref.endsWith(".md")) {
      return `[${label}](${buildVaultUrl(trimmedHref)})`;
    }
    return full;
  });

  const byVaultUrl = new Map(
    citations
      .filter((citation) => citation.vault_url && citation.source_url)
      .map((citation) => [citation.vault_url, citation.source_url]),
  );

  normalized = normalized.replace(/\[([^\]]+)\]\((\/vault\/[^)]+)\)(?!\s*\[primary\]\()/g, (full, label, href) => {
    const sourceUrl = byVaultUrl.get(href);
    if (!sourceUrl) {
      return full;
    }
    return `${full} [primary](${sourceUrl})`;
  });

  return normalized;
}

function buildVaultUrl(relativePath) {
  return `/vault/${encodeURI(String(relativePath || "").replace(/^\/+/, ""))}`;
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

function truncate(text, limit) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= limit) {
    return clean;
  }
  return `${clean.slice(0, limit - 3).trim()}...`;
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
