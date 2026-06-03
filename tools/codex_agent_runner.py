#!/usr/bin/env python3
"""Shared Codex Python SDK runner for VaultLens agent turns.

This is the only place that should call the Codex SDK. It intentionally uses
Codex account auth (local auth cache or CODEX_ACCESS_TOKEN), not OpenAI API
keys, so model turns are billed against Codex/ChatGPT limits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from openai_codex import ApprovalMode, Codex, LocalImageInput, Sandbox, TextInput, retry_on_overload
    from openai_codex.client import CodexConfig
    from openai_codex.types import Personality, ReasoningEffort, ReasoningSummary
except ModuleNotFoundError as exc:  # pragma: no cover - CI intentionally skips the live Codex extra.
    ApprovalMode = Codex = CodexConfig = LocalImageInput = Personality = ReasoningEffort = ReasoningSummary = Sandbox = TextInput = None  # type: ignore[assignment]
    CODEX_SDK_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def retry_on_overload(fn):  # type: ignore[no-redef]
        return fn()

else:
    CODEX_SDK_IMPORT_ERROR = None


REASONING_RANK = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
DEFAULT_MODEL = os.environ.get("VAULT_CODEX_MODEL") or os.environ.get("VAULT_AGENT_MODEL") or "auto"
DEFAULT_SANDBOX = "full_access"
SANDBOX_ALIASES = {
    "readonly": "read_only",
    "read-only": "read_only",
    "read_only": "read_only",
    "ro": "read_only",
    "workspace": "workspace_write",
    "workspace-write": "workspace_write",
    "workspace_write": "workspace_write",
    "write": "workspace_write",
    "full": "full_access",
    "full-access": "full_access",
    "full_access": "full_access",
    "danger-full-access": "full_access",
    "danger_full_access": "full_access",
}
GENERIC_TOOL_GAP_RE = re.compile(
    r"\b(shell|file|tool|dashboard)\b.*\b(unavailable|not available|failed|could not|not accessible)|"
    r"\bguaranteed vault context pack\b|\bfull dashboard scan\b|\bvault is inaccessible\b",
    re.IGNORECASE,
)


ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "storeInVault": {"type": "boolean"},
        "sendAck": {"type": "boolean"},
        "acknowledgement": {"type": "string"},
        "classification": {
            "type": "string",
            "enum": [
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
        "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "containsOperatorInstruction": {"type": "boolean"},
        "summary": {"type": "string"},
        "instructionSummary": {"type": "string"},
        "reasonsToKeep": {"type": "array", "items": {"type": "string"}},
        "artifactPreference": {"type": "string", "enum": ["none", "web_clip", "screenshot", "pdf", "paste_text"]},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": [
                            "append_message_to_stream",
                            "run_vault_ingest",
                            "rebuild_artifact_capture_queue",
                            "refresh_live_metadata_jobs_recent",
                            "refresh_live_metadata_knowledge_all",
                            "refresh_live_metadata_current_links",
                            "agentic_vault_work",
                            "answer_vault_query",
                            "update_task_ledger",
                        ],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["tool", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
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
    "additionalProperties": False,
}


VAULT_WORK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "telegram_summary": {"type": "string"},
        "durable_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "change_type": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["path", "change_type", "reason"],
                "additionalProperties": False,
            },
        },
        "sources_considered": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "kind": {"type": "string"},
                    "used_for": {"type": "string"},
                },
                "required": ["source", "kind", "used_for"],
                "additionalProperties": False,
            },
        },
        "unresolved_items": {"type": "array", "items": {"type": "string"}},
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "telegram_summary", "durable_changes", "sources_considered", "unresolved_items", "next_actions"],
    "additionalProperties": False,
}


EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "start": {"type": "string"},
        "end": {"type": "string"},
        "allDay": {"type": "boolean"},
        "timeZone": {"type": "string"},
        "location": {"type": "string"},
        "description": {"type": "string"},
        "recurrence": {"type": "array", "items": {"type": "string"}},
        "attendees": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "start", "end", "allDay", "timeZone", "location", "description", "recurrence", "attendees"],
    "additionalProperties": False,
}


CALENDAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "calendarIntent": {"type": "boolean"},
        "operation": {"type": "string", "enum": ["none", "create", "update", "delete", "cancel"]},
        "targetEventId": {"type": ["string", "null"]},
        "targetCalendarId": {"type": "string"},
        "needsClarification": {"type": "boolean"},
        "clarificationQuestion": {"type": "string"},
        "needsConfirmation": {"type": "boolean"},
        "userConfirmed": {"type": "boolean"},
        "confirmationText": {"type": "string"},
        "acknowledgement": {"type": "string"},
        "events": {"type": "array", "items": EVENT_SCHEMA},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reasoningSummary": {"type": "string"},
    },
    "required": [
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
    "additionalProperties": False,
}


QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer_markdown": {"type": "string"},
        "concise_answer": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "note_type": {"type": "string"},
                    "relevance": {"type": "string"},
                    "source_url": {"type": ["string", "null"]},
                },
                "required": ["path", "title", "note_type", "relevance", "source_url"],
                "additionalProperties": False,
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "follow_up_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer_markdown", "concise_answer", "confidence", "citations", "gaps", "follow_up_questions"],
    "additionalProperties": False,
}


BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_send": {"type": "boolean"},
        "telegram_text": {"type": "string"},
        "selected_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                    "source": {"type": ["string", "null"]},
                    "why_today": {"type": "string"},
                    "suggested_next_step": {"type": "string"},
                },
                "required": ["title", "kind", "date", "source", "why_today", "suggested_next_step"],
                "additionalProperties": False,
            },
        },
        "recommended_reading": {
            "type": ["object", "null"],
            "properties": {
                "title": {"type": "string"},
                "source": {"type": ["string", "null"]},
                "why_this_matters": {"type": "string"},
            },
            "required": ["title", "source", "why_this_matters"],
            "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": ["should_send", "telegram_text", "selected_actions", "recommended_reading", "rationale"],
    "additionalProperties": False,
}


DREAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "durable_facts": {"type": "array", "items": {"type": "string"}},
        "preferences": {"type": "array", "items": {"type": "string"}},
        "decisions_or_systems": {"type": "array", "items": {"type": "string"}},
        "task_implications": {"type": "array", "items": {"type": "string"}},
        "topic_updates": {"type": "array", "items": {"type": "string"}},
        "should_promote": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "durable_facts",
        "preferences",
        "decisions_or_systems",
        "task_implications",
        "topic_updates",
        "should_promote",
        "summary",
    ],
    "additionalProperties": False,
}


VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "extracted_text": {"type": "string"},
        "urls": {"type": "array", "items": {"type": "string"}},
        "qr_values": {"type": "array", "items": {"type": "string"}},
        "event_clues": {"type": "array", "items": {"type": "string"}},
        "job_clues": {"type": "array", "items": {"type": "string"}},
        "reminder_clues": {"type": "array", "items": {"type": "string"}},
        "needs_manual_review": {"type": "boolean"},
    },
    "required": ["summary", "extracted_text", "urls", "qr_values", "event_clues", "job_clues", "reminder_clues", "needs_manual_review"],
    "additionalProperties": False,
}


def read_stdin_json() -> dict[str, Any]:
    text = sys.stdin.read().strip()
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RuntimeError("Expected stdin JSON object")
    return parsed


def write_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def write_event(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def extract_json(text: str | None) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        raise RuntimeError("Codex returned an empty response")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}\s*$", value)
        if not match:
            raise RuntimeError(f"Codex did not return JSON: {value[:400]}")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("Codex JSON response was not an object")
    return parsed


def model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [model_dump(item) for item in value]
    if isinstance(value, dict):
        return {str(key): model_dump(item) for key, item in value.items()}
    return value


def normalize_usage(usage: Any) -> dict[str, Any] | None:
    dumped = model_dump(usage)
    if not isinstance(dumped, dict):
        return None
    return dumped


def billing(usage: Any) -> dict[str, Any]:
    return {
        "billingMode": "codex_subscription",
        "note": "Usage is charged against Codex/ChatGPT limits, not OpenAI API credits.",
        "usage": normalize_usage(usage),
    }


def reasoning_effort(value: str | None, default: str) -> ReasoningEffort:
    require_codex_sdk()
    clean = str(value or default).strip().lower()
    if clean not in REASONING_RANK:
        clean = default
    return ReasoningEffort(clean)


def reasoning_summary(value: str = "concise") -> ReasoningSummary:
    require_codex_sdk()
    return ReasoningSummary.model_validate(value)


def sandbox_name_from_payload(payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    raw = (
        payload.get("sandbox")
        or payload.get("sandboxMode")
        or os.environ.get("VAULT_CODEX_SANDBOX")
        or os.environ.get("VAULT_AGENT_SANDBOX")
        or DEFAULT_SANDBOX
    )
    clean = str(raw or "").strip().lower().replace(" ", "_")
    return SANDBOX_ALIASES.get(clean, DEFAULT_SANDBOX)


def codex_sandbox(payload: dict[str, Any] | None = None) -> Sandbox:
    require_codex_sdk()
    return getattr(Sandbox, sandbox_name_from_payload(payload))


def codex_approval_mode() -> ApprovalMode:
    require_codex_sdk()
    raw = str(os.environ.get("VAULT_CODEX_APPROVAL_MODE", "deny_all")).strip().lower().replace("-", "_")
    if raw == "auto_review":
        return ApprovalMode.auto_review
    return ApprovalMode.deny_all


def require_codex_sdk() -> None:
    if CODEX_SDK_IMPORT_ERROR is None:
        return
    raise RuntimeError(
        "The Codex Python SDK is not installed in this environment. "
        "For local live agent runs, use `uv sync --extra codex` or install `openai-codex`. "
        "CI and deterministic tests intentionally do not install the live Codex extra."
    ) from CODEX_SDK_IMPORT_ERROR


def codex_client() -> Codex:
    require_codex_sdk()
    codex_bin = str(os.environ.get("VAULT_CODEX_BIN") or os.environ.get("CODEX_BIN") or "").strip()
    config_overrides = tuple(
        item.strip()
        for item in re.split(r"[\n,]", os.environ.get("VAULT_CODEX_CONFIG_OVERRIDES", ""))
        if item.strip()
    )
    config_kwargs = {
        "config_overrides": config_overrides,
        "client_name": "vault_lens",
        "client_title": "VaultLens",
    }
    if codex_bin:
        return Codex(CodexConfig(codex_bin=codex_bin, **config_kwargs))
    if config_overrides:
        return Codex(CodexConfig(**config_kwargs))
    return Codex()


def pick_model(codex: Codex, requested: str | None = None) -> str:
    value = str(requested or DEFAULT_MODEL or "auto").strip()
    if value and value.lower() != "auto":
        return value
    fallback = os.environ.get("VAULT_CODEX_FALLBACK_MODEL", "gpt-5.5").strip() or "gpt-5.5"
    try:
        models = codex.models(include_hidden=False)
        visible = [item for item in models.data if not getattr(item, "hidden", False)]
        known = {str(getattr(item, "id", "")) for item in visible} | {str(getattr(item, "model", "")) for item in visible}
        top = [item for item in visible if not (getattr(item, "upgrade", None) and str(getattr(item, "upgrade")) in known)]
        candidates = top or visible
        if candidates:
            return str(max(candidates, key=lambda item: (str(getattr(item, "model", "")), str(getattr(item, "id", "")))).model)
    except Exception:
        pass
    return fallback


def thread_for(codex: Codex, payload: dict[str, Any], *, model: str, effort: str, include_web_search: bool = False):
    vault_root = str(Path(payload.get("workingDirectory") or os.getcwd()).resolve())
    sandbox = codex_sandbox(payload)
    config = {
        "model_reasoning_effort": effort,
        "web_search": "live" if include_web_search else "disabled",
    }
    common = {
        "model": model,
        "cwd": vault_root,
        "config": config,
        "approval_mode": codex_approval_mode(),
        "sandbox": sandbox,
        "personality": Personality.pragmatic,
        "developer_instructions": thread_developer_instructions(
            include_web_search=include_web_search,
            sandbox_name=sandbox.name,
        ),
    }
    thread_id = str(payload.get("threadId") or "").strip()
    if thread_id:
        return codex.thread_resume(thread_id, **common)
    return codex.thread_start(**common)


def thread_developer_instructions(*, include_web_search: bool, sandbox_name: str | None = None) -> str:
    web_search_policy = "enabled when it materially improves filing, enrichment, or answering" if include_web_search else "disabled"
    sandbox_policy = sandbox_name or DEFAULT_SANDBOX
    return f"""
You are the VaultLens retrieval and filing agent.

Harness policy:
- Work inside the configured vault cwd.
- Current sandbox profile: {sandbox_policy}.
- You have full autonomy inside the restored vault working tree: read files, write files, run shell commands, call existing scripts, create helper scripts, and use multi-step workflows.
- Prefer efficient commands such as rg, rg --files, sed, targeted file reads, small Python/Node helpers, and existing repo scripts, but do not stop there when deeper work is needed.
- You may write canonical vault notes, topics, dashboards, outputs, traces, task state, cache records, browser artifact records, templates, indexes, profile/person context, decisions, systems, and memory-review artifacts when that creates durable value.
- You may adapt the vault organization itself when a better retrieval structure is warranted; update indexes/backlinks/dashboards/templates so future agents can understand the change.
- Treat user-provided raw sources and import logs as immutable evidence. Add new artifacts instead of rewriting or deleting raw inputs.
- External side effects outside the restored vault are committed by deterministic VaultLens code: prepare data freely, but do not directly send Telegram messages, mutate Google Calendar, or upload S3 state unless that deterministic tool path is explicitly invoked by the caller.
- For links, follow redirects and nested links when needed. For X/LinkedIn/dynamic pages, use lightweight adapters, HTTP, browser artifacts, and/or enqueue/browser-enrich as appropriate instead of giving up early.
- Web search is {web_search_policy}; use it as a complement to vault memory, not as a replacement for it.
- If a tool path is unavailable, continue from supplied context and citations; do not expose generic tool availability caveats to the user.
""".strip()


def run_structured(
    *,
    payload: dict[str, Any],
    prompt: str,
    schema: dict[str, Any],
    default_effort: str,
    include_web_search: bool = False,
    images: list[str] | None = None,
) -> tuple[Any, dict[str, Any], str, str]:
    effort_value = str(payload.get("reasoningEffort") or default_effort).strip().lower()
    with codex_client() as codex:
        selected_model = pick_model(codex, payload.get("model"))
        thread = thread_for(codex, payload, model=selected_model, effort=effort_value, include_web_search=include_web_search)
        input_value: str | list[Any]
        if images:
            input_value = [TextInput(prompt)] + [LocalImageInput(str(Path(image).resolve())) for image in images]
        else:
            input_value = prompt
        result = retry_on_overload(
            lambda: thread.run(
                input_value,
                effort=reasoning_effort(effort_value, default_effort),
                output_schema=schema,
                sandbox=codex_sandbox(payload),
                personality=Personality.pragmatic,
                summary=reasoning_summary("concise"),
            )
        )
        return result, extract_json(result.final_response), selected_model, thread.id


def normalize_item(item: Any) -> dict[str, Any] | None:
    data = model_dump(getattr(item, "root", item))
    if not isinstance(data, dict):
        return None
    kind = str(data.get("type") or data.get("kind") or "")
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", kind).lower()
    if snake == "user_message":
        text_length = 0
        for part in data.get("content") or []:
            if isinstance(part, dict):
                text_length += len(str(part.get("text") or ""))
        return {"type": "user_message", "text": "[redacted prompt/context]", "input_chars": text_length}
    if snake == "agent_message":
        return {"type": "agent_message", "text": str(data.get("text") or "")}
    if snake in {"reasoning", "reasoning_summary"}:
        return {"type": "reasoning", "text": str(data.get("text") or data.get("summary") or data.get("content") or "")}
    if snake in {"command_execution", "command_exec", "local_shell_command"}:
        return {
            "type": "command_execution",
            "command": str(data.get("command") or data.get("cmd") or ""),
            "status": str(data.get("status") or ""),
            "exit_code": data.get("exit_code") or data.get("exitCode"),
            "output_preview": str(data.get("output") or data.get("stdout") or "")[:1200],
        }
    if "web" in snake and "search" in snake:
        return {"type": "web_search", "query": str(data.get("query") or data.get("url") or "")}
    if "tool" in snake:
        return {
            "type": "mcp_tool_call",
            "server": str(data.get("server") or ""),
            "tool": str(data.get("tool") or data.get("name") or ""),
            "status": str(data.get("status") or ""),
            "arguments_preview": json.dumps(data.get("arguments") or data.get("input") or {}, ensure_ascii=False)[:1200],
        }
    if snake:
        return {"type": snake, "raw": data}
    return None


def summarize_trace(items: Any) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for item in items or []:
        normalized = normalize_item(item)
        if normalized:
            trace.append(normalized)
    return trace


def hydrate_answer(vault_root: Path, answer: dict[str, Any]) -> dict[str, Any]:
    citations = []
    root = vault_root.resolve()
    for citation in answer.get("citations") or []:
        if not isinstance(citation, dict):
            continue
        path = str(citation.get("path") or "").strip()
        if path:
            try:
                candidate = Path(path)
                if candidate.is_absolute():
                    resolved = candidate.resolve()
                    if str(resolved).startswith(str(root)):
                        path = str(resolved.relative_to(root))
                else:
                    path = str(candidate)
            except OSError:
                pass
        source_url = str(citation.get("source_url") or "").strip() or read_note_url(vault_root, path)
        hydrated = dict(citation)
        hydrated["path"] = path
        hydrated["source_url"] = source_url or None
        hydrated["vault_url"] = f"/vault/{path}" if path else ""
        citations.append(hydrated)
    hydrated_answer = dict(answer)
    hydrated_answer["citations"] = citations
    hydrated_answer["gaps"] = sanitize_gaps(hydrated_answer)
    return hydrated_answer


def sanitize_gaps(answer: dict[str, Any]) -> list[str]:
    gaps = [str(gap).strip() for gap in answer.get("gaps") or [] if str(gap).strip()]
    has_evidence = bool(answer.get("citations")) or bool(str(answer.get("answer_markdown") or answer.get("concise_answer") or "").strip())
    if not has_evidence:
        return gaps
    return [gap for gap in gaps if not GENERIC_TOOL_GAP_RE.search(gap)]


def read_note_url(vault_root: Path, relative_path: str) -> str:
    if not relative_path:
        return ""
    target = (vault_root / relative_path).resolve()
    if not str(target).startswith(str(vault_root.resolve())) or not target.exists():
        return ""
    try:
        text = target.read_text(encoding="utf-8", errors="replace")[:5000]
    except OSError:
        return ""
    match = re.search(r"(?m)^url:\s*(.+?)\s*$", text)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def context_from_helper(vault_root: Path, question: str, recent_context: str = "") -> str:
    helper = vault_root / "tools" / "telegram_vault_query.mjs"
    if not helper.exists():
        return ""
    env = os.environ.copy()
    env["VAULT_QUERY_CONTEXT_ONLY"] = "1"
    command = [os.environ.get("VAULT_JS_RUNTIME", "bun"), str(helper)]
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(
                {
                    "workingDirectory": str(vault_root),
                    "question": question,
                    "recentConversationContext": recent_context,
                }
            ),
            text=True,
            capture_output=True,
            cwd=vault_root,
            env=env,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            return recent_context
        parsed = json.loads(proc.stdout)
        return str(parsed.get("vaultContext") or "")
    except Exception:
        return recent_context


def build_router_prompt(payload: dict[str, Any]) -> str:
    return f"""
You are the first point of contact for a personal vault ingestion system.

Your job is to inspect one incoming Telegram message and decide how it should be handled.
The user may send links, X/Twitter posts, jobs, screenshots, photos, QR codes, direct vault questions, reminders, personal notes, or mixed messages with extra instructions.

Available local actions:
1. append_message_to_stream
2. run_vault_ingest
3. rebuild_artifact_capture_queue
4. refresh_live_metadata_jobs_recent
5. refresh_live_metadata_knowledge_all
6. refresh_live_metadata_current_links
7. agentic_vault_work
8. answer_vault_query
9. update_task_ledger

Rules:
- If the message should become part of the vault, set storeInVault=true and include append_message_to_stream plus run_vault_ingest.
- Use agentic_vault_work when the item deserves real filing/synthesis beyond basic ingest: links, jobs, articles, images, screenshots, personal facts, preferences, decisions, systems, reminders, events, or anything that should improve future personalization.
- agentic_vault_work is allowed to run shell/file/browser workflows and modify the vault; do not avoid it just because deterministic ingest can create a first draft.
- If the message is asking a question that should be answered from the vault, request answer_vault_query.
- Resolve follow-ups like "this", "that", "the link", "that screenshot", or "that role" from recent Telegram context.
- If the user says they completed/applied/read/cancelled/skipped/handled something, request update_task_ledger and treat it as authoritative.
- If the user marks something low/medium/high/critical priority, request update_task_ledger.
- Treat useful screenshots/photos/image notes as real vault content.
- For saved LinkedIn, X, article, job, event, and technical links, usually request refresh_live_metadata_current_links.
- Send short, natural acknowledgements. Use light emoji when useful, but avoid being cutesy for serious content.
- Return JSON only.

Operator instructions:
{json.dumps(payload.get("instructions") or {}, indent=2)}

Known chats:
{json.dumps(payload.get("knownChats") or {}, indent=2)}

Recent Telegram context:
{str(payload.get("recentConversationContext") or "").strip() or "(No recent context supplied.)"}

Incoming message:
{json.dumps(payload.get("message") or {}, indent=2)}
""".strip()


def build_vault_work_prompt(payload: dict[str, Any]) -> str:
    return f"""
You are the full-power VaultLens filing and enrichment agent.

This is not a classifier turn. Actually do the work before returning JSON.

Mission:
- Inspect the current Telegram message, attachments, recent conversation context, and existing vault.
- Decide the best durable representation for what the user sent or implied.
- Create or update the right markdown notes, topics, projects, decisions, systems, memories, dashboards, outputs, backlinks, and artifact references.
- Preserve context about Suraj: preferences, recurring interests, relationships, taste, priorities, decisions, operating systems, and task state when the message reveals durable signal.
- Improve the vault's retrieval structure if the current taxonomy is insufficient. It is acceptable to add or update templates, indexes, topic pages, dashboards, memory-review artifacts, and canonical note organization when that helps future agents.

Tool freedom:
- Use shell/file tools freely inside the vault cwd.
- Use rg/rg --files, targeted reads, Python/Node/Bun helpers, existing repo scripts, HTTP fetches, and browser-oriented tools when available.
- Follow redirects, nested links, quoted posts, X cards, LinkedIn/job-board redirects, QR-derived links, and article/source links when needed.
- If browser work is needed but not available in this turn, create or update browser-enrichment queue/artifact notes clearly enough for the browser worker to finish the job.

Quality rules:
- Treat raw imports and raw source artifacts as immutable evidence; add new artifacts instead of rewriting user-provided raw data.
- Do not invent dates, credentials, facts about people, deadlines, or relationship details.
- Record uncertainty explicitly in frontmatter/body.
- Prefer canonical notes over loose summaries. Link notes together so future agents can crawl quickly.
- Rebuild dashboards/cache if you materially change canonical notes and the scripts are available.
- Do not directly send Telegram messages, mutate Google Calendar, or upload S3. The caller will handle those external side effects after this turn.

Return JSON only after completing the vault work.

Current date: {payload.get("currentDate") or datetime.now().date().isoformat()}
Default timezone: {payload.get("timezone") or os.environ.get("TZ") or "America/Los_Angeles"}

Agent routing decision:
{json.dumps(payload.get("decision") or {}, indent=2)}

Incoming Telegram message:
{json.dumps(payload.get("message") or {}, indent=2)}

Recent Telegram context:
{str(payload.get("recentConversationContext") or "").strip() or "(No recent context supplied.)"}
""".strip()


def build_calendar_prompt(payload: dict[str, Any]) -> str:
    return f"""
You are a careful calendar planning agent for a personal Telegram assistant.

Decide whether this Telegram message is asking to create, update, delete, cancel, or clarify Google Calendar events.

Critical rules:
- Never mark an event ready to execute unless the user explicitly confirms a pending proposal or the current message already includes explicit confirmation plus complete event details.
- If the user asks to save/add/schedule something to calendar, extract the best proposal and ask for confirmation before execution.
- If required details are missing or ambiguous, ask a concise clarification question.
- Required timed-event details: title, start date, start time, end time or duration, timezone if ambiguous.
- Required all-day details: title and date/date range.
- For recurring classes or batches, use one RRULE when appropriate, otherwise emit multiple concrete events.
- For updates like "modify the previous event", use pendingCalendarRequest first, then recentCalendarHistory.
- For update/delete operations, copy the exact event_id into targetEventId; if you cannot identify it, ask clarification.
- Use America/Los_Angeles as the default timezone unless the message clearly says otherwise.
- Event descriptions should include relevant source context and say the event was created from Telegram.
- Return JSON only.

Current date: {payload.get("currentDate")}
Default timezone: {payload.get("timezone")}
Default target calendar ID: {payload.get("targetCalendarId") or "primary"}

Pending calendar request:
{json.dumps(payload.get("pendingCalendarRequest"), indent=2)}

Recent calendar history:
{json.dumps(payload.get("recentCalendarHistory") or [], indent=2)}

Incoming Telegram message:
{json.dumps(payload.get("message") or {}, indent=2)}
""".strip()


def build_query_prompt(question: str, include_web_search: bool, vault_context: str) -> str:
    return f"""
You are answering questions against a local-first personal vault.

Primary objective:
- answer the user's question from the vault accurately and efficiently
- use the guaranteed context pack before broad file scans
- if shell/file tools fail, do not claim the vault is inaccessible when guaranteed context has relevant evidence

Search discipline:
1. Read AGENTS.md if you need the vault contract.
2. Check hot.md first when it exists.
3. Resolve Telegram follow-up references from recent Telegram context before broad search.
4. Use rg and rg --files before opening files.
5. Prefer canonical notes under items/, topics/, projects/, outputs/, and dashboards/.
6. You may modify files when the question creates durable value: save a useful answer under outputs/, improve a topic/project page, update memory-review artifacts, or add backlinks. Do not force a write for throwaway questions.

Answering rules:
- treat the local vault as source of truth
- web search is {"allowed only when the vault is insufficient and external context materially helps" if include_web_search else "disabled for this turn"}
- cite vault files in the structured citations array
- in answer_markdown, put inline markdown citations next to supported claims
- user-facing citations should prefer external primary source URLs when available
- only fall back to /vault/... note URLs when no external source exists
- never use Obsidian wiki links in answer_markdown
- never list generic tool, shell, file-search, or dashboard-scan availability as a user-facing gap
- if the guaranteed context pack is sufficient, answer from it without caveating that broader search was not needed or not attempted
- only put substantive unknowns in gaps, such as missing facts about a person, unclear dates, weak source evidence, or unavailable primary context
- be direct, helpful, warm, and concise
- return JSON only

Guaranteed vault context pack:
{vault_context or "(No preloaded vault context was found.)"}

User question:
{question}
""".strip()


def build_brief_prompt(payload: dict[str, Any], context_pack: str) -> str:
    return f"""
You are Suraj's morning brief agent.

Goal:
- Create a smart, personalized, high-signal morning Telegram brief for {payload.get("today")}.
- The deterministic lists are only a shortlist. You decide what actually matters.
- Use profile, active systems, interests, decisions, reminders, jobs, saved links, recent curiosity, and today's Google Calendar events.

Selection principles:
- Include only genuinely urgent, high-impact, or intentionally saved follow-up items.
- Prioritize deadlines/reminders within the next week, event logistics, job/opportunity items where applying early matters, and explicit user promises.
- Include important Google Calendar events today; omit filler/low-signal holds.
- Treat explicit high/critical priority as a strong signal.
- Include at most one recommended reading.
- Send no brief if there is no meaningful signal.
- Keep telegram_text compact, direct, warm, and ready to send as plain Telegram text.
- Use raw source URLs when available.
- Return JSON only.

Candidate action shortlist:
{json.dumps(payload.get("candidateActions") or [], indent=2)}

Candidate reading shortlist:
{json.dumps(payload.get("candidateReadings") or [], indent=2)}

Candidate Google Calendar events for today:
{json.dumps(payload.get("candidateCalendarEvents") or [], indent=2) if payload.get("candidateCalendarEvents") else (f"Calendar unavailable: {payload.get('calendarError')}" if payload.get("calendarError") else "[]")}

Guaranteed vault/profile context:
{context_pack or "(No additional context found.)"}
""".strip()


def build_dream_prompt(context: str) -> str:
    return f"""
You are the vault dreaming/promoter pass.

Goal:
- Review recent session memory, task state, and key vault context.
- Promote only durable user understanding: preferences, decisions, systems, recurring interests, and task implications.
- Do not invent facts.
- Do not promote one-off noise.
- Keep output compact and human-reviewable.

The output is a reviewable artifact, not hidden memory.

Context:
{context}

Return JSON only.
""".strip()


def build_vision_prompt(payload: dict[str, Any]) -> str:
    return f"""
Analyze this Telegram attachment for a personal knowledge vault.
Return strict JSON with keys: summary, extracted_text, urls, qr_values, event_clues, job_clues, reminder_clues, needs_manual_review.

The Telegram caption/context is user intent. Let it decide what details matter most.
- If the caption asks to create/save/add/schedule/modify a calendar event, prioritize title, exact dates, times, timezone clues, recurrence, location, organizer, registration URLs, and ambiguity.
- If this looks like a job post/application screenshot, extract role/company/status/date clues.
- If this looks like an event screenshot/flyer, extract event/date/time/location/registration clues.
- If there is a QR code, decode it if possible.
- If visible text is useful, include it in extracted_text.
- Do not invent details.

Telegram message context:
{str(payload.get("originalText") or payload.get("original_text") or "").strip() or "(no caption or text supplied)"}
""".strip()


def text_if_exists(path: Path, max_bytes: int = 80_000) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:max_bytes]
    except OSError:
        return ""


def trim(text: str, limit: int) -> str:
    clean = str(text or "").strip()
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "\n[truncated]"


def build_morning_context(vault_root: Path, payload: dict[str, Any]) -> str:
    sections: list[str] = []
    for rel in ["hot.md", "index.md", "dashboards/urgent.md", "dashboards/reading-queue.md", "log.md"]:
        text = text_if_exists(vault_root / rel, 120_000)
        if text:
            sections.append(f"### {rel}\n{trim(text, 3200 if rel != 'log.md' else 1800)}")
    for item in list(payload.get("candidateActions") or []) + list(payload.get("candidateReadings") or []):
        rel = str((item or {}).get("path") or "").strip()
        if rel:
            text = text_if_exists(vault_root / rel, 80_000)
            if text:
                sections.append(f"### {rel}\n{trim(text, 3000)}")
    telegram = text_if_exists(vault_root / "imports/telegram-inbox/telegram-live.txt", 120_000)
    if telegram:
        sections.append("### imports/telegram-inbox/telegram-live.txt\n" + "\n".join(telegram.splitlines()[-80:]))
    return "\n\n".join(sections)[:42_000]


def build_dream_context(vault_root: Path) -> str:
    sections: list[str] = []
    for rel in ["hot.md", "index.md", "dashboards/tasks.md", "dashboards/urgent.md", ".vault/tasks/tasks.json"]:
        text = text_if_exists(vault_root / rel, 80_000)
        if text:
            sections.append(f"### {rel}\n{trim(text, 5000)}")
    memory_root = vault_root / "memory"
    try:
        for name in sorted(memory_root.iterdir(), reverse=True)[:7]:
            if name.name.endswith(".md"):
                text = text_if_exists(name, 60_000)
                if text:
                    sections.append(f"### memory/{name.name}\n{trim(text[-5000:], 5000)}")
    except OSError:
        pass
    return "\n\n".join(sections)[:44_000]


def handle_health(_payload: dict[str, Any]) -> None:
    with codex_client() as codex:
        account = model_dump(codex.account())
        model = pick_model(codex, os.environ.get("VAULT_CODEX_MODEL", "auto"))
        write_json(
            {
                "ok": True,
                "auth": "codex",
                "billingMode": "codex_subscription",
                "model": model,
                "sandbox": sandbox_name_from_payload(_payload),
                "account": account,
            }
        )


def handle_models(_payload: dict[str, Any]) -> None:
    with codex_client() as codex:
        models = codex.models(include_hidden=True)
        write_json({"ok": True, "models": model_dump(models)})


def handle_router(payload: dict[str, Any]) -> None:
    result, decision, model, thread_id = run_structured(
        payload=payload,
        prompt=build_router_prompt(payload),
        schema=ROUTER_SCHEMA,
        default_effort=os.environ.get("VAULT_ROUTER_REASONING_EFFORT", "low"),
        include_web_search=bool(payload.get("includeWebSearch", os.environ.get("VAULT_ROUTER_WEB_SEARCH_ENABLED", "true").lower() != "false")),
    )
    write_json({"threadId": thread_id, "decision": decision, "finalResponse": result.final_response, "usage": normalize_usage(result.usage), "billing": billing(result.usage), "model": model})


def handle_vault_work(payload: dict[str, Any]) -> None:
    result, work, model, thread_id = run_structured(
        payload=payload,
        prompt=build_vault_work_prompt(payload),
        schema=VAULT_WORK_SCHEMA,
        default_effort=os.environ.get("VAULT_WORK_REASONING_EFFORT", "medium"),
        include_web_search=bool(payload.get("includeWebSearch", True)),
    )
    write_json(
        {
            "threadId": thread_id,
            "work": work,
            "finalResponse": result.final_response,
            "usage": normalize_usage(result.usage),
            "billing": billing(result.usage),
            "model": model,
            "sandbox": sandbox_name_from_payload(payload),
        }
    )


def handle_calendar(payload: dict[str, Any]) -> None:
    result, plan, model, thread_id = run_structured(
        payload=payload,
        prompt=build_calendar_prompt(payload),
        schema=CALENDAR_SCHEMA,
        default_effort=os.environ.get("VAULT_CALENDAR_REASONING_EFFORT", "medium"),
        include_web_search=False,
    )
    write_json({"threadId": thread_id, "plan": plan, "finalResponse": result.final_response, "usage": normalize_usage(result.usage), "billing": billing(result.usage), "model": model})


def handle_query(payload: dict[str, Any], *, stream: bool = False) -> None:
    vault_root = Path(payload.get("workingDirectory") or os.getcwd()).resolve()
    question = str(payload.get("question") or "").strip()
    if not question:
        raise RuntimeError("Missing question")
    include_web_search = bool(payload.get("includeWebSearch", True))
    recent_context = str(payload.get("recentConversationContext") or "")
    vault_context = str(payload.get("vaultContext") or "") or context_from_helper(vault_root, question, recent_context)
    prompt = build_query_prompt(question, include_web_search, vault_context)
    effort_value = str(payload.get("reasoningEffort") or os.environ.get("VAULT_QUERY_REASONING_EFFORT", "medium")).strip().lower()
    started_at = time.time()

    with codex_client() as codex:
        selected_model = pick_model(codex, payload.get("model"))
        sandbox = codex_sandbox(payload)
        thread = thread_for(codex, payload, model=selected_model, effort=effort_value, include_web_search=include_web_search)
        if stream:
            turn = thread.turn(
                prompt,
                effort=reasoning_effort(effort_value, "medium"),
                output_schema=QUERY_SCHEMA,
                sandbox=sandbox,
                personality=Personality.pragmatic,
                summary=reasoning_summary("concise"),
            )
            completed_items: list[Any] = []
            final_response = ""
            usage = None
            sent_thread_event = False
            for event in turn.stream():
                method = getattr(event, "method", "")
                payload_obj = getattr(event, "payload", None)
                if method == "turn/started":
                    if not sent_thread_event:
                        write_event({"type": "thread.started", "threadId": thread.id or str(getattr(payload_obj, "thread_id", "") or "")})
                        sent_thread_event = True
                    write_event({"type": "turn.started"})
                elif method == "turn/completed":
                    usage = normalize_usage(getattr(getattr(payload_obj, "turn", None), "usage", None) or getattr(payload_obj, "usage", None))
                    write_event({"type": "turn.completed", "usage": usage})
                elif method == "thread/tokenUsage/updated":
                    usage = normalize_usage(
                        getattr(payload_obj, "usage", None)
                        or getattr(payload_obj, "token_usage", None)
                        or getattr(payload_obj, "tokenUsage", None)
                        or payload_obj
                    )
                    write_event({"type": "thread.token_usage.updated", "usage": usage})
                elif method == "turn/failed":
                    error = getattr(getattr(payload_obj, "error", None), "message", "") or "Turn failed"
                    write_event({"type": "turn.failed", "error": error})
                elif method == "item/agentMessage/delta":
                    delta = str(getattr(payload_obj, "delta", "") or "")
                    if delta:
                        write_event({"type": "item", "phase": "updated", "item": {"type": "agent_message", "text": delta}})
                elif method == "item/completed":
                    item = getattr(payload_obj, "item", None)
                    completed_items.append(item)
                    normalized = normalize_item(getattr(item, "root", item))
                    if normalized:
                        if normalized.get("type") == "agent_message":
                            final_response = str(normalized.get("text") or final_response)
                        write_event({"type": "item", "phase": "completed", "item": normalized})
                elif method == "turn/plan/updated":
                    write_event({"type": "item", "phase": "updated", "item": {"type": "reasoning", "text": json.dumps(model_dump(payload_obj), ensure_ascii=False)[:1200]}})
            if not final_response:
                raise RuntimeError("Codex did not return a final structured response.")
            answer = hydrate_answer(vault_root, extract_json(final_response))
            write_event(
                {
                    "type": "result",
                    "threadId": thread.id,
                    "answer": answer,
                    "trace": summarize_trace(completed_items),
                    "usage": usage,
                    "billing": {"billingMode": "codex_subscription", "usage": usage},
                    "meta": {
                        "model": selected_model,
                        "reasoningEffort": effort_value,
                        "sandbox": sandbox.name,
                        "includeWebSearch": include_web_search,
                        "vaultContextBytes": len(vault_context.encode("utf-8")),
                        "durationMs": int((time.time() - started_at) * 1000),
                    },
                }
            )
            return

        result = retry_on_overload(
            lambda: thread.run(
                prompt,
                effort=reasoning_effort(effort_value, "medium"),
                output_schema=QUERY_SCHEMA,
                sandbox=sandbox,
                personality=Personality.pragmatic,
                summary=reasoning_summary("concise"),
            )
        )
        answer = hydrate_answer(vault_root, extract_json(result.final_response))
        write_json(
            {
                "threadId": thread.id,
                "answer": answer,
                "usage": normalize_usage(result.usage),
                "billing": billing(result.usage),
                "trace": summarize_trace(result.items),
                "meta": {
                    "model": selected_model,
                    "reasoningEffort": effort_value,
                    "sandbox": sandbox.name,
                    "includeWebSearch": include_web_search,
                    "vaultContextBytes": len(vault_context.encode("utf-8")),
                    "durationMs": int((time.time() - started_at) * 1000),
                },
            }
        )


def handle_morning_brief(payload: dict[str, Any]) -> None:
    vault_root = Path(payload.get("workingDirectory") or os.getcwd()).resolve()
    context = str(payload.get("contextPack") or "") or build_morning_context(vault_root, payload)
    result, brief, model, thread_id = run_structured(
        payload=payload,
        prompt=build_brief_prompt(payload, context),
        schema=BRIEF_SCHEMA,
        default_effort=os.environ.get("VAULT_MORNING_BRIEF_REASONING_EFFORT", "medium"),
        include_web_search=False,
    )
    write_json({**brief, "thread_id": thread_id, "usage": normalize_usage(result.usage), "billing": billing(result.usage), "model": model, "context_bytes": len(context.encode("utf-8"))})


def handle_dream(payload: dict[str, Any]) -> None:
    if payload.get("mockResult"):
        write_json(payload["mockResult"])
        return
    vault_root = Path(payload.get("workingDirectory") or os.getcwd()).resolve()
    context = str(payload.get("context") or "") or build_dream_context(vault_root)
    result, dream, model, thread_id = run_structured(
        payload=payload,
        prompt=build_dream_prompt(context),
        schema=DREAM_SCHEMA,
        default_effort=os.environ.get("VAULT_DREAM_REASONING_EFFORT", "low"),
        include_web_search=False,
    )
    write_json({**dream, "thread_id": thread_id, "usage": normalize_usage(result.usage), "billing": billing(result.usage), "model": model})


def handle_vision(payload: dict[str, Any]) -> None:
    image_path = str(payload.get("artifactPath") or payload.get("artifact_path") or "").strip()
    if not image_path:
        raise RuntimeError("Missing artifactPath")
    path = Path(image_path)
    if not path.is_absolute():
        path = Path(payload.get("workingDirectory") or os.getcwd()).resolve() / path
    if not path.exists():
        raise RuntimeError(f"Attachment image does not exist: {path}")
    result, analysis, model, thread_id = run_structured(
        payload=payload,
        prompt=build_vision_prompt(payload),
        schema=VISION_SCHEMA,
        default_effort=os.environ.get("VAULT_VISION_REASONING_EFFORT", "low"),
        include_web_search=False,
        images=[str(path)],
    )
    write_json({**analysis, "thread_id": thread_id, "usage": normalize_usage(result.usage), "billing": billing(result.usage), "model": model})


HANDLERS = {
    "health": handle_health,
    "models": handle_models,
    "telegram-router": handle_router,
    "vault-work": handle_vault_work,
    "calendar-plan": handle_calendar,
    "vault-query": lambda payload: handle_query(payload, stream=False),
    "vault-query-stream": lambda payload: handle_query(payload, stream=True),
    "morning-brief": handle_morning_brief,
    "dream": handle_dream,
    "vision-attachment": handle_vision,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="VaultLens Codex Python SDK runner")
    parser.add_argument("command", choices=sorted(HANDLERS))
    args = parser.parse_args()
    payload = read_stdin_json()
    HANDLERS[args.command](payload)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        sys.exit(1)
