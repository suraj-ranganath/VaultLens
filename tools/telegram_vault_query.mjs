#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Codex } from "@openai/codex-sdk";

const execFileAsync = promisify(execFile);
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

const SEARCH_ROOTS = ["items", "topics", "projects", "dashboards", "outputs"];
const CORE_CONTEXT_FILES = ["hot.md", "index.md", "log.md"];
const TELEGRAM_CONTEXT_FILES = [
  "imports/telegram-inbox/telegram-live.txt",
  "imports/telegram-inbox/telegram-live telegram-agent-decisions.jsonl",
  "imports/telegram-inbox/telegram-live .telegram_processed_updates.jsonl",
];
const STOPWORDS = new Set([
  "about",
  "after",
  "again",
  "and",
  "are",
  "based",
  "because",
  "been",
  "being",
  "can",
  "could",
  "does",
  "for",
  "from",
  "give",
  "have",
  "how",
  "who",
  "anf",
  "the",
  "hi",
  "hey",
  "hello",
  "iam",
  "am",
  "into",
  "just",
  "know",
  "like",
  "more",
  "that",
  "their",
  "there",
  "this",
  "what",
  "when",
  "where",
  "which",
  "while",
  "with",
  "would",
  "you",
  "your",
]);

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

function buildPrompt(question, includeWebSearch, vaultContext) {
  return `
You are answering questions against a local-first personal vault.

Primary objective:
- answer the user's question from the vault as accurately and efficiently as possible
- you have a guaranteed vault context pack below that was read directly from local vault files before this agent turn
- if shell/file tools are unavailable or fail, use the guaranteed context pack instead of saying the vault is inaccessible
- do not claim you cannot access the vault when relevant evidence appears in the guaranteed context pack

Search discipline:
1. Read \`AGENTS.md\` first if you need the vault contract.
2. Check \`hot.md\` first when it exists.
3. For Telegram follow-up questions, resolve "this", "that", "it", "there", "the link", "the screenshot", "that role", and "the previous one" from the recent Telegram context in the guaranteed context pack before doing broad vault search.
4. Use \`rg\` and \`rg --files\` to find candidate notes before opening files.
5. Prefer canonical notes under \`items/\`, \`topics/\`, \`projects/\`, \`outputs/\`, and \`dashboards/\`.
6. If the user asks how something was ingested, stored, classified, or processed, inspect \`imports/telegram-inbox/\`, especially processed update logs and agent decision traces.
7. Only look at \`raw/\` if the canonical notes are insufficient.
8. Do not modify, create, or delete any files.
9. Keep the tool path efficient. Avoid opening lots of irrelevant files.

Answering rules:
- treat the local vault as the source of truth
- for ingestion-history questions, use stored ingest traces and decision logs rather than guessing
- treat recent Telegram links, screenshot/image summaries, QR extracts, attachment artifact paths, and recent agent decisions as first-class evidence when the question references recent conversation context
- if a referenced recent link/image has not been fully compiled into a canonical note yet, answer from the recent Telegram context and clearly say that the answer is based on the recent message or attachment analysis
- web search is ${includeWebSearch ? "allowed only when the vault is insufficient and external context materially helps" : "disabled for this turn"}
- if a question asks about a person and the vault only has a relationship note plus a profile/linkedin reference, still use that as weak context and clearly label it as limited evidence
- for compatibility/person-summary questions, combine what the vault knows about the user with any captured profile/person notes; if profile detail is thin and web search is enabled, you may look up public context from the provided profile URL, but distinguish searched public context from vault memory
- cite the vault files you actually relied on in the structured \`citations\` array
- cite paths relative to the vault root in the structured \`citations\` array
- in \`answer_markdown\`, place inline markdown citations directly next to the claims they support
- user-facing citation links in \`answer_markdown\` should prefer the external primary source URL for each cited note when available
- only fall back to \`/vault/...\` note URLs when no external primary source exists for that citation
- never use Obsidian wiki-link syntax like \`[[...]]\` inside \`answer_markdown\`; use standard markdown links only
- if the vault does not support part of the answer, say that explicitly in \`gaps\`
- keep the answer high-signal and useful, not verbose for its own sake
- write like a sharp, warm personal assistant in Telegram, not a compliance report
- start with the direct answer, then give the surrounding context that makes the answer useful
- be a little engaging and human: light emojis are good when natural, but do not overdo them
- if the question is personal, advisory, or taste-based, sound like a smart friend who knows the user's context
- avoid stiff phrases like "The vault can confirm"; prefer natural phrasing like "Yep — I have this saved as..."
- keep caveats honest but not deflating; say what is known, what is inferred, and what would improve the answer
- include a useful next-step offer only when it fits, e.g. "If you want, I can also..."
- \`concise_answer\` should be the Telegram-ready response: direct, conversational, and usually 1-6 short paragraphs

Return JSON only.

Guaranteed vault context pack:
${vaultContext || "(No preloaded vault context was found.)"}

User question:
${question}
`.trim();
}

function buildThreadOptions({ model, reasoningEffort, includeWebSearch, workingDirectory }) {
  return {
    model,
    workingDirectory,
    additionalDirectories: [
      path.join(workingDirectory, "items"),
      path.join(workingDirectory, "topics"),
      path.join(workingDirectory, "projects"),
      path.join(workingDirectory, "dashboards"),
      path.join(workingDirectory, "outputs"),
      path.join(workingDirectory, "imports"),
      path.join(workingDirectory, "raw", "docs"),
    ].filter((dir) => fs.existsSync(dir)),
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

async function buildVaultContextPack(vaultRoot, question, recentConversationContext = "") {
  const terms = queryTerms(question);
  const sections = [];
  await compileVaultCache(vaultRoot);

  const recentContext = String(recentConversationContext || "").trim();
  if (recentContext) {
    sections.push(renderContextSection("telegram/recent-conversation-context", trimForContext(recentContext, 14_000)));
  }

  const digestContext = await readDigestContext(vaultRoot);
  if (digestContext) {
    sections.push(renderContextSection(".vault/cache/agent-digest.json", digestContext));
  }

  for (const relativePath of CORE_CONTEXT_FILES) {
    const text = await readTextIfExists(path.join(vaultRoot, relativePath));
    if (text) {
      sections.push(renderContextSection(relativePath, trimForContext(text, 2400)));
    }
  }

  for (const relativePath of TELEGRAM_CONTEXT_FILES) {
    const text = await readTextIfExists(path.join(vaultRoot, relativePath));
    if (text) {
      const summary = relativePath.includes("telegram-agent-decisions")
        ? summarizeTelegramDecisionLog(text)
        : tailLines(text, relativePath.endsWith(".jsonl") ? 30 : 90);
      sections.push(renderContextSection(relativePath, summary));
    }
  }

  for (const item of await relationshipContextItems(vaultRoot, question)) {
    sections.push(renderContextSection(item.relativePath, item.snippet));
  }

  for (const item of await profileContextItems(vaultRoot, terms)) {
    sections.push(renderContextSection(item.relativePath, item.snippet));
  }

  const searchResults = await searchVaultCache(vaultRoot, question, 16);
  for (const result of searchResults) {
    const body = [
      `title: ${result.title || ""}`,
      `type: ${result.type || ""}`,
      result.url ? `url: ${result.url}` : "",
      result.published_on ? `published_on: ${result.published_on}` : "",
      result.discovered_on ? `discovered_on: ${result.discovered_on}` : "",
      result.deadline ? `deadline: ${result.deadline}` : "",
      Array.isArray(result.tags) && result.tags.length ? `tags: ${result.tags.join(", ")}` : "",
      Array.isArray(result.topics) && result.topics.length ? `topics: ${result.topics.join(", ")}` : "",
      `retrieval_score: ${Number(result.score || 0).toFixed(4)}`,
      result.snippet ? `snippet: ${result.snippet}` : "",
    ]
      .filter(Boolean)
      .join("\n");
    sections.push(renderContextSection(result.path, body));
  }

  const candidates = await collectMarkdownCandidates(vaultRoot);
  const alreadyIncluded = new Set(searchResults.map((result) => result.path));
  const ranked = candidates
    .map((candidate) => ({
      ...candidate,
      score: scoreCandidate(candidate.relativePath, candidate.text, terms, question),
    }))
    .filter((candidate) => candidate.score > 0 && !alreadyIncluded.has(candidate.relativePath))
    .sort((a, b) => b.score - a.score || a.relativePath.localeCompare(b.relativePath))
    .slice(0, 8);

  for (const candidate of ranked) {
    sections.push(renderContextSection(candidate.relativePath, summarizeMarkdown(candidate.text)));
  }

  return sections.join("\n\n").slice(0, 34_000);
}

async function compileVaultCache(vaultRoot) {
  const script = path.join(vaultRoot, "tools", "vault_compile_cache.py");
  if (!(await fileExists(script))) {
    return;
  }
  try {
    await execFileAsync("python3", [script, "--vault-root", vaultRoot, "--quiet"], {
      cwd: vaultRoot,
      timeout: 45_000,
      maxBuffer: 1024 * 1024,
    });
  } catch {
    // The query path can still work from direct markdown reads if cache compile fails.
  }
}

async function searchVaultCache(vaultRoot, question, limit) {
  const script = path.join(vaultRoot, "tools", "vault_search.py");
  if (!(await fileExists(script))) {
    return [];
  }
  try {
    const { stdout } = await execFileAsync(
      "python3",
      [script, "search", "--vault-root", vaultRoot, "--query", question, "--limit", String(limit)],
      {
        cwd: vaultRoot,
        timeout: 30_000,
        maxBuffer: 2 * 1024 * 1024,
      },
    );
    const parsed = JSON.parse(stdout);
    return Array.isArray(parsed.results) ? parsed.results : [];
  } catch {
    return [];
  }
}

async function readDigestContext(vaultRoot) {
  const digestPath = path.join(vaultRoot, ".vault", "cache", "agent-digest.json");
  const text = await readTextIfExists(digestPath, 10_000_000);
  if (!text) {
    return "";
  }
  try {
    const digest = JSON.parse(text);
    const pages = Array.isArray(digest.pages) ? digest.pages : [];
    const recent = pages
      .slice()
      .sort((a, b) =>
        String(b.discovered_on || b.published_on || "").localeCompare(String(a.discovered_on || a.published_on || "")),
      )
      .slice(0, 24);
    return JSON.stringify(
      {
        generated_at: digest.generated_at,
        page_count: digest.page_count,
        claim_count: digest.claim_count,
        source_count: digest.source_count,
        recent_pages: recent.map((page) => ({
          path: page.path,
          title: page.title,
          type: page.type,
          summary: page.summary,
          url: page.url,
          discovered_on: page.discovered_on,
          published_on: page.published_on,
          deadline: page.deadline,
          tags: page.tags,
          topics: page.topics,
        })),
      },
      null,
      2,
    );
  } catch {
    return trimForContext(text, 5000);
  }
}

async function relationshipContextItems(vaultRoot, question) {
  if (!/\bdating|relationship|partner|girlfriend|boyfriend|compatible|compatibility\b/i.test(question)) {
    return [];
  }

  const textParts = [];
  for (const relativePath of TELEGRAM_CONTEXT_FILES) {
    const text = await readTextIfExists(path.join(vaultRoot, relativePath));
    if (text) {
      textParts.push(text);
    }
  }
  const names = extractRelationshipNames(textParts.join("\n")).slice(0, 5);
  if (!names.length) {
    return [];
  }

  const files = await collectMarkdownCandidates(vaultRoot);
  const items = [];
  const seen = new Set();
  for (const name of names) {
    const parts = name.toLowerCase().split(/\s+/).filter(Boolean);
    for (const candidate of files) {
      const haystack = `${candidate.relativePath}\n${frontmatterAndHead(candidate.text)}`.toLowerCase();
      if (parts.every((part) => haystack.includes(part)) && !seen.has(candidate.relativePath)) {
        seen.add(candidate.relativePath);
        items.push({
          relativePath: candidate.relativePath,
          snippet: summarizeMarkdown(candidate.text),
        });
      }
    }
  }
  return items.slice(0, 8);
}

function extractRelationshipNames(text) {
  const names = new Set();
  const patterns = [
    /\bdating\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)/g,
    /\brelationship-status memory:\s*they are dating\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)/g,
    /\bLinkedIn profile link for\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)/g,
    /\blinkedin-profile-([a-z]+)-([a-z]+)/gi,
  ];
  for (const pattern of patterns) {
    for (const match of String(text || "").matchAll(pattern)) {
      if (match.length >= 3 && pattern.flags.includes("i")) {
        names.add(`${capitalize(match[1])} ${capitalize(match[2])}`.trim());
      } else if (match[1]) {
        names.add(match[1].trim());
      }
    }
  }
  return [...names];
}

function capitalize(value) {
  const text = String(value || "").trim();
  return text ? `${text[0].toUpperCase()}${text.slice(1).toLowerCase()}` : "";
}

function queryTerms(question) {
  const words = String(question || "")
    .toLowerCase()
    .match(/[a-z0-9][a-z0-9_-]{2,}/g) || [];
  return [...new Set(words.filter((word) => !STOPWORDS.has(word)))].slice(0, 24);
}

async function profileContextItems(vaultRoot, terms) {
  const rawDocs = path.join(vaultRoot, "raw", "docs");
  const files = await listFiles(rawDocs, [".md", ".txt"]);
  const profileFiles = files.filter((file) => /handoff|profile|personal|preferences|identity/i.test(path.basename(file)));
  const personalQuery = /\b(who am i|about me|dating|compatible|compatibility|preference|values|personality|profile|know about me)\b/i.test(
    terms.join(" "),
  );
  const selected = personalQuery ? profileFiles.slice(0, 8) : profileFiles.slice(0, 3);
  const items = [];
  for (const file of selected) {
    const text = await readTextIfExists(file);
    if (text) {
      items.push({
        relativePath: path.relative(vaultRoot, file),
        snippet: trimForContext(text, personalQuery ? 4200 : 2000),
      });
    }
  }
  return items;
}

async function collectMarkdownCandidates(vaultRoot) {
  const files = [];
  for (const root of SEARCH_ROOTS) {
    files.push(...(await listFiles(path.join(vaultRoot, root), [".md"])));
  }
  const candidates = [];
  for (const file of files.slice(0, 2500)) {
    const text = await readTextIfExists(file);
    if (!text) {
      continue;
    }
    candidates.push({
      relativePath: path.relative(vaultRoot, file),
      text,
    });
  }
  return candidates;
}

function scoreCandidate(relativePath, text, terms, question) {
  const haystack = `${relativePath}\n${frontmatterAndHead(text)}`.toLowerCase();
  let score = 0;
  for (const term of terms) {
    if (termMatches(haystack, term)) {
      score += relativePath.toLowerCase().includes(term) ? 5 : 2;
    }
  }
  if (/\bdating|relationship|partner|girlfriend|boyfriend|compatible|compatibility\b/i.test(question)) {
    if (/\bdating|girlfriend|boyfriend|compatible|compatibility|arunima\b/i.test(haystack)) {
      score += 20;
    }
  }
  if (/\bjob|apply|application|intern|role|deadline|opportunit/i.test(question) && /items\/jobs|opportunit|deadline|application/i.test(haystack)) {
    score += 12;
  }
  if (/\bevent|calendar|schedule|when|where\b/i.test(question) && /items\/events|event|calendar|schedule|location/i.test(haystack)) {
    score += 12;
  }
  return score;
}

function termMatches(haystack, term) {
  const escaped = String(term || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^a-z0-9_-])${escaped}([^a-z0-9_-]|$)`, "i").test(haystack);
}

function summarizeMarkdown(text) {
  const source = String(text || "");
  const parts = [];
  const frontmatter = extractFrontmatter(source);
  if (frontmatter) {
    parts.push(frontmatter);
  }
  const body = source.replace(/^---\n[\s\S]*?\n---\n/, "").trim();
  const usefulLines = body.split(/\r?\n/);
  const selected = usefulLines
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("![]("))
    .slice(0, 45)
    .join("\n");
  if (selected) {
    parts.push(selected);
  }
  return trimForContext(parts.join("\n\n"), 3200);
}

function frontmatterAndHead(text) {
  const source = String(text || "");
  return `${extractFrontmatter(source) || ""}\n${source.replace(/^---\n[\s\S]*?\n---\n/, "").slice(0, 2500)}`;
}

function extractFrontmatter(text) {
  const source = String(text || "");
  if (!source.startsWith("---\n")) {
    return "";
  }
  const end = source.indexOf("\n---\n", 4);
  return end === -1 ? "" : source.slice(0, end + 5).trim();
}

async function listFiles(root, extensions) {
  const results = [];
  if (!(await pathExists(root))) {
    return results;
  }
  const entries = await fsp.readdir(root, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.name.startsWith("._")) {
      continue;
    }
    const fullPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      results.push(...(await listFiles(fullPath, extensions)));
    } else if (entry.isFile() && extensions.includes(path.extname(entry.name).toLowerCase())) {
      results.push(fullPath);
    }
  }
  return results;
}

async function readTextIfExists(filePath, maxBytes = 80_000) {
  try {
    const stat = await fsp.stat(filePath);
    if (!stat.isFile()) {
      return "";
    }
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

async function pathExists(target) {
  try {
    await fsp.access(target);
    return true;
  } catch {
    return false;
  }
}

function renderContextSection(relativePath, body) {
  return `### ${relativePath}\n${String(body || "").trim()}`;
}

function tailLines(text, count) {
  const lines = String(text || "").split(/\r?\n/).filter(Boolean);
  return lines.slice(-count).join("\n");
}

function summarizeTelegramDecisionLog(text) {
  const lines = String(text || "").split(/\r?\n/).filter(Boolean).slice(-16);
  const summaries = [];
  for (const line of lines) {
    try {
      const record = JSON.parse(line);
      const message = record.message || {};
      const decision = record.decision || {};
      const actions = Array.isArray(record.actions)
        ? record.actions.map((action) => action.tool).filter(Boolean).join(", ")
        : "";
      summaries.push(
        [
          `logged_at: ${record.logged_at || ""}`,
          `message: ${message.raw_text || message.export_line || ""}`,
          `classification: ${decision.classification || ""}`,
          `stored: ${Boolean(decision.storeInVault)}`,
          `summary: ${decision.summary || ""}`,
          actions ? `actions: ${actions}` : "",
        ]
          .filter(Boolean)
          .join("\n"),
      );
    } catch {
      summaries.push(line.slice(0, 500));
    }
  }
  return summaries.join("\n\n");
}

function trimForContext(text, limit) {
  const clean = String(text || "").trim();
  if (clean.length <= limit) {
    return clean;
  }
  return `${clean.slice(0, limit).trim()}\n[truncated]`;
}

async function hydrateAnswer(answer, vaultRoot) {
  const citations = [];
  for (const citation of Array.isArray(answer.citations) ? answer.citations : []) {
    const sourceUrl = await sourceUrlForCitation(vaultRoot, citation);
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

async function sourceUrlForCitation(vaultRoot, citation) {
  const relativePath = String(citation.path || "").trim();
  if (!relativePath.startsWith("items/") && !relativePath.startsWith("topics/") && !relativePath.startsWith("projects/") && !relativePath.startsWith("outputs/")) {
    return null;
  }
  return citation.source_url || (await readPrimaryUrlForCitation(vaultRoot, relativePath));
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
  const recentConversationContext = String(payload.recentConversationContext || "");
  const vaultContext = await buildVaultContextPack(vaultRoot, question, recentConversationContext);
  if (process.env.VAULT_QUERY_CONTEXT_ONLY === "1") {
    process.stdout.write(
      JSON.stringify(
        {
          question,
          vaultContextBytes: Buffer.byteLength(vaultContext, "utf8"),
          vaultContext,
        },
        null,
        2,
      ) + "\n",
    );
    return;
  }

  const codex = new Codex({ apiKey });
  const threadOptions = buildThreadOptions({
    model,
    reasoningEffort,
    includeWebSearch,
    workingDirectory: vaultRoot,
  });

  const thread = payload.threadId ? codex.resumeThread(payload.threadId, threadOptions) : codex.startThread(threadOptions);
  const startedAt = Date.now();
  const turn = await thread.run(buildPrompt(question, includeWebSearch, vaultContext), { outputSchema: QUERY_SCHEMA });
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
          vaultContextBytes: Buffer.byteLength(vaultContext, "utf8"),
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
