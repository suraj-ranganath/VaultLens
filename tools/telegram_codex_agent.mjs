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
  const { message, knownChats = {}, instructions = {}, recentConversationContext = "" } = payload;
  return `
You are the first point of contact for a personal vault ingestion system.

Your job is to inspect one incoming Telegram message and decide how it should be handled.
The user may send:
- links to technical articles
- X/Twitter links
- job opportunities
- screenshots, photos, QR codes, or image-based notes
- direct questions that should be answered from the vault
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
6. refresh_live_metadata_current_links
7. answer_vault_query
8. update_task_ledger

Rules:
- If the message should become part of the vault, set storeInVault=true.
- If storeInVault=true, include append_message_to_stream and run_vault_ingest in actions.
- If the message is primarily asking a question that should be answered from the vault, request answer_vault_query.
- If the message asks a follow-up question about "this", "that", "it", "there", "the link", "that screenshot", "that role", or something sent a few turns ago, use the recent Telegram context below and request answer_vault_query.
- For pure questions, usually set storeInVault=false unless the message also asks to remember something.
- If the user says they completed, applied to, submitted, read, cancelled, skipped, or handled something, request update_task_ledger.
- Treat the user's completion statement as authoritative. Infer references like "there", "that one", and named companies from recent conversation context when possible. Do not ask for confirmation unless multiple tasks are genuinely ambiguous.
- If the user explicitly says something is low, medium, high, or critical priority, request update_task_ledger so the matching vault note/task can be reprioritized.
- If attachments are present, use their extracted context and saved artifact paths when deciding classification and importance.
- Treat screenshots, photos, and image-heavy messages with useful information as real vault content, not as low-value attachments.
- If an image contains technical notes, article screenshots, job details, event details, reminders, or generally useful reference information, usually store it in the vault.
- Only ignore attachment-heavy messages when they are genuinely trivial, accidental, or non-informative.
- Only request live metadata refresh when it is clearly worth the extra work.
- For URL-bearing messages that are saved to the vault, prefer refresh_live_metadata_current_links over broad refreshes.
- For LinkedIn profiles/posts, X posts, articles, job posts, event pages, and technical resources, usually request refresh_live_metadata_current_links after run_vault_ingest.
- Only request rebuild_artifact_capture_queue when the message likely introduces a weak or blocked link that will need supporting artifacts later.
- Preserve user-added context or instructions in your reasoning and summaries.
- Prefer concise, high-signal action plans.
- If sending an acknowledgement, make it short, natural, and a little personable. Prefer specific acknowledgements like "Saved this to the vault 🧠", "Got it — filed under jobs 💼", or "Saved, and I’ll keep this in mind 👍" over a bare thumbs-up.
- Do not be cutesy for serious or sensitive messages; warm and useful beats quirky.
- Return JSON only.

Operator instructions:
${JSON.stringify(instructions, null, 2)}

Known chats:
${JSON.stringify(knownChats, null, 2)}

Recent Telegram context:
${String(recentConversationContext || "").trim() || "(No recent context supplied.)"}

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
        "vault_query",
        "task_update",
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
              "refresh_live_metadata_current_links",
              "answer_vault_query",
              "update_task_ledger",
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
