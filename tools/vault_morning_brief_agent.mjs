#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { Codex } from "@openai/codex-sdk";

const BRIEF_SCHEMA = {
  type: "object",
  properties: {
    should_send: { type: "boolean" },
    telegram_text: { type: "string" },
    selected_actions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          title: { type: "string" },
          kind: { type: "string" },
          date: { type: ["string", "null"] },
          source: { type: ["string", "null"] },
          why_today: { type: "string" },
          suggested_next_step: { type: "string" },
        },
        required: ["title", "kind", "date", "source", "why_today", "suggested_next_step"],
        additionalProperties: false,
      },
    },
    recommended_reading: {
      type: ["object", "null"],
      properties: {
        title: { type: "string" },
        source: { type: ["string", "null"] },
        why_this_matters: { type: "string" },
      },
      required: ["title", "source", "why_this_matters"],
      additionalProperties: false,
    },
    rationale: { type: "string" },
  },
  required: ["should_send", "telegram_text", "selected_actions", "recommended_reading", "rationale"],
  additionalProperties: false,
};

const CORE_CONTEXT_FILES = ["hot.md", "index.md", "dashboards/urgent.md", "dashboards/reading-queue.md", "log.md"];
const PROFILE_HINT_FILES = [
  "raw/docs/suraj_agent_handoff_master_profile.md",
  "raw/docs/suraj_agent_handoff_profile.md",
];

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

async function main() {
  const raw = await readStdin();
  const payload = parseJson(raw);
  const apiKey = (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  if (!apiKey) {
    throw new Error("Missing OPENAI_API_KEY or CODEX_API_KEY for morning brief agent");
  }

  const vaultRoot = path.resolve(payload.workingDirectory || process.cwd());
  const today = String(payload.today || new Date().toISOString().slice(0, 10));
  const model = String(payload.model || "gpt-5.4");
  const reasoningEffort = String(payload.reasoningEffort || "medium");
  const candidateActions = Array.isArray(payload.candidateActions) ? payload.candidateActions : [];
  const candidateReadings = Array.isArray(payload.candidateReadings) ? payload.candidateReadings : [];
  const candidateCalendarEvents = Array.isArray(payload.candidateCalendarEvents) ? payload.candidateCalendarEvents : [];
  const calendarError = String(payload.calendarError || "");
  const maxActions = Number(payload.maxActions || 5);
  const contextPack = await buildContextPack(vaultRoot, candidateActions, candidateReadings, candidateCalendarEvents, calendarError);

  const prompt = buildPrompt({
    today,
    maxActions,
    candidateActions,
    candidateReadings,
    candidateCalendarEvents,
    calendarError,
    contextPack,
  });

  const codex = new Codex({ apiKey });
  const thread = codex.startThread({
    model,
    workingDirectory: vaultRoot,
    additionalDirectories: [
      path.join(vaultRoot, "items"),
      path.join(vaultRoot, "topics"),
      path.join(vaultRoot, "projects"),
      path.join(vaultRoot, "dashboards"),
      path.join(vaultRoot, "outputs"),
      path.join(vaultRoot, "raw", "docs"),
      path.join(vaultRoot, "imports"),
    ].filter((dir) => fs.existsSync(dir)),
    skipGitRepoCheck: true,
    approvalPolicy: "never",
    sandboxMode: "read-only",
    networkAccessEnabled: false,
    webSearchEnabled: false,
    modelReasoningEffort: reasoningEffort,
  });

  const turn = await thread.run(prompt, { outputSchema: BRIEF_SCHEMA });
  const brief = extractJson(turn.finalResponse);
  process.stdout.write(
    JSON.stringify(
      {
        ...brief,
        thread_id: thread.id,
        usage: turn.usage || null,
        context_bytes: Buffer.byteLength(contextPack, "utf8"),
      },
      null,
      2,
    ) + "\n",
  );
}

function buildPrompt({ today, maxActions, candidateActions, candidateReadings, candidateCalendarEvents, calendarError, contextPack }) {
  return `
You are Suraj's morning brief agent.

Goal:
- Create a smart, personalized, high-signal morning Telegram brief for ${today}.
- This is not a deterministic digest. Use judgment.
- The deterministic candidate lists are only a shortlist. You decide what actually matters.
- Use Suraj's profile, active systems, interests, decisions, reminders, jobs, saved links, and recent curiosity to choose what deserves attention today.

Selection principles:
- Include only things that are genuinely urgent, high impact, or intentionally saved for follow-up.
- Prioritize deadlines/reminders within the next week, event logistics, job/opportunity items where applying early matters, and explicit promises/reminders from the user.
- Check today's Google Calendar events. Include important events happening today, especially meetings, classes, interviews, appointments, travel, presentations, or anything with logistics Suraj needs to notice. Omit trivial holds, low-signal blockers, declined events, and filler.
- Treat explicit user priority as a strong signal. If the user marked something high or critical priority, it should usually appear unless it is stale, already done, or clearly less important than tighter deadline items.
- Include at most one recommended reading. Pick the one recent item most likely to compound Suraj's current work, taste, or thinking.
- It is okay to send no brief if there is no meaningful signal.
- Do not include filler. Do not include low-signal "maybe read this" items.
- Preserve uncertainty honestly: if a date/deadline is inferred or weak, say that briefly.
- If calendar access failed or no calendar events were fetched, do not invent calendar events.
- Write as a useful personal assistant, not a report.
- Be direct, warm, and slightly engaging. Light emojis are fine if natural.
- The brief should wake him up with clear action items for the day.

Output rules:
- telegram_text must be ready to send as plain Telegram text.
- Keep telegram_text compact: usually 5-12 lines.
- Start with a short heading.
- Use raw source URLs when available; otherwise use the note path.
- selected_actions should match the actions included in telegram_text.
- recommended_reading should match the reading included in telegram_text, or null.

Candidate action shortlist:
${JSON.stringify(candidateActions, null, 2)}

Candidate reading shortlist:
${JSON.stringify(candidateReadings, null, 2)}

Candidate Google Calendar events for today:
${candidateCalendarEvents.length ? JSON.stringify(candidateCalendarEvents, null, 2) : calendarError ? `Calendar unavailable: ${calendarError}` : "[]"}

Guaranteed vault/profile context:
${contextPack || "(No additional context found.)"}

Return JSON only.
`.trim();
}

async function buildContextPack(vaultRoot, candidateActions, candidateReadings, candidateCalendarEvents, calendarError) {
  const sections = [];
  if (candidateCalendarEvents.length || calendarError) {
    sections.push(
      renderSection(
        "google-calendar/today",
        candidateCalendarEvents.length ? JSON.stringify(candidateCalendarEvents, null, 2) : `Calendar unavailable: ${calendarError}`,
      ),
    );
  }

  for (const relativePath of CORE_CONTEXT_FILES) {
    const text = await readTextIfExists(path.join(vaultRoot, relativePath));
    if (text) {
      sections.push(renderSection(relativePath, trimForContext(text, relativePath === "log.md" ? 1800 : 3200)));
    }
  }

  const digest = await readDigestSummary(vaultRoot);
  if (digest) {
    sections.push(renderSection(".vault/cache/agent-digest.json", digest));
  }

  for (const relativePath of PROFILE_HINT_FILES) {
    const text = await readTextIfExists(path.join(vaultRoot, relativePath), 120_000);
    if (text) {
      sections.push(renderSection(relativePath, trimForContext(text, 5200)));
    }
  }

  for (const item of [...candidateActions, ...candidateReadings]) {
    const relativePath = String(item.path || "").trim();
    if (!relativePath) {
      continue;
    }
    const text = await readTextIfExists(path.join(vaultRoot, relativePath), 80_000);
    if (text) {
      sections.push(renderSection(relativePath, summarizeMarkdown(text)));
    }
  }

  const recentTelegram = await readTextIfExists(path.join(vaultRoot, "imports/telegram-inbox/telegram-live.txt"));
  if (recentTelegram) {
    sections.push(renderSection("imports/telegram-inbox/telegram-live.txt", tailLines(recentTelegram, 80)));
  }

  const recentMemory = await readRecentMemory(vaultRoot);
  if (recentMemory) {
    sections.push(renderSection("memory/recent-session-memory", recentMemory));
  }

  return sections.join("\n\n").slice(0, 42_000);
}

async function readRecentMemory(vaultRoot) {
  const memoryRoot = path.join(vaultRoot, "memory");
  try {
    const files = (await fsp.readdir(memoryRoot))
      .filter((name) => /^\d{4}-\d{2}-\d{2}\.md$/.test(name))
      .sort()
      .reverse()
      .slice(0, 5);
    const sections = [];
    for (const file of files) {
      const text = await readTextIfExists(path.join(memoryRoot, file), 30_000);
      if (text) {
        sections.push(`### memory/${file}\n${tailLines(text, 60)}`);
      }
    }
    return sections.join("\n\n").slice(0, 10_000);
  } catch {
    return "";
  }
}

async function readDigestSummary(vaultRoot) {
  const text = await readTextIfExists(path.join(vaultRoot, ".vault/cache/agent-digest.json"), 10_000_000);
  if (!text) {
    return "";
  }
  try {
    const digest = JSON.parse(text);
    const pages = Array.isArray(digest.pages) ? digest.pages : [];
    const recent = pages
      .slice()
      .sort((a, b) => String(b.discovered_on || b.published_on || "").localeCompare(String(a.discovered_on || a.published_on || "")))
      .slice(0, 30)
      .map((page) => ({
        path: page.path,
        title: page.title,
        type: page.type,
        summary: page.summary,
        url: page.url,
        discovered_on: page.discovered_on,
        published_on: page.published_on,
        deadline: page.deadline,
        priority: page.priority,
        tags: page.tags,
        topics: page.topics,
      }));
    return JSON.stringify(
      {
        generated_at: digest.generated_at,
        page_count: digest.page_count,
        recent_pages: recent,
      },
      null,
      2,
    );
  } catch {
    return trimForContext(text, 5000);
  }
}

function summarizeMarkdown(text) {
  const source = String(text || "");
  const parts = [];
  const frontmatter = extractFrontmatter(source);
  if (frontmatter) {
    parts.push(frontmatter);
  }
  const body = source.replace(/^---\n[\s\S]*?\n---\n/, "").trim();
  const selected = body
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("![]("))
    .slice(0, 35)
    .join("\n");
  if (selected) {
    parts.push(selected);
  }
  return trimForContext(parts.join("\n\n"), 3200);
}

function extractFrontmatter(text) {
  const source = String(text || "");
  if (!source.startsWith("---\n")) {
    return "";
  }
  const end = source.indexOf("\n---\n", 4);
  return end === -1 ? "" : source.slice(0, end + 5).trim();
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

function renderSection(relativePath, body) {
  return `### ${relativePath}\n${String(body || "").trim()}`;
}

function tailLines(text, count) {
  return String(text || "").split(/\r?\n/).filter(Boolean).slice(-count).join("\n");
}

function trimForContext(text, limit) {
  const clean = String(text || "").trim();
  if (clean.length <= limit) {
    return clean;
  }
  return `${clean.slice(0, limit).trim()}\n[truncated]`;
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack || error.message : String(error)}\n`);
  process.exit(1);
});
