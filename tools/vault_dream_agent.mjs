#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { Codex } from "@openai/codex-sdk";

const DREAM_SCHEMA = {
  type: "object",
  properties: {
    durable_facts: { type: "array", items: { type: "string" } },
    preferences: { type: "array", items: { type: "string" } },
    decisions_or_systems: { type: "array", items: { type: "string" } },
    task_implications: { type: "array", items: { type: "string" } },
    topic_updates: { type: "array", items: { type: "string" } },
    should_promote: { type: "array", items: { type: "string" } },
    summary: { type: "string" },
  },
  required: [
    "durable_facts",
    "preferences",
    "decisions_or_systems",
    "task_implications",
    "topic_updates",
    "should_promote",
    "summary",
  ],
  additionalProperties: false,
};
const PROFILE_HINT_RE = /handoff|profile|personal|preferences|identity/i;

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
  try {
    return JSON.parse(trimmed);
  } catch {
    const match = trimmed.match(/\{[\s\S]*\}$/);
    if (!match) throw new Error(`Codex did not return JSON: ${trimmed.slice(0, 400)}`);
    return JSON.parse(match[0]);
  }
}

async function main() {
  const payload = parseJson(await readStdin());
  const vaultRoot = path.resolve(payload.workingDirectory || process.cwd());
  const apiKey = (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  const context = await buildContext(vaultRoot);

  if (payload.mockResult) {
    process.stdout.write(JSON.stringify(payload.mockResult, null, 2) + "\n");
    return;
  }
  if (!apiKey) {
    throw new Error("Missing OPENAI_API_KEY or CODEX_API_KEY for vault dreaming");
  }

  const codex = new Codex({ apiKey });
  const thread = codex.startThread({
    model: payload.model || process.env.VAULT_DREAM_MODEL || process.env.VAULT_AGENT_MODEL || "gpt-5.4",
    workingDirectory: vaultRoot,
    additionalDirectories: [path.join(vaultRoot, "memory"), path.join(vaultRoot, "items"), path.join(vaultRoot, "topics"), path.join(vaultRoot, "projects")].filter((dir) =>
      fs.existsSync(dir),
    ),
    skipGitRepoCheck: true,
    approvalPolicy: "never",
    sandboxMode: "read-only",
    networkAccessEnabled: false,
    webSearchEnabled: false,
    modelReasoningEffort: payload.reasoningEffort || process.env.VAULT_DREAM_REASONING_EFFORT || "low",
  });

  const turn = await thread.run(buildPrompt(context), { outputSchema: DREAM_SCHEMA });
  const result = extractJson(turn.finalResponse);
  process.stdout.write(JSON.stringify({ ...result, thread_id: thread.id, usage: turn.usage || null }, null, 2) + "\n");
}

function buildPrompt(context) {
  return `
You are the vault dreaming/promoter pass.

Goal:
- Review recent session memory, task state, and key vault context.
- Promote only durable user understanding: preferences, decisions, systems, recurring interests, and task implications.
- Do not invent facts.
- Do not promote one-off noise.
- Keep output compact and human-reviewable.

OpenClaw-inspired rule: memory promotion should be staged and explicit. The output is a reviewable artifact, not hidden memory.

Context:
${context}

Return JSON only.
`.trim();
}

async function buildContext(vaultRoot) {
  const sections = [];
  for (const relative of ["hot.md", "index.md", "dashboards/tasks.md", "dashboards/urgent.md", ".vault/tasks/tasks.json"]) {
    const text = await readText(path.join(vaultRoot, relative), 40_000);
    if (text) sections.push(`### ${relative}\n${trim(text, 5000)}`);
  }
  const memory = await readRecentMemory(vaultRoot);
  if (memory) sections.push(`### recent memory\n${memory}`);
  for (const relative of await discoverProfileHintFiles(vaultRoot)) {
    const text = await readText(path.join(vaultRoot, relative), 100_000);
    if (text) sections.push(`### ${relative}\n${trim(text, 6000)}`);
  }
  return sections.join("\n\n").slice(0, 44_000);
}

async function discoverProfileHintFiles(vaultRoot) {
  const fromEnv = String(process.env.VAULT_PROFILE_HINT_FILES || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const discovered = [];
  const rawDocs = path.join(vaultRoot, "raw", "docs");
  try {
    for (const name of await fsp.readdir(rawDocs)) {
      if (PROFILE_HINT_RE.test(name) && /\.(md|txt)$/i.test(name)) {
        discovered.push(path.join("raw", "docs", name));
      }
    }
  } catch {
    // Profile context is optional.
  }
  return [...new Set([...fromEnv, ...discovered])].slice(0, 8);
}

async function readRecentMemory(vaultRoot) {
  const root = path.join(vaultRoot, "memory");
  try {
    const files = (await fsp.readdir(root))
      .filter((name) => /^\d{4}-\d{2}-\d{2}\.md$/.test(name))
      .sort()
      .reverse()
      .slice(0, 7);
    const chunks = [];
    for (const file of files) {
      const text = await readText(path.join(root, file), 60_000);
      if (text) chunks.push(`### memory/${file}\n${tail(text, 5000)}`);
    }
    return chunks.join("\n\n");
  } catch {
    return "";
  }
}

async function readText(filePath, maxBytes) {
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

function tail(text, maxChars) {
  const clean = String(text || "").trim();
  return clean.length <= maxChars ? clean : `[truncated]\n${clean.slice(-maxChars)}`;
}

function trim(text, maxChars) {
  const clean = String(text || "").trim();
  return clean.length <= maxChars ? clean : `${clean.slice(0, maxChars)}\n[truncated]`;
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack || error.message : String(error)}\n`);
  process.exit(1);
});
