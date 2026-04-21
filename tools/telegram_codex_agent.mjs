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
  const { message, knownChats = {}, instructions = {} } = payload;
  return `
You are the first point of contact for a personal vault ingestion system.

Your job is to inspect one incoming Telegram message and decide how it should be handled.
The user may send:
- links to technical articles
- X/Twitter links
- job opportunities
- reminders or personal notes
- mixed messages that include a link plus extra instructions
- messages that tell the system how to remember or prioritize something

You must decide the handling plan. Do not assume every message is just a generic link save.

Available local actions you may request:
1. append_message_to_stream
2. run_vault_ingest
3. rebuild_artifact_capture_queue
4. refresh_live_metadata_jobs_recent
5. refresh_live_metadata_knowledge_all

Rules:
- If the message should become part of the vault, set storeInVault=true.
- If storeInVault=true, include append_message_to_stream and run_vault_ingest in actions.
- Only request live metadata refresh when it is clearly worth the extra work.
- Only request rebuild_artifact_capture_queue when the message likely introduces a weak or blocked link that will need supporting artifacts later.
- Preserve user-added context or instructions in your reasoning and summaries.
- Prefer concise, high-signal action plans.
- Return JSON only.

Operator instructions:
${JSON.stringify(instructions, null, 2)}

Known chats:
${JSON.stringify(knownChats, null, 2)}

Incoming message:
${JSON.stringify(message, null, 2)}
`.trim();
}

const schema = {
  type: "object",
  properties: {
    storeInVault: { type: "boolean" },
    sendAck: { type: "boolean" },
    acknowledgement: { type: "string" },
    classification: {
      type: "string",
      enum: [
        "job_opportunity",
        "technical_article",
        "social_link",
        "thought_or_note",
        "reminder",
        "resource",
        "event",
        "mixed",
        "ignore",
      ],
    },
    priority: { type: "string", enum: ["low", "medium", "high", "critical"] },
    containsOperatorInstruction: { type: "boolean" },
    summary: { type: "string" },
    instructionSummary: { type: "string" },
    reasonsToKeep: {
      type: "array",
      items: { type: "string" },
    },
    artifactPreference: {
      type: "string",
      enum: ["none", "web_clip", "screenshot", "pdf", "paste_text"],
    },
    actions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          tool: {
            type: "string",
            enum: [
              "append_message_to_stream",
              "run_vault_ingest",
              "rebuild_artifact_capture_queue",
              "refresh_live_metadata_jobs_recent",
              "refresh_live_metadata_knowledge_all",
            ],
          },
          reason: { type: "string" },
        },
        required: ["tool", "reason"],
        additionalProperties: false,
      },
    },
  },
  required: [
    "storeInVault",
    "sendAck",
    "acknowledgement",
    "classification",
    "priority",
    "containsOperatorInstruction",
    "summary",
    "instructionSummary",
    "reasonsToKeep",
    "artifactPreference",
    "actions",
  ],
  additionalProperties: false,
};

async function main() {
  const raw = await readStdin();
  const payload = parseJson(raw);

  const apiKey = (process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  if (!apiKey) {
    throw new Error("Missing OPENAI_API_KEY or CODEX_API_KEY for Codex SDK");
  }

  const model = payload.model || "gpt-5.4";
  const threadOptions = {
    model,
    workingDirectory: payload.workingDirectory || process.cwd(),
    skipGitRepoCheck: true,
    approvalPolicy: "never",
    sandboxMode: "danger-full-access",
    networkAccessEnabled: true,
    webSearchEnabled: true,
    modelReasoningEffort: payload.reasoningEffort || "medium",
    ...(payload.additionalDirectories ? { additionalDirectories: payload.additionalDirectories } : {}),
  };

  const codex = new Codex({
    apiKey,
  });

  const thread = payload.threadId
    ? codex.resumeThread(payload.threadId, threadOptions)
    : codex.startThread(threadOptions);

  const turn = await thread.run(buildPrompt(payload), { outputSchema: schema });
  const decision = extractJson(turn.finalResponse);

  process.stdout.write(
    JSON.stringify(
      {
        threadId: thread.id,
        decision,
        finalResponse: turn.finalResponse,
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
