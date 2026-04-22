#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { Codex } from "@openai/codex-sdk";

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

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`Failed to parse JSON input: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function extractJson(text) {
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
- cite the vault files you actually relied on in the structured \`citations\` array
- cite paths relative to the vault root in the structured \`citations\` array
- in \`answer_markdown\`, place inline markdown citations directly next to the claims they support
- user-facing citation links in \`answer_markdown\` should prefer the external primary source URL for each cited note when available
- only fall back to \`/vault/...\` note URLs when no external primary source exists for that citation
- never use Obsidian wiki-link syntax like \`[[...]]\` inside \`answer_markdown\`; use standard markdown links only
- if the vault does not support part of the answer, say that explicitly in \`gaps\`
- keep the answer high-signal and useful, not verbose for its own sake

Return JSON only.

User question:
${question}
`.trim();
}

function buildThreadOptions({ model, reasoningEffort, includeWebSearch, workingDirectory }) {
  return {
    model,
    workingDirectory,
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
    totalUsd: inputCost + cachedInputCost + outputCost,
  };
}

async function hydrateAnswer(answer, vaultRoot) {
  const citations = [];
  for (const citation of Array.isArray(answer.citations) ? answer.citations : []) {
    const sourceUrl = citation.source_url || (await readPrimaryUrlForCitation(vaultRoot, citation.path));
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

async function readPrimaryUrlForCitation(vaultRoot, relativePath) {
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
  let normalized = String(markdown || "");
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
    const url = byPath.get(target) || buildVaultUrl(target);
    return `[${label}](${url})`;
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

function summarizeTrace(items) {
  return items.map(normalizeThreadItem).filter(Boolean).slice(0, 80);
}

function normalizeThreadItem(item) {
  if (item.type === "command_execution") {
    return {
      id: item.id,
      type: "command_execution",
      command: item.command,
      status: item.status,
      exit_code: item.exit_code ?? null,
      output_preview: truncate(item.aggregated_output || "", 400),
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
      arguments_preview: truncate(JSON.stringify(item.arguments || {}), 220),
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

function buildVaultUrl(relativePath) {
  return `/vault/${encodeURI(String(relativePath || "").replace(/^\/+/, ""))}`;
}

function safeJoin(root, requestPath) {
  const target = path.resolve(root, `.${requestPath.startsWith("/") ? requestPath : `/${requestPath}`}`);
  return target.startsWith(root) ? target : null;
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

async function main() {
  const raw = await readStdin();
  const payload = parseJson(raw);

  const apiKey = (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  if (!apiKey) {
    throw new Error("Missing OPENAI_API_KEY or CODEX_API_KEY for vault query runner");
  }

  const vaultRoot = path.resolve(payload.workingDirectory || process.cwd());
  const question = String(payload.question || "").trim();
  if (!question) {
    throw new Error("Missing question");
  }

  const includeWebSearch = Boolean(payload.includeWebSearch ?? true);
  const model = String(payload.model || "gpt-5.4").trim() || "gpt-5.4";
  const reasoningEffort = String(payload.reasoningEffort || "medium").trim() || "medium";

  const codex = new Codex({ apiKey });
  const threadOptions = buildThreadOptions({
    model,
    reasoningEffort,
    includeWebSearch,
    workingDirectory: vaultRoot,
  });

  const thread = payload.threadId ? codex.resumeThread(payload.threadId, threadOptions) : codex.startThread(threadOptions);
  const startedAt = Date.now();
  const turn = await thread.run(buildPrompt(question, includeWebSearch), { outputSchema: QUERY_SCHEMA });
  const answer = await hydrateAnswer(extractJson(turn.finalResponse), vaultRoot);
  const usage = turn.usage || null;
  const cost = calculateCost(model, usage);

  process.stdout.write(
    JSON.stringify(
      {
        threadId: thread.id,
        answer,
        usage,
        cost,
        trace: summarizeTrace(turn.items),
        meta: {
          model,
          reasoningEffort,
          includeWebSearch,
          durationMs: Date.now() - startedAt,
        },
      },
      null,
      2,
    ) + "\n",
  );
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack || error.message : String(error)}\n`);
  process.exit(1);
});
