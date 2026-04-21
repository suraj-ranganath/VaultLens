#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { Codex } from "@openai/codex-sdk";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const vaultRoot = path.resolve(__dirname, "..");
const webRoot = path.join(vaultRoot, "web");

loadEnvFile(path.join(vaultRoot, ".env.local"));

const PORT = Number.parseInt(process.env.VAULT_QUERY_PORT || "4318", 10);
const DEFAULT_MODEL = (process.env.VAULT_QUERY_DEFAULT_MODEL || "gpt-5.4").trim();
const codex = new Codex({
  apiKey: (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim(),
});

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
        },
        required: ["path", "title", "note_type", "relevance"],
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

    if (req.method === "GET" && url.pathname.startsWith("/vault/")) {
      return serveVaultFile(url.pathname.replace(/^\/vault\//, ""), res);
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
  const answer = parseJson(turn.finalResponse);

  return json(res, 200, {
    ok: true,
    threadId: thread.id,
    answer,
    trace: summarizeTrace(turn.items),
    usage: turn.usage,
    meta: {
      model,
      reasoningEffort,
      includeWebSearch,
      durationMs: Date.now() - startedAt,
    },
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
- if the vault does not support part of the answer, say that explicitly in \`gaps\`
- keep the answer high-signal and useful, not verbose for its own sake

Return JSON only.

User question:
${question}
`.trim();
}

function summarizeTrace(items) {
  return items
    .flatMap((item) => {
      if (item.type === "command_execution") {
        return [
          {
            type: "command_execution",
            command: item.command,
            status: item.status,
            exit_code: item.exit_code ?? null,
            output_preview: truncate(item.aggregated_output || "", 500),
          },
        ];
      }
      if (item.type === "web_search") {
        return [
          {
            type: "web_search",
            query: item.query,
          },
        ];
      }
      if (item.type === "mcp_tool_call") {
        return [
          {
            type: "mcp_tool_call",
            server: item.server,
            tool: item.tool,
            status: item.status,
            error: item.error?.message || null,
          },
        ];
      }
      if (item.type === "todo_list") {
        return [
          {
            type: "todo_list",
            items: item.items,
          },
        ];
      }
      if (item.type === "error") {
        if ((item.message || "").includes("[features].collab")) {
          return [];
        }
        return [
          {
            type: "error",
            message: item.message,
          },
        ];
      }
      return [];
    })
    .slice(0, 80);
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

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  return text ? JSON.parse(text) : {};
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

function formatError(error) {
  return error instanceof Error ? error.stack || error.message : String(error);
}
