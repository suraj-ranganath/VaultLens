#!/usr/bin/env node

import process from "node:process";
import { Codex } from "@openai/codex-sdk";

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

function buildPrompt(payload) {
  const { message, pendingCalendarRequest = null, recentCalendarHistory = [], timezone, currentDate } = payload;
  return `
You are a careful calendar planning agent for a personal Telegram assistant.

Your job is to decide whether this Telegram message is asking to create, update, delete, or clarify Google Calendar events.
The message may include text, image extraction, QR values, event flyers, job screenshots, or a previous pending calendar proposal.

Critical rules:
- Never mark an event ready to execute unless the user explicitly confirms a pending proposal or the current message already includes an explicit confirmation plus complete event details.
- If the user asks to save/add/schedule something to calendar, extract the best event proposal and ask for confirmation before execution.
- If any required detail is missing or ambiguous, ask a concise clarification question.
- Required details for timed events: title, start date, start time, end time or duration, timezone if ambiguous.
- Required details for all-day events: title, date or date range.
- For recurring classes or batches, use one event with an RRULE when appropriate, otherwise emit multiple concrete events.
- For updates like "modify the previous event", use pendingCalendarRequest first, then recentCalendarHistory.
- Use America/Los_Angeles as the default timezone unless the message clearly says otherwise.
- Event descriptions should include relevant source context and note that the event was created from Telegram.
- Return JSON only.

Current date: ${currentDate}
Default timezone: ${timezone}

Pending calendar request:
${JSON.stringify(pendingCalendarRequest, null, 2)}

Recent calendar history:
${JSON.stringify(recentCalendarHistory, null, 2)}

Incoming Telegram message:
${JSON.stringify(message, null, 2)}
`.trim();
}

const eventSchema = {
  type: "object",
  properties: {
    summary: { type: "string" },
    start: { type: "string" },
    end: { type: "string" },
    allDay: { type: "boolean" },
    timeZone: { type: "string" },
    location: { type: "string" },
    description: { type: "string" },
    recurrence: { type: "array", items: { type: "string" } },
    attendees: { type: "array", items: { type: "string" } },
  },
  required: ["summary", "start", "end", "allDay", "timeZone", "location", "description", "recurrence", "attendees"],
  additionalProperties: false,
};

const schema = {
  type: "object",
  properties: {
    calendarIntent: { type: "boolean" },
    operation: { type: "string", enum: ["none", "create", "update", "delete", "cancel"] },
    targetEventId: { type: ["string", "null"] },
    targetCalendarId: { type: "string" },
    needsClarification: { type: "boolean" },
    clarificationQuestion: { type: "string" },
    needsConfirmation: { type: "boolean" },
    userConfirmed: { type: "boolean" },
    confirmationText: { type: "string" },
    acknowledgement: { type: "string" },
    events: { type: "array", items: eventSchema },
    confidence: { type: "string", enum: ["high", "medium", "low"] },
    reasoningSummary: { type: "string" },
  },
  required: [
    "calendarIntent",
    "operation",
    "targetEventId",
    "targetCalendarId",
    "needsClarification",
    "clarificationQuestion",
    "needsConfirmation",
    "userConfirmed",
    "confirmationText",
    "acknowledgement",
    "events",
    "confidence",
    "reasoningSummary",
  ],
  additionalProperties: false,
};

async function main() {
  const payload = parseJson(await readStdin());
  const apiKey = (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  if (!apiKey) {
    throw new Error("Missing OPENAI_API_KEY or CODEX_API_KEY for Codex SDK");
  }

  const codex = new Codex({ apiKey });
  const thread = codex.startThread({
    model: payload.model || "gpt-5.4",
    workingDirectory: payload.workingDirectory || process.cwd(),
    skipGitRepoCheck: true,
    approvalPolicy: "never",
    sandboxMode: "read-only",
    networkAccessEnabled: false,
    webSearchEnabled: false,
    modelReasoningEffort: payload.reasoningEffort || "medium",
  });

  const turn = await thread.run(buildPrompt(payload), { outputSchema: schema });
  const plan = extractJson(turn.finalResponse);
  process.stdout.write(JSON.stringify({ threadId: thread.id, plan, finalResponse: turn.finalResponse }, null, 2) + "\n");
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack || error.message : String(error)}\n`);
  process.exit(1);
});
