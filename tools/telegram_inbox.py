#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import hashlib
import mimetypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from ingest_chat_export import ingest
except ModuleNotFoundError:
    from tools.ingest_chat_export import ingest

try:
    from vault_trajectory import append_trajectory_event
except ModuleNotFoundError:
    from tools.vault_trajectory import append_trajectory_event

try:
    from vault_events import append_event as append_vault_event
except ModuleNotFoundError:
    from tools.vault_events import append_event as append_vault_event

try:
    from telegram_delivery_queue import drain_telegram_delivery_queue, send_or_queue_telegram_message
except ModuleNotFoundError:
    from tools.telegram_delivery_queue import drain_telegram_delivery_queue, send_or_queue_telegram_message

try:
    from vault_session_memory import append_memory
except ModuleNotFoundError:
    from tools.vault_session_memory import append_memory

try:
    from vault_tasks import list_tasks as vault_list_tasks
    from vault_tasks import sync_from_vault as vault_sync_tasks
    from vault_tasks import update_note_by_path, update_task_by_id
except ModuleNotFoundError:
    from tools.vault_tasks import list_tasks as vault_list_tasks
    from tools.vault_tasks import sync_from_vault as vault_sync_tasks
    from tools.vault_tasks import update_note_by_path, update_task_by_id


CURRENT_DATE = date.today()
API_ROOT = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT_SECONDS = 50
DEFAULT_LOOP_INTERVAL_SECONDS = 3
DEFAULT_SESSION_NAME = "telegram-live"
STATE_FILE_NAME = ".telegram_state.json"
RAW_UPDATES_FILE_NAME = "telegram-updates.jsonl"
PROCESSED_UPDATES_FILE_NAME = ".telegram_processed_updates.jsonl"
AGENT_DECISIONS_FILE_NAME = "telegram-agent-decisions.jsonl"
CALENDAR_ACTIONS_FILE_NAME = "telegram-calendar-actions.jsonl"
AGENT_EVENTS_FILE_NAME = "agent-events.jsonl"
DEFAULT_AGENT_MODEL = "gpt-5.4"
ATTACHMENT_ANALYSIS_MAX_BYTES = 8 * 1024 * 1024
ATTACHMENT_ANALYSIS_TIMEOUT_SECONDS = 90
DEFAULT_CALENDAR_ID = os.environ.get("VAULT_CALENDAR_ID") or os.environ.get("VAULT_BRIEF_CALENDAR_ID") or "primary"
DEFAULT_TIMEZONE = "America/Los_Angeles"
URL_RE = re.compile(r"https?://\S+")
CALENDAR_INTENT_RE = re.compile(
    r"\b(calendar|cal|gcal|schedule|scheduled|save (?:this )?(?:event|class|meeting)|add (?:this )?(?:event|class|meeting)|"
    r"put (?:this )?on (?:my )?calendar|reschedule|move (?:the )?(?:event|class|meeting)|modify (?:the )?(?:event|class|meeting)|"
    r"update (?:the )?(?:event|class|meeting)|cancel (?:the )?(?:event|class|meeting)|delete (?:the )?(?:event|class|meeting))\b",
    re.IGNORECASE,
)
CALENDAR_CONFIRM_RE = re.compile(
    r"^\s*(yes|yes please|yes pls|yep|yeah|yup|confirm|confirmed|looks good|correct|save it|do it|add it|please save it)\s*[.!]?\s*$",
    re.IGNORECASE,
)
CALENDAR_CANCEL_RE = re.compile(r"^\s*(no|nope|cancel|stop|never mind|nevermind|discard|don't|do not)\s*[.!]?\s*$", re.IGNORECASE)
TASK_COMPLETION_RE = re.compile(
    r"\b(done|did it|finished|completed|submitted|sent|applied|applied to|applied there|have applied|i applied|"
    r"read it|handled|took care|cancelled|canceled|skipped|not doing|closed|resolved)\b",
    re.IGNORECASE,
)
TASK_PRIORITY_RE = re.compile(
    r"\b(low|medium|high|critical)\s+priority\b|\bprioriti[sz]e\s+(?:this|that|it|there)?\s*(high|critical)\b",
    re.IGNORECASE,
)
FOLLOWUP_REFERENCE_RE = re.compile(
    r"\b(this|that|it|there|link|article|post|tweet|x post|screenshot|image|photo|picture|role|job|event|"
    r"previous|earlier|above|sent|one)\b",
    re.IGNORECASE,
)
QUESTION_INTENT_RE = re.compile(
    r"\?|^\s*(what|why|how|when|where|who|which|can|could|should|would|is|are|do|does|did|"
    r"summari[sz]e|explain|compare|tell me|give me|thoughts|opinion|worth)\b",
    re.IGNORECASE,
)
COMMAND_RE = re.compile(r"^/(today|queue|status|trace)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
COMMAND_CALLBACK_PREFIX = "vault:"
COMMAND_CALLBACKS_FILE = Path(".vault") / "telegram-command-center" / "callbacks.json"
COMMAND_ITEM_LIMIT = 5


def js_runtime() -> str:
    return os.environ.get("VAULT_JS_RUNTIME", "bun")


def read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return fallback or {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return fallback or {}


def load_local_env(vault_root: Path) -> None:
    for candidate in [vault_root / ".env.local", vault_root / ".env"]:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def format_export_timestamp(dt: datetime) -> str:
    return dt.strftime("%m/%d/%y, %I:%M:%S %p")


def sender_name(message: dict[str, Any]) -> str:
    sender = message.get("from") or {}
    first = str(sender.get("first_name") or "").strip()
    last = str(sender.get("last_name") or "").strip()
    username = str(sender.get("username") or "").strip()
    full = " ".join(part for part in [first, last] if part).strip()
    if full:
        return full
    if username:
        return username
    chat = message.get("chat") or {}
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    return "Unknown"


def slugify_fragment(text: str, fallback: str = "file", max_len: int = 40) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len].strip("-") or fallback


def ext_from_name(name: str | None) -> str:
    if not name:
        return ""
    return Path(str(name)).suffix.lower()


def looks_like_image(file_name: str | None, mime_type: str | None) -> bool:
    mime = str(mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    suffix = ext_from_name(file_name)
    return suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}


def choose_attachment_extension(file_name: str | None, mime_type: str | None, file_path: str | None, fallback: str) -> str:
    for candidate in [file_name, file_path]:
        suffix = ext_from_name(candidate)
        if suffix:
            return suffix
    guessed = mimetypes.guess_extension(str(mime_type or "").split(";")[0].strip())
    if guessed:
        return guessed
    return fallback


def extract_message_text(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    if message.get("text"):
        chunks.append(str(message["text"]))
    if message.get("caption"):
        chunks.append(str(message["caption"]))

    attachment_notes: list[str] = []
    if message.get("photo"):
        attachment_notes.append("[photo]")
    if message.get("video"):
        attachment_notes.append("[video]")
    if message.get("voice"):
        attachment_notes.append("[voice]")
    if message.get("audio"):
        attachment_notes.append("[audio]")
    if message.get("sticker"):
        attachment_notes.append("[sticker]")
    if message.get("document"):
        document = message["document"] or {}
        file_name = document.get("file_name")
        if file_name:
            attachment_notes.append(f"[document: {file_name}]")
        else:
            attachment_notes.append("[document]")
    if message.get("location"):
        location = message["location"] or {}
        lat = location.get("latitude")
        lon = location.get("longitude")
        attachment_notes.append(f"[location: {lat}, {lon}]")
    if message.get("contact"):
        contact = message["contact"] or {}
        name = " ".join(str(contact.get(key) or "").strip() for key in ["first_name", "last_name"]).strip()
        attachment_notes.append(f"[contact: {name or 'shared contact'}]")

    if attachment_notes:
        chunks.append("\n".join(attachment_notes))

    return "\n".join(chunk.strip() for chunk in chunks if str(chunk).strip()).strip()


def collect_message_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if message.get("photo"):
        photo_sizes = message.get("photo") or []
        if photo_sizes:
            photo = photo_sizes[-1] or {}
            attachments.append(
                {
                    "kind": "photo",
                    "file_id": str(photo.get("file_id") or ""),
                    "file_unique_id": str(photo.get("file_unique_id") or ""),
                    "file_name": "",
                    "mime_type": "image/jpeg",
                    "width": photo.get("width"),
                    "height": photo.get("height"),
                    "file_size": photo.get("file_size"),
                    "is_image": True,
                }
            )

    if message.get("document"):
        document = message.get("document") or {}
        mime_type = str(document.get("mime_type") or "")
        file_name = str(document.get("file_name") or "")
        attachments.append(
            {
                "kind": "document",
                "file_id": str(document.get("file_id") or ""),
                "file_unique_id": str(document.get("file_unique_id") or ""),
                "file_name": file_name,
                "mime_type": mime_type,
                "file_size": document.get("file_size"),
                "is_image": looks_like_image(file_name, mime_type),
            }
        )

    return [attachment for attachment in attachments if attachment.get("file_id")]


def normalize_message(update: dict[str, Any]) -> dict[str, Any] | None:
    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
    )
    if not message:
        return None

    text = extract_message_text(message)
    if not text:
        return None

    timestamp = datetime.fromtimestamp(int(message["date"]))
    chat = message.get("chat") or {}
    sender = sender_name(message)

    return {
        "update_id": int(update["update_id"]),
        "chat_id": int(chat.get("id")),
        "chat_type": str(chat.get("type") or ""),
        "chat_title": str(chat.get("title") or ""),
        "sender": sender,
        "sender_username": str((message.get("from") or {}).get("username") or ""),
        "message_id": int(message.get("message_id")),
        "timestamp_iso": timestamp.isoformat(),
        "export_line": render_export_message(timestamp, sender, text),
        "raw_text": text,
    }


def telegram_file_url(token: str, file_path: str) -> str:
    return f"{API_ROOT}/file/bot{token}/{file_path.lstrip('/')}"


def telegram_get_file(token: str, file_id: str) -> dict[str, Any]:
    payload = telegram_api(token, "getFile", file_id=file_id)
    result = payload.get("result") or {}
    if not isinstance(result, dict) or not result.get("file_path"):
        raise RuntimeError(f"Telegram getFile did not return a file path for {file_id}")
    return result


def download_telegram_attachment(token: str, file_id: str) -> tuple[bytes, dict[str, Any]]:
    file_meta = telegram_get_file(token, file_id)
    response = requests.get(telegram_file_url(token, str(file_meta["file_path"])), timeout=ATTACHMENT_ANALYSIS_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.content, file_meta


def response_output_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("output_text") or "").strip()
    if text:
        return text
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()


def summarize_attachment_with_openai(
    *,
    openai_api_key: str,
    model: str,
    artifact_path: Path,
    mime_type: str,
    original_text: str,
) -> dict[str, Any]:
    content_bytes = artifact_path.read_bytes()
    if len(content_bytes) > ATTACHMENT_ANALYSIS_MAX_BYTES:
        return {
            "summary": f"Attachment saved to {artifact_path.name}, but it was too large for inline vision analysis.",
            "extracted_text": "",
            "urls": [],
            "qr_values": [],
            "event_clues": [],
            "job_clues": [],
            "reminder_clues": [],
            "needs_manual_review": True,
        }

    encoded = base64.b64encode(content_bytes).decode("ascii")
    prompt = (
        "Analyze this Telegram attachment for a personal knowledge vault.\n"
        "Return strict JSON with keys: summary, extracted_text, urls, qr_values, event_clues, job_clues, "
        "reminder_clues, needs_manual_review.\n"
        "Use short strings. event_clues, job_clues, reminder_clues must be arrays of short bullet-like strings.\n"
        "The Telegram message context is user intent, not background noise. Let it decide what details matter most.\n"
        "If the caption asks to create, save, add, schedule, or modify a calendar event, prioritize calendar extraction: "
        "title, exact dates, start/end times, timezone clues, recurrence rules, class schedules, location, organizer, "
        "registration/check-in URLs, and ambiguity that requires confirmation.\n"
        "If the caption asks about recurring classes, extract every date/time pattern and whether a single recurrence rule or "
        "multiple separate events is more appropriate.\n"
        "If the caption asks to remember, revisit, summarize, apply, or follow up, prioritize the fields needed for that task.\n"
        "If this looks like a screenshot of a job post or application status, extract role/company/status/date clues.\n"
        "If this looks like an event screenshot or flyer, extract event/date/time/location/registration clues.\n"
        "If there is a QR code, decode it if possible and include the resolved text or URL in qr_values.\n"
        "If there is visible text, place the useful text in extracted_text.\n"
        "Do not invent details."
    )
    context = str(original_text or "").strip()
    user_parts: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Telegram message context / user instruction:\n"
                f"{context or '(no caption or text supplied)'}\n\n"
                "Extract image details that best satisfy this instruction. If the instruction is calendar-related, make "
                "event_clues maximally useful for creating accurate calendar events."
            ),
        },
        {
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{encoded}",
        },
    ]
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": prompt}]},
                {"role": "user", "content": user_parts},
            ],
            "text": {"format": {"type": "json_object"}},
        },
        timeout=ATTACHMENT_ANALYSIS_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    text = response_output_text(payload)
    if not text:
        raise RuntimeError("OpenAI attachment analysis returned no text")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI attachment analysis did not return a JSON object")
    return parsed


def clean_attachment_list(value: Any, *, limit: int = 5, item_limit: int = 180) -> list[str]:
    cleaned: list[str] = []
    for item in value or []:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if not text:
            continue
        cleaned.append(text[:item_limit])
        if len(cleaned) >= limit:
            break
    return cleaned


def clean_attachment_text(value: Any, *, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def extract_message_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?)\"]}'>")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def save_attachment_bytes(
    *,
    vault_root: Path,
    normalized_message: dict[str, Any],
    attachment: dict[str, Any],
    file_meta: dict[str, Any],
    content: bytes,
) -> Path:
    is_image = bool(attachment.get("is_image"))
    base_dir = vault_root / "raw" / ("images" if is_image else "docs") / "telegram"
    base_dir.mkdir(parents=True, exist_ok=True)

    timestamp = normalized_message["timestamp_iso"][:10]
    stem_parts = [
        timestamp,
        "telegram",
        f"chat-{normalized_message['chat_id']}",
        f"msg-{normalized_message['message_id']}",
        attachment.get("kind") or "file",
    ]
    if attachment.get("file_name"):
        stem_parts.append(slugify_fragment(Path(str(attachment["file_name"])).stem, fallback="file"))
    extension = choose_attachment_extension(
        attachment.get("file_name"),
        attachment.get("mime_type"),
        file_meta.get("file_path"),
        ".jpg" if is_image else ".bin",
    )
    filename = " ".join(str(part) for part in stem_parts if str(part).strip()) + extension
    path = base_dir / filename
    if path.exists():
        unique_suffix = slugify_fragment(str(attachment.get("file_unique_id") or attachment.get("file_id") or "dup"), fallback="dup", max_len=16)
        path = base_dir / f"{path.stem} {unique_suffix}{path.suffix}"
    path.write_bytes(content)
    return path


def build_attachment_context(attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return ""
    lines = ["Attachment context:"]
    for index, attachment in enumerate(attachments, start=1):
        kind = str(attachment.get("kind") or "attachment")
        artifact_path = str(attachment.get("artifact_path") or "").strip()
        mime_type = str(attachment.get("mime_type") or "").strip()
        lines.append(f"- attachment_{index}: {kind}")
        if artifact_path:
            lines.append(f"  saved_artifact: {artifact_path}")
        if mime_type:
            lines.append(f"  mime_type: {mime_type}")
        summary = clean_attachment_text(attachment.get("summary"), limit=280)
        if summary:
            lines.append(f"  summary: {summary}")
        extracted_text = clean_attachment_text(attachment.get("extracted_text"), limit=700)
        if extracted_text:
            lines.append(f"  extracted_text: {extracted_text}")
        for label, key in [
            ("qr_values", "qr_values"),
            ("urls", "urls"),
            ("event_clues", "event_clues"),
            ("job_clues", "job_clues"),
            ("reminder_clues", "reminder_clues"),
        ]:
            values = clean_attachment_list(attachment.get(key))
            if values:
                lines.append(f"  {label}: {' | '.join(values)}")
        if attachment.get("analysis_error"):
            lines.append(f"  analysis_error: {attachment['analysis_error']}")
    return "\n".join(lines).strip()


def enrich_message_attachments(
    *,
    vault_root: Path,
    token: str,
    openai_api_key: str,
    attachment_model: str,
    update: dict[str, Any],
    normalized_message: dict[str, Any],
) -> dict[str, Any]:
    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or {}
    )
    attachments = collect_message_attachments(message)
    if not attachments:
        normalized_message["attachments"] = []
        return normalized_message

    enriched_attachments: list[dict[str, Any]] = []
    for attachment in attachments:
        enriched = dict(attachment)
        try:
            content, file_meta = download_telegram_attachment(token, str(attachment["file_id"]))
            artifact_path = save_attachment_bytes(
                vault_root=vault_root,
                normalized_message=normalized_message,
                attachment=attachment,
                file_meta=file_meta,
                content=content,
            )
            enriched["artifact_path"] = artifact_path.relative_to(vault_root).as_posix()
            if attachment.get("is_image"):
                mime_type = str(attachment.get("mime_type") or mimetypes.guess_type(artifact_path.name)[0] or "image/jpeg")
                analysis = summarize_attachment_with_openai(
                    openai_api_key=openai_api_key,
                    model=attachment_model,
                    artifact_path=artifact_path,
                    mime_type=mime_type,
                    original_text=normalized_message.get("raw_text") or "",
                )
                enriched["summary"] = clean_attachment_text(analysis.get("summary"), limit=320)
                enriched["extracted_text"] = clean_attachment_text(analysis.get("extracted_text"), limit=1200)
                enriched["urls"] = clean_attachment_list(analysis.get("urls"))
                enriched["qr_values"] = clean_attachment_list(analysis.get("qr_values"))
                enriched["event_clues"] = clean_attachment_list(analysis.get("event_clues"))
                enriched["job_clues"] = clean_attachment_list(analysis.get("job_clues"))
                enriched["reminder_clues"] = clean_attachment_list(analysis.get("reminder_clues"))
                enriched["needs_manual_review"] = bool(analysis.get("needs_manual_review"))
            else:
                enriched["summary"] = f"Non-image attachment saved to {artifact_path.name}."
        except Exception as exc:
            enriched["analysis_error"] = f"{type(exc).__name__}: {exc}"
        enriched_attachments.append(enriched)

    attachment_context = build_attachment_context(enriched_attachments)
    raw_text_parts = [str(normalized_message.get("raw_text") or "").strip(), attachment_context]
    raw_text = "\n\n".join(part for part in raw_text_parts if part)
    normalized_message["attachments"] = enriched_attachments
    normalized_message["raw_text"] = raw_text.strip()
    timestamp = datetime.fromisoformat(str(normalized_message["timestamp_iso"]))
    normalized_message["export_line"] = render_export_message(timestamp, str(normalized_message["sender"]), normalized_message["raw_text"])
    return normalized_message


def render_export_message(timestamp: datetime, sender: str, text: str) -> str:
    lines = text.splitlines() or [""]
    first = f"[{format_export_timestamp(timestamp)}] {sender}: {lines[0]}"
    if len(lines) == 1:
        return first
    return "\n".join([first] + lines[1:])


def telegram_api(token: str, method: str, **params: Any) -> dict[str, Any]:
    response = requests.get(f"{API_ROOT}/bot{token}/{method}", params=params, timeout=70)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {payload}")
    return payload


def telegram_send_message(
    token: str,
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return send_or_queue_telegram_message(
        vault_root=Path(os.environ.get("VAULT_ROOT", Path.cwd())).resolve(),
        token=token,
        chat_id=chat_id,
        text=text,
        reply_to_message_id=reply_to_message_id,
        reply_markup=reply_markup,
    )


def telegram_edit_message_text(token: str, chat_id: int, message_id: int, text: str) -> dict[str, Any]:
    return telegram_api(token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text)


def telegram_send_chat_action(token: str, chat_id: int, action: str = "typing") -> dict[str, Any]:
    return telegram_api(token, "sendChatAction", chat_id=chat_id, action=action)


def telegram_answer_callback_query(token: str, callback_query_id: str, text: str = "", show_alert: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": "true" if show_alert else "false"}
    if text:
        params["text"] = text[:180]
    return telegram_api(token, "answerCallbackQuery", **params)


class TelegramTypingHeartbeat:
    def __init__(self, token: str, chat_id: int, interval_seconds: float = 3.5) -> None:
        self.token = token
        self.chat_id = chat_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            telegram_send_chat_action(self.token, self.chat_id, "typing")
        except Exception:
            pass
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                telegram_send_chat_action(self.token, self.chat_id, "typing")
            except Exception:
                pass
            self._stop.wait(self.interval_seconds)


class TelegramPreviewMessage:
    def __init__(self, token: str, chat_id: int, reply_to_message_id: int | None = None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.message_id: int | None = None
        self.last_text = ""
        self.last_update = 0.0

    def update(self, text: str, *, force: bool = False) -> None:
        if os.environ.get("VAULT_TELEGRAM_PREVIEW", "1").strip() in {"0", "false", "False"}:
            return
        clean = str(text or "").strip()
        if not clean or clean == self.last_text:
            return
        now = time.time()
        if not force and self.message_id is not None and now - self.last_update < 1.0:
            return
        self.last_text = clean
        self.last_update = now
        try:
            if self.message_id is None:
                response = telegram_send_message(
                    self.token,
                    chat_id=self.chat_id,
                    text=clean,
                    reply_to_message_id=self.reply_to_message_id,
                )
                message = response.get("result") or {}
                if isinstance(message, dict) and isinstance(message.get("message_id"), int):
                    self.message_id = int(message["message_id"])
                return
            telegram_edit_message_text(self.token, self.chat_id, self.message_id, clean[:4000])
        except Exception:
            pass

    def finish(self, text: str) -> bool:
        clean = str(text or "").strip()
        if not clean or self.message_id is None:
            return False
        try:
            telegram_edit_message_text(self.token, self.chat_id, self.message_id, clean[:4000])
            self.last_text = clean[:4000]
            return True
        except Exception:
            return False


def telegram_send_long_message(token: str, chat_id: int, text: str, reply_to_message_id: int | None = None) -> list[dict[str, Any]]:
    chunks = split_message_chunks(text)
    responses: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        responses.append(
            telegram_send_message(
                token,
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=reply_to_message_id if index == 0 else None,
            )
        )
    return responses


def telegram_deliver_long_message(
    token: str,
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    preview: TelegramPreviewMessage | None = None,
) -> list[dict[str, Any]]:
    chunks = split_message_chunks(text)
    responses: list[dict[str, Any]] = []
    start_index = 0
    if preview is not None and chunks and preview.finish(chunks[0]):
        start_index = 1
    for index, chunk in enumerate(chunks[start_index:], start=start_index):
        responses.append(
            telegram_send_message(
                token,
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=reply_to_message_id if index == 0 else None,
            )
        )
    return responses


def get_updates(token: str, offset: int | None, poll_timeout: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "timeout": poll_timeout,
        "allowed_updates": json.dumps(["message", "edited_message", "channel_post", "edited_channel_post", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    payload = telegram_api(token, "getUpdates", **params)
    return list(payload.get("result") or [])


def callback_chat_id(update: dict[str, Any]) -> int | None:
    callback = update.get("callback_query") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    try:
        return int(chat.get("id"))
    except Exception:
        return None


def handle_vault_command(
    *,
    vault_root: Path,
    token: str,
    inbox_dir: Path,
    session_name: str,
    state: dict[str, Any],
    normalized_message: dict[str, Any],
) -> dict[str, Any] | None:
    raw_text = str(normalized_message.get("raw_text") or "").strip()
    match = COMMAND_RE.match(raw_text)
    if not match:
        return None

    command = match.group(1).lower()
    chat_id = int(normalized_message["chat_id"])
    if command == "today":
        text, reply_markup = render_today_command(vault_root)
    elif command == "queue":
        return send_queue_command_cards(
            vault_root=vault_root,
            token=token,
            chat_id=chat_id,
            reply_to_message_id=int(normalized_message["message_id"]),
        )
    elif command == "status":
        text, reply_markup = render_status_command(vault_root, inbox_dir=inbox_dir, session_name=session_name, state=state)
    elif command == "trace":
        text, reply_markup = render_trace_command(inbox_dir=inbox_dir, session_name=session_name, chat_id=chat_id)
    else:
        return None

    telegram_send_message(
        token,
        chat_id=chat_id,
        text=text,
        reply_to_message_id=int(normalized_message["message_id"]),
        reply_markup=reply_markup,
    )
    return {"status": "handled", "command": command, "answered": True, "acked": False}


def handle_vault_callback_update(
    *,
    vault_root: Path,
    token: str,
    update: dict[str, Any],
) -> dict[str, Any] | None:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    data = str(callback.get("data") or "")
    if not data.startswith(COMMAND_CALLBACK_PREFIX):
        return None
    callback_id = str(callback.get("id") or "")
    token_id = data[len(COMMAND_CALLBACK_PREFIX) :]
    payload = load_command_callback(vault_root, token_id)
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    try:
        chat_id = int(chat.get("id"))
    except Exception:
        return {"status": "ignored", "reason": "missing_chat_id"}
    reply_to_message_id = message.get("message_id") if isinstance(message.get("message_id"), int) else None

    if not payload:
        if callback_id:
            telegram_answer_callback_query(token, callback_id, "This button expired. Run /today or /queue again.", show_alert=True)
        return {"status": "expired", "answered": True, "acked": False}

    action = str(payload.get("action") or "")
    label = str(payload.get("label") or payload.get("title") or "item").strip()
    result: dict[str, Any]
    if callback_id:
        telegram_answer_callback_query(token, callback_id, "Working on it…")

    if action == "show_task":
        result = {"updated": False, "detail": True}
        text, reply_markup = render_task_detail_card(vault_root, payload)
        telegram_send_message(token, chat_id=chat_id, text=text[:3800], reply_to_message_id=reply_to_message_id, reply_markup=reply_markup)
        return {"status": "handled", "action": action, "answered": True, "acked": False, "result": result}
    if action == "show_note":
        result = {"updated": False, "detail": True}
        text, reply_markup = render_note_detail_card(vault_root, payload)
        telegram_send_message(token, chat_id=chat_id, text=text[:3800], reply_to_message_id=reply_to_message_id, reply_markup=reply_markup)
        return {"status": "handled", "action": action, "answered": True, "acked": False, "result": result}

    if action in {"task_done", "task_applied"}:
        result = update_task_by_id(
            vault_root,
            str(payload.get("task_id") or ""),
            status="done",
            source="telegram_callback",
            source_id=f"callback:{update.get('update_id')}",
        )
        if not result.get("updated") and payload.get("note_path"):
            result = update_note_by_path(
                vault_root,
                str(payload.get("note_path") or ""),
                status="done",
                source="telegram_callback",
                source_id=f"callback:{update.get('update_id')}",
            )
        run_vault_cache_compile_if_possible(vault_root)
        verb = "Marked applied" if action == "task_applied" else "Marked done"
        text = f"{verb}: {label}"
        if not result.get("updated") and result.get("reason") != "already_set":
            text = f"I could not update that item: {result.get('reason') or 'unknown error'}"
    elif action in {"task_high", "note_high"}:
        if payload.get("task_id"):
            result = update_task_by_id(
                vault_root,
                str(payload.get("task_id") or ""),
                priority="high",
                source="telegram_callback",
                source_id=f"callback:{update.get('update_id')}",
            )
        else:
            result = update_note_by_path(
                vault_root,
                str(payload.get("note_path") or ""),
                priority="high",
                source="telegram_callback",
                source_id=f"callback:{update.get('update_id')}",
            )
        run_vault_cache_compile_if_possible(vault_root)
        text = f"Prioritized: {label}\n\nThis means the vault will treat it as high priority when building /today, morning briefs, task surfacing, and future retrieval."
        if not result.get("updated") and result.get("reason") != "already_set":
            text = f"I could not reprioritize that item: {result.get('reason') or 'unknown error'}"
    elif action == "summarize_note":
        result = {"updated": False, "summary": True}
        text = render_note_summary(vault_root, str(payload.get("note_path") or ""))
    else:
        result = {"updated": False, "reason": "unsupported_action"}
        text = f"Unsupported action: {action}"

    telegram_send_message(token, chat_id=chat_id, text=text[:3800], reply_to_message_id=reply_to_message_id)
    return {"status": "handled", "action": action, "answered": True, "acked": False, "result": result}


def run_vault_cache_compile_if_possible(vault_root: Path) -> None:
    try:
        run_vault_cache_compile(vault_root)
    except Exception:
        pass


def render_today_command(vault_root: Path) -> tuple[str, dict[str, Any] | None]:
    try:
        vault_sync_tasks(vault_root)
    except Exception:
        pass
    tasks = rank_today_tasks(vault_root)
    reading = select_recommended_reading(vault_root)
    lines = ["Today’s command center", ""]
    if tasks:
        lines.append("Most urgent:")
        for index, task in enumerate(tasks, start=1):
            due = str(task.get("due_on") or "no explicit date")
            priority = str(task.get("priority") or "medium")
            lines.append(f"{index}. {task.get('title')} — {priority}, due: {due}")
            source = str(task.get("source_url") or task.get("note_path") or "")
            if source:
                lines.append(f"   {source}")
    else:
        lines.append("No urgent open task-ledger items surfaced right now.")

    if reading:
        lines.extend(["", "Recommended read:", f"- {reading['title']} ({reading.get('type') or 'item'})"])
        if reading.get("summary"):
            lines.append(f"  {truncate_context(str(reading['summary']), 220)}")
        if reading.get("url"):
            lines.append(f"  {reading['url']}")

    rows: list[list[dict[str, Any]]] = []
    for index, task in enumerate(tasks, start=1):
        rows.append([task_detail_button(vault_root, task, index)])
    if reading:
        rows.append([note_detail_button(vault_root, reading, len(tasks) + 1)])
    return "\n".join(lines), inline_keyboard(rows)


def render_queue_command(vault_root: Path) -> tuple[str, dict[str, Any] | None]:
    items = recent_queue_items(vault_root, limit=COMMAND_ITEM_LIMIT)
    lines = ["Latest saved queue", ""]
    if not items:
        return "No recent queue items found in the compiled vault cache yet.", None
    for index, item in enumerate(items, start=1):
        date_label = item.get("discovered_on") or item.get("published_on") or "unknown date"
        priority = item.get("priority") or "medium"
        lines.append(f"{index}. {item.get('title')} — {item.get('type')}, {priority}, added: {date_label}")
        if item.get("summary"):
            lines.append(f"   {truncate_context(str(item['summary']), 180)}")
        source = str(item.get("url") or item.get("path") or "")
        if source:
            lines.append(f"   {source}")
    rows = chunk_buttons([note_detail_button(vault_root, item, index) for index, item in enumerate(items, start=1)], size=2)
    return "\n".join(lines), inline_keyboard(rows)


def send_queue_command_cards(*, vault_root: Path, token: str, chat_id: int, reply_to_message_id: int) -> dict[str, Any]:
    items = recent_queue_items(vault_root, limit=COMMAND_ITEM_LIMIT)
    if not items:
        telegram_send_message(
            token,
            chat_id=chat_id,
            text="No recent queue items found in the compiled vault cache yet.",
            reply_to_message_id=reply_to_message_id,
        )
        return {"status": "handled", "command": "queue", "answered": True, "acked": False, "cards": 0}

    telegram_send_message(
        token,
        chat_id=chat_id,
        text=f"Latest saved queue: {len(items)} items. Each card has its own actions attached below it.",
        reply_to_message_id=reply_to_message_id,
    )
    for index, item in enumerate(items, start=1):
        telegram_send_message(
            token,
            chat_id=chat_id,
            text=render_queue_item_card(item, index),
            reply_markup=inline_keyboard(note_action_rows(vault_root, item, include_done=True)),
        )
    return {"status": "handled", "command": "queue", "answered": True, "acked": False, "cards": len(items)}


def render_queue_item_card(item: dict[str, Any], index: int) -> str:
    title = str(item.get("title") or "Untitled item")
    note_type = str(item.get("type") or "item")
    date_label = str(item.get("discovered_on") or item.get("published_on") or "unknown date")
    priority = str(item.get("priority") or "medium")
    lines = [f"{index}. {title}", f"{note_type} · {priority} priority · added {date_label}"]
    if item.get("summary"):
        lines.extend(["", truncate_context(str(item["summary"]), 700)])
    if item.get("why_saved"):
        lines.extend(["", f"Why saved: {truncate_context(str(item['why_saved']), 280)}"])
    if item.get("url"):
        lines.extend(["", f"Source: {item.get('url')}"])
    return "\n".join(lines)


def render_status_command(vault_root: Path, *, inbox_dir: Path, session_name: str, state: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    tasks = vault_list_tasks(vault_root, status="all", limit=500)
    open_tasks = [task for task in tasks if str(task.get("status") or "") == "open"]
    delivery_queue = vault_root / ".vault" / "telegram-delivery-queue"
    queued = len(list(delivery_queue.glob("*.json"))) if delivery_queue.exists() else 0
    digest = load_agent_digest(vault_root)
    decisions = load_jsonl(agent_decisions_path(inbox_dir, session_name))
    last_decision = decisions[-1] if decisions else {}
    lines = [
        "Vault bot status",
        f"- Last ingest: {state.get('last_ingest_at') or 'unknown'}",
        f"- Open tasks: {len(open_tasks)}",
        f"- Queued outbound messages: {queued}",
        f"- Digest pages: {digest.get('page_count') or 'unknown'}",
        f"- Digest generated: {digest.get('generated_at') or 'unknown'}",
    ]
    if last_decision:
        decision = last_decision.get("decision") or {}
        lines.append(f"- Last agent classification: {decision.get('classification') or 'unknown'}")
    return "\n".join(lines), None


def render_trace_command(*, inbox_dir: Path, session_name: str, chat_id: int) -> tuple[str, dict[str, Any] | None]:
    decisions = [
        record
        for record in load_jsonl(agent_decisions_path(inbox_dir, session_name))
        if int(record.get("chat_id") or 0) == int(chat_id)
    ]
    if not decisions:
        return "No recent Telegram agent trace found for this chat yet.", None
    lines = ["Recent agent trace", ""]
    for index, record in enumerate(reversed(decisions[-3:]), start=1):
        decision = record.get("decision") or {}
        tools = [str(action.get("tool") or "") for action in record.get("actions") or [] if isinstance(action, dict)]
        results = [str(result.get("tool") or "") + ":" + str(result.get("status") or "") for result in record.get("tool_results") or []]
        lines.append(f"{index}. update {record.get('update_id')} at {record.get('logged_at')}")
        lines.append(f"   classification: {decision.get('classification') or 'unknown'}")
        if decision.get("summary"):
            lines.append(f"   summary: {truncate_context(str(decision.get('summary')), 220)}")
        lines.append(f"   tools: {', '.join([tool for tool in tools if tool]) or 'none'}")
        if results:
            lines.append(f"   results: {', '.join(results[:6])}")
    return "\n".join(lines), None


def rank_today_tasks(vault_root: Path, limit: int = COMMAND_ITEM_LIMIT) -> list[dict[str, Any]]:
    tasks = vault_list_tasks(vault_root, status="open", limit=100)
    scored = [(today_task_score(task), task) for task in tasks]
    scored = [(score, task) for score, task in scored if score > 0]
    scored.sort(key=lambda item: (-item[0], str(item[1].get("due_on") or "9999-99-99"), str(item[1].get("title") or "")))
    return [task for _, task in scored[:limit]]


def today_task_score(task: dict[str, Any]) -> int:
    score = 0
    priority = str(task.get("priority") or "").lower()
    score += {"critical": 50, "high": 35, "medium": 8, "low": 0}.get(priority, 8)
    due = parse_date_value(task.get("due_on"))
    if due:
        days = (due - CURRENT_DATE).days
        if days < 0:
            score += 45
        elif days <= 7:
            score += 55 - (days * 5)
        elif days <= 14:
            score += 10
    if str(task.get("task_type") or "") == "apply":
        score += 12
    if str(task.get("task_type") or "") == "reminder":
        score += 12
    return score


def recent_queue_items(vault_root: Path, limit: int = COMMAND_ITEM_LIMIT) -> list[dict[str, Any]]:
    digest = load_agent_digest(vault_root)
    pages = digest.get("pages") or []
    items = [
        page
        for page in pages
        if isinstance(page, dict)
        and str(page.get("path") or "").startswith("items/")
        and str(page.get("type") or "") in {"article", "resource", "tweet", "job", "opportunity", "event", "reminder", "thought"}
        and str(page.get("status") or "").lower() not in {"done", "closed", "archived"}
    ]
    items.sort(key=lambda item: (str(item.get("discovered_on") or item.get("published_on") or ""), float(item.get("mtime") or 0.0)), reverse=True)
    return items[:limit]


def select_recommended_reading(vault_root: Path) -> dict[str, Any] | None:
    digest = load_agent_digest(vault_root)
    pages = digest.get("pages") or []
    candidates = [
        page
        for page in pages
        if isinstance(page, dict)
        and str(page.get("type") or "") in {"article", "resource", "tweet"}
        and str(page.get("status") or "").lower() not in {"done", "closed", "archived"}
    ]
    candidates.sort(key=lambda item: (reading_score(item), str(item.get("discovered_on") or item.get("published_on") or "")), reverse=True)
    return candidates[0] if candidates else None


def reading_score(item: dict[str, Any]) -> int:
    score = {"critical": 40, "high": 30, "medium": 12, "low": 4}.get(str(item.get("priority") or "").lower(), 8)
    if item.get("why_saved"):
        score += 8
    if item.get("summary"):
        score += 6
    discovered = parse_date_value(item.get("discovered_on") or item.get("published_on"))
    if discovered:
        age = (CURRENT_DATE - discovered).days
        if age <= 7:
            score += 20
        elif age <= 30:
            score += 10
    return score


def load_agent_digest(vault_root: Path) -> dict[str, Any]:
    path = vault_root / ".vault" / "cache" / "agent-digest.json"
    if not path.exists():
        run_vault_cache_compile_if_possible(vault_root)
    return read_json(path, fallback={})


def task_detail_button(vault_root: Path, task: dict[str, Any], index: int) -> dict[str, str]:
    title = str(task.get("title") or f"task {index}")
    return callback_button(
        vault_root,
        f"{index} · Details",
        {
            "action": "show_task",
            "task_id": task.get("id"),
            "note_path": task.get("note_path"),
            "label": title,
        },
    )


def note_detail_button(vault_root: Path, item: dict[str, Any], index: int) -> dict[str, str]:
    title = str(item.get("title") or f"item {index}")
    return callback_button(
        vault_root,
        f"{index} · Details",
        {
            "action": "show_note",
            "note_path": item.get("path") or item.get("note_path"),
            "label": title,
        },
    )


def task_action_rows(vault_root: Path, task: dict[str, Any], index: int | None = None) -> list[list[dict[str, Any]]]:
    title = str(task.get("title") or f"task {index}")
    action = "task_applied" if str(task.get("task_type") or "") == "apply" else "task_done"
    done_label = "📨 Mark applied" if action == "task_applied" else "✅ Mark done"
    row = [
        callback_button(vault_root, done_label, {"action": action, "task_id": task.get("id"), "note_path": task.get("note_path"), "label": title}),
        callback_button(vault_root, "⭐ Prioritize", {"action": "task_high", "task_id": task.get("id"), "note_path": task.get("note_path"), "label": title}),
    ]
    rows = [row]
    if task.get("source_url"):
        rows.append([{"text": "Open source", "url": str(task["source_url"])}])
    return rows


def note_action_rows(vault_root: Path, item: dict[str, Any], *, include_done: bool) -> list[list[dict[str, Any]]]:
    title = str(item.get("title") or "item")
    path = str(item.get("path") or item.get("note_path") or "")
    row = [callback_button(vault_root, "⭐ Prioritize", {"action": "note_high", "note_path": path, "label": title})]
    if include_done:
        done_label = "✅ Mark read/done" if str(item.get("type") or "") in {"article", "resource", "tweet"} else "✅ Mark done"
        row.append(callback_button(vault_root, done_label, {"action": "task_done", "note_path": path, "label": title}))
    rows = [row]
    if item.get("url"):
        rows.append([{"text": "Open source", "url": str(item["url"])}])
    return rows


def chunk_buttons(buttons: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [buttons[index : index + size] for index in range(0, len(buttons), max(1, size))]


def callback_button(vault_root: Path, text: str, payload: dict[str, Any]) -> dict[str, str]:
    token_id = register_command_callback(vault_root, payload)
    return {"text": text[:64], "callback_data": f"{COMMAND_CALLBACK_PREFIX}{token_id}"}


def inline_keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
    clean_rows = [[button for button in row if button] for row in rows if row]
    return {"inline_keyboard": clean_rows} if clean_rows else None


def command_callbacks_path(vault_root: Path) -> Path:
    return vault_root / COMMAND_CALLBACKS_FILE


def register_command_callback(vault_root: Path, payload: dict[str, Any]) -> str:
    store = read_json(command_callbacks_path(vault_root), fallback={"schema": "my-vault-telegram-command-callbacks-v1", "callbacks": {}})
    callbacks = store.setdefault("callbacks", {})
    prune_command_callbacks(callbacks)
    enriched = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        **{key: value for key, value in payload.items() if value not in (None, "", [], {})},
    }
    raw = json.dumps(enriched, sort_keys=True, ensure_ascii=False) + str(time.time_ns())
    token_id = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]
    callbacks[token_id] = enriched
    store["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(command_callbacks_path(vault_root), store)
    return token_id


def load_command_callback(vault_root: Path, token_id: str) -> dict[str, Any] | None:
    store = read_json(command_callbacks_path(vault_root), fallback={"callbacks": {}})
    callbacks = store.get("callbacks") or {}
    payload = callbacks.get(token_id)
    return payload if isinstance(payload, dict) else None


def prune_command_callbacks(callbacks: dict[str, Any], *, max_age_seconds: int = 7 * 24 * 3600, max_items: int = 500) -> None:
    now = datetime.now(timezone.utc).timestamp()
    for token_id, payload in list(callbacks.items()):
        if not isinstance(payload, dict):
            callbacks.pop(token_id, None)
            continue
        created = parse_iso_timestamp(str(payload.get("created_at") or ""))
        if created and now - created > max_age_seconds:
            callbacks.pop(token_id, None)
    if len(callbacks) > max_items:
        ordered = sorted(callbacks.items(), key=lambda item: str((item[1] or {}).get("created_at") or ""))
        for token_id, _ in ordered[: len(callbacks) - max_items]:
            callbacks.pop(token_id, None)


def render_task_detail_card(vault_root: Path, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    task_id = str(payload.get("task_id") or "")
    task = next((item for item in vault_list_tasks(vault_root, status="all", limit=500) if str(item.get("id") or "") == task_id), {})
    if not task and payload.get("note_path"):
        note = page_for_path(vault_root, str(payload.get("note_path") or ""))
        if note:
            text, markup = render_note_detail_card(vault_root, {"note_path": note.get("path"), "label": note.get("title")})
            return text, markup
    if not task:
        return "I could not find that task anymore. Run /today to refresh the buttons.", None

    lines = [str(task.get("title") or payload.get("label") or "Task")]
    details = [
        ("Type", task.get("task_type")),
        ("Priority", task.get("priority")),
        ("Due", task.get("due_on") or "no explicit date"),
        ("Status", task.get("status")),
    ]
    for label, value in details:
        if value:
            lines.append(f"{label}: {value}")
    source = str(task.get("source_url") or task.get("note_path") or "")
    if source:
        lines.extend(["", f"Source: {source}"])
    note_path = str(task.get("note_path") or "")
    if note_path:
        page = page_for_path(vault_root, note_path)
        if page.get("summary"):
            lines.extend(["", truncate_context(str(page["summary"]), 550)])
        if page.get("why_saved"):
            lines.append(f"Why saved: {truncate_context(str(page['why_saved']), 260)}")
    lines.extend(["", "Actions: mark it done/applied, or prioritize it so it surfaces more often."])
    return "\n".join(lines), inline_keyboard(task_action_rows(vault_root, task))


def render_note_detail_card(vault_root: Path, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    rel_path = str(payload.get("note_path") or "")
    page = page_for_path(vault_root, rel_path)
    if not page:
        return "I could not find that item anymore. Run /queue to refresh the buttons.", None

    note_type = str(page.get("type") or "item")
    lines = [str(page.get("title") or rel_path)]
    details = [
        ("Type", note_type),
        ("Priority", page.get("priority") or "medium"),
        ("Added", page.get("discovered_on") or "unknown"),
        ("Published", page.get("published_on")),
        ("Status", page.get("status")),
    ]
    for label, value in details:
        if value:
            lines.append(f"{label}: {value}")
    if page.get("summary"):
        lines.extend(["", truncate_context(str(page["summary"]), 750)])
    if page.get("why_saved"):
        lines.extend(["", f"Why saved: {truncate_context(str(page['why_saved']), 350)}"])
    if page.get("url"):
        lines.extend(["", f"Source: {page.get('url')}"])
    lines.append(f"Vault note: {rel_path}")
    lines.extend(["", "Actions: prioritize means set priority to high for surfacing; mark read/done closes it out of the active queue."])
    return "\n".join(lines), inline_keyboard(note_action_rows(vault_root, page, include_done=True))


def page_for_path(vault_root: Path, rel_path: str) -> dict[str, Any]:
    if not rel_path:
        return {}
    digest = load_agent_digest(vault_root)
    page = next((item for item in digest.get("pages") or [] if isinstance(item, dict) and item.get("path") == rel_path), {})
    if page:
        return page
    return read_note_summary_from_disk(vault_root, rel_path)


def render_note_summary(vault_root: Path, rel_path: str) -> str:
    page = page_for_path(vault_root, rel_path)
    if not page:
        return "I could not find that note anymore. Run /queue to refresh the buttons."
    lines = [str(page.get("title") or rel_path)]
    if page.get("type"):
        lines.append(f"Type: {page.get('type')}")
    if page.get("summary"):
        lines.extend(["", truncate_context(str(page.get("summary")), 900)])
    if page.get("why_saved"):
        lines.extend(["", f"Why saved: {truncate_context(str(page.get('why_saved')), 500)}"])
    if page.get("url"):
        lines.extend(["", f"Source: {page.get('url')}"])
    lines.append(f"Vault note: {rel_path}")
    return "\n".join(lines)


def read_note_summary_from_disk(vault_root: Path, rel_path: str) -> dict[str, Any]:
    if not rel_path or Path(rel_path).is_absolute():
        return {}
    path = (vault_root / rel_path).resolve()
    try:
        if not path.is_relative_to(vault_root.resolve()) or not path.exists() or path.suffix != ".md":
            return {}
    except AttributeError:  # pragma: no cover
        if not str(path).startswith(str(vault_root.resolve())) or not path.exists() or path.suffix != ".md":
            return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    return {"title": path.stem, "summary": truncate_context(body, 900), "path": rel_path}


def parse_date_value(value: Any) -> date | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_iso_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def load_state(inbox_dir: Path) -> dict[str, Any]:
    state_path = inbox_dir / STATE_FILE_NAME
    return read_json(
        state_path,
        fallback={
            "last_update_id": None,
            "known_chats": {},
            "last_ingest_at": "",
            "stream_file": "",
            "agent_threads": {},
            "query_threads": {},
            "calendar_pending": {},
            "calendar_history": [],
        },
    )


def save_state(inbox_dir: Path, state: dict[str, Any]) -> None:
    write_json(inbox_dir / STATE_FILE_NAME, state)


def ensure_directories(vault_root: Path) -> tuple[Path, Path]:
    inbox_dir = vault_root / "imports" / "telegram-inbox"
    raw_updates_dir = vault_root / "raw" / "telegram-updates"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    raw_updates_dir.mkdir(parents=True, exist_ok=True)
    return inbox_dir, raw_updates_dir


def parse_allowed_chat_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    allowed: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        allowed.add(int(part))
    return allowed


def stream_path(inbox_dir: Path, session_name: str) -> Path:
    return inbox_dir / f"{session_name}.txt"


def raw_updates_path(raw_updates_dir: Path, session_name: str) -> Path:
    return raw_updates_dir / f"{CURRENT_DATE.isoformat()} {session_name} {RAW_UPDATES_FILE_NAME}"


def processed_updates_path(inbox_dir: Path, session_name: str) -> Path:
    return inbox_dir / f"{session_name} {PROCESSED_UPDATES_FILE_NAME}"


def agent_decisions_path(inbox_dir: Path, session_name: str) -> Path:
    return inbox_dir / f"{session_name} {AGENT_DECISIONS_FILE_NAME}"


def calendar_actions_path(inbox_dir: Path, session_name: str) -> Path:
    return inbox_dir / f"{session_name} {CALENDAR_ACTIONS_FILE_NAME}"


def load_processed_update_ids(path: Path) -> set[int]:
    processed: set[int] = set()
    for record in load_jsonl(path):
        update_id = record.get("update_id")
        if isinstance(update_id, int):
            processed.add(update_id)
    return processed


def append_processed_update(path: Path, payload: dict[str, Any]) -> None:
    append_jsonl(path, [payload])


def ensure_action_order(
    actions: list[dict[str, str]],
    *,
    store_in_vault: bool,
    include_ingest: bool,
) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(tool: str, reason: str) -> None:
        if tool in seen:
            return
        ordered.append({"tool": tool, "reason": reason})
        seen.add(tool)

    if store_in_vault:
        add("append_message_to_stream", "Persist the message into the normalized Telegram stream.")
        if include_ingest:
            add("run_vault_ingest", "Make the new message searchable in the vault immediately.")

    for action in actions:
        tool = str(action.get("tool") or "").strip()
        reason = str(action.get("reason") or "").strip() or "Requested by Codex agent."
        if tool:
            add(tool, reason)

    return ordered


def normalize_agent_decision(decision: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(decision or {})
    store_in_vault = bool(normalized.get("storeInVault"))

    if store_in_vault:
        normalized["sendAck"] = True
        acknowledgement = str(normalized.get("acknowledgement") or "").strip()
        normalized["acknowledgement"] = acknowledgement or default_acknowledgement(normalized)
    else:
        normalized["sendAck"] = bool(normalized.get("sendAck", False))
        normalized["acknowledgement"] = str(normalized.get("acknowledgement") or "").strip()

    return normalized


def default_acknowledgement(decision: dict[str, Any]) -> str:
    classification = str(decision.get("classification") or "").strip()
    if classification == "job_opportunity":
        return "Saved this job lead 💼"
    if classification == "technical_article":
        return "Saved for the technical reading queue 🧠"
    if classification == "social_link":
        return "Saved the link, and I’ll keep the context attached 🔗"
    if classification == "event":
        return "Saved the event context 📅"
    if classification == "reminder":
        return "Saved the reminder ✅"
    if classification == "thought_or_note":
        return "Saved this to memory 🧠"
    if classification == "resource":
        return "Saved this resource 🔖"
    if classification == "mixed":
        return "Saved and filed the useful parts 🗂️"
    return "Saved to the vault 👍"


def has_calendar_pending(state: dict[str, Any], chat_id: int) -> bool:
    return bool((state.get("calendar_pending") or {}).get(str(chat_id)))


def should_run_calendar_agent(normalized_message: dict[str, Any], state: dict[str, Any]) -> bool:
    chat_id = int(normalized_message["chat_id"])
    if has_calendar_pending(state, chat_id):
        return True
    return bool(CALENDAR_INTENT_RE.search(str(normalized_message.get("raw_text") or "")))


def recent_calendar_history(state: dict[str, Any], chat_id: int, limit: int = 8) -> list[dict[str, Any]]:
    records = []
    for record in state.get("calendar_history") or []:
        if int(record.get("chat_id") or 0) == int(chat_id):
            records.append(record)
    return records[-limit:]


def clean_scalar_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def resolve_calendar_target_event(
    plan: dict[str, Any],
    pending_request: dict[str, Any] | None,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    operation = str(plan.get("operation") or "none").strip()
    if operation not in {"update", "delete"} or str(plan.get("targetEventId") or "").strip() or clean_scalar_list(plan.get("targetEventIds")):
        return plan

    resolved = dict(plan)
    if operation == "update":
        events = [event for event in plan.get("events") or [] if isinstance(event, dict)]
        if len(events) > 1:
            matched_ids = match_calendar_events_to_history(events, history)
            if len(matched_ids) == len(events):
                resolved["targetEventIds"] = matched_ids
                resolved["targetEventId"] = matched_ids[0]
                resolved["targetCalendarId"] = str(resolved.get("targetCalendarId") or DEFAULT_CALENDAR_ID)
                resolved["targetResolution"] = {
                    "source": "recent_calendar_history",
                    "matched_count": len(matched_ids),
                    "mode": "batch_summary_start_match",
                }
                return resolved

    pending_plan = (pending_request or {}).get("plan") if isinstance(pending_request, dict) else {}
    if isinstance(pending_plan, dict):
        pending_event_id = str(pending_plan.get("targetEventId") or "").strip()
        if pending_event_id:
            resolved["targetEventId"] = pending_event_id
            resolved["targetCalendarId"] = str(resolved.get("targetCalendarId") or pending_plan.get("targetCalendarId") or DEFAULT_CALENDAR_ID)
            resolved["targetResolution"] = {"source": "pending_calendar_request"}
            return resolved

    for record in reversed(history or []):
        event_id = str(record.get("event_id") or "").strip()
        if not event_id:
            continue
        resolved["targetEventId"] = event_id
        resolved["targetCalendarId"] = str(resolved.get("targetCalendarId") or record.get("calendar_id") or DEFAULT_CALENDAR_ID)
        resolved["targetResolution"] = {
            "source": "recent_calendar_history",
            "summary": str(record.get("summary") or ""),
            "start": record.get("start"),
            "operation": record.get("operation"),
        }
        return resolved

    return resolved


def calendar_target_missing(plan: dict[str, Any]) -> bool:
    return (
        str(plan.get("operation") or "none").strip() in {"update", "delete"}
        and not str(plan.get("targetEventId") or "").strip()
        and not clean_scalar_list(plan.get("targetEventIds"))
    )


def match_calendar_events_to_history(events: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[str]:
    matched: list[str] = []
    used: set[int] = set()
    history_records = list(enumerate(history or []))
    for event in events:
        match = best_calendar_history_match(event, history_records, used)
        if match is None:
            return []
        index, record = match
        event_id = str(record.get("event_id") or "").strip()
        if not event_id:
            return []
        used.add(index)
        matched.append(event_id)
    return matched


def best_calendar_history_match(
    event: dict[str, Any],
    history_records: list[tuple[int, dict[str, Any]]],
    used: set[int],
) -> tuple[int, dict[str, Any]] | None:
    event_title = normalize_calendar_match_text(event.get("summary"))
    event_start = normalize_calendar_start(event.get("start"))
    fallback: tuple[int, dict[str, Any]] | None = None
    for index, record in reversed(history_records):
        if index in used or not str(record.get("event_id") or "").strip():
            continue
        record_title = normalize_calendar_match_text(record.get("summary"))
        record_start = normalize_calendar_start(record.get("start"))
        title_matches = bool(event_title and record_title and (event_title == record_title or event_title in record_title or record_title in event_title))
        start_matches = bool(event_start and record_start and event_start == record_start)
        if title_matches and start_matches:
            return index, record
        if title_matches and fallback is None:
            fallback = (index, record)
    return fallback


def normalize_calendar_match_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower()).strip()


def normalize_calendar_start(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date") or ""
    text = str(value or "").strip()
    return text[:16] if len(text) >= 16 else text


def plan_calendar_target_clarification(plan: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    clarified = dict(plan)
    operation = str(plan.get("operation") or "update").strip()
    choices = []
    for record in reversed(history or []):
        event_id = str(record.get("event_id") or "").strip()
        if not event_id:
            continue
        summary = str(record.get("summary") or "Untitled event").strip()
        start = record.get("start") or ""
        choices.append(f"- {summary} ({start})")
        if len(choices) >= 4:
            break
    if choices:
        question = f"Which event should I {operation}? Recent events I can target:\n" + "\n".join(choices)
    else:
        question = f"Which calendar event should I {operation}? I do not have a saved event ID for the previous event."
    clarified["needsClarification"] = True
    clarified["clarificationQuestion"] = question
    clarified["needsConfirmation"] = False
    clarified["userConfirmed"] = False
    clarified["confidence"] = "low"
    return clarified


def invoke_calendar_agent(
    vault_root: Path,
    openai_api_key: str,
    message: dict[str, Any],
    pending_request: dict[str, Any] | None,
    history: list[dict[str, Any]],
    model: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    script_path = vault_root / "tools" / "telegram_calendar_agent.mjs"
    payload = {
        "model": model,
        "reasoningEffort": reasoning_effort,
        "workingDirectory": str(vault_root),
        "message": message,
        "pendingCalendarRequest": pending_request,
        "recentCalendarHistory": history,
        "timezone": os.environ.get("VAULT_DEFAULT_TIMEZONE", DEFAULT_TIMEZONE),
        "targetCalendarId": DEFAULT_CALENDAR_ID,
        "currentDate": CURRENT_DATE.isoformat(),
    }
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = openai_api_key
    result = subprocess.run(
        [js_runtime(), str(script_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=vault_root,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Calendar agent process failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Calendar agent returned invalid JSON: {exc}") from exc


def gws_command(vault_root: Path) -> list[str]:
    local = vault_root / "node_modules" / ".bin" / "gws"
    if local.exists():
        return [str(local)]
    return ["gws"]


def calendar_event_body(event: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": str(event.get("summary") or "Untitled event").strip(),
        "location": str(event.get("location") or "").strip(),
        "description": str(event.get("description") or "").strip(),
    }
    timezone = str(event.get("timeZone") or os.environ.get("VAULT_DEFAULT_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    if event.get("allDay"):
        start_date = str(event.get("start") or "")[:10]
        end_date = str(event.get("end") or "")[:10]
        if start_date and end_date and end_date <= start_date:
            try:
                end_date = (datetime.strptime(start_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
            except ValueError:
                pass
        body["start"] = {"date": start_date}
        body["end"] = {"date": end_date}
    else:
        body["start"] = {"dateTime": str(event.get("start") or "").strip(), "timeZone": timezone}
        body["end"] = {"dateTime": str(event.get("end") or "").strip(), "timeZone": timezone}
    recurrence = [str(item).strip() for item in event.get("recurrence") or [] if str(item).strip()]
    if recurrence:
        body["recurrence"] = recurrence
    attendees = [str(item).strip() for item in event.get("attendees") or [] if str(item).strip()]
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]
    return {key: value for key, value in body.items() if value != "" and value != [] and value != {}}


def configure_gws_credentials(vault_root: Path, env: dict[str, str]) -> None:
    credentials_json = os.environ.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON", "").strip()
    if credentials_json and "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE" not in env:
        path = vault_root / ".runtime" / "google-workspace-credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalize_gws_credentials_json(credentials_json))
        env["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(path)
        env.setdefault("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND", "file")


def normalize_gws_credentials_json(raw: str) -> str:
    text = str(raw or "").strip()
    candidates = [text]
    try:
        candidates.append(bytes(text, "utf-8").decode("unicode_escape"))
    except Exception:
        pass

    for candidate in candidates:
        current = candidate.strip()
        for _ in range(3):
            try:
                parsed = json.loads(current)
            except json.JSONDecodeError:
                break
            if isinstance(parsed, dict):
                return json.dumps(normalize_gws_credentials_payload(parsed), separators=(",", ":"))
            if isinstance(parsed, str):
                current = parsed.strip()
                continue
            break

    raise RuntimeError("GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON could not be parsed as credentials JSON")


def normalize_gws_credentials_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    private_key = normalized.get("private_key")
    if isinstance(private_key, str):
        key = private_key.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")
        if "-----BEGIN" in key and not key.endswith("\n"):
            key += "\n"
        normalized["private_key"] = key
    return normalized


def run_gws(vault_root: Path, args: list[str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    configure_gws_credentials(vault_root, env)
    command = gws_command(vault_root) + args
    if payload is not None:
        command.extend(["--json", json.dumps(payload)])
    result = subprocess.run(command, text=True, capture_output=True, cwd=vault_root, env=env, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "gws command failed"
        raise RuntimeError(detail)
    stdout = result.stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"raw": stdout}


def execute_calendar_plan(vault_root: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    calendar_id = str(plan.get("targetCalendarId") or DEFAULT_CALENDAR_ID).strip() or DEFAULT_CALENDAR_ID
    operation = str(plan.get("operation") or "none").strip()
    results: list[dict[str, Any]] = []

    if operation == "create":
        for event in plan.get("events") or []:
            created = run_gws(
                vault_root,
                ["calendar", "events", "insert", "--params", json.dumps({"calendarId": calendar_id})],
                calendar_event_body(event),
            )
            results.append({"operation": "create", "calendar_id": calendar_id, "event": created, "input": event})
        return results

    if operation == "update":
        target_event_ids = clean_scalar_list(plan.get("targetEventIds"))
        events = plan.get("events") or []
        if target_event_ids:
            if len(target_event_ids) != len(events):
                raise RuntimeError("Calendar batch update target count does not match event count")
            for event_id, event in zip(target_event_ids, events):
                updated = run_gws(
                    vault_root,
                    ["calendar", "events", "patch", "--params", json.dumps({"calendarId": calendar_id, "eventId": event_id})],
                    calendar_event_body(event),
                )
                results.append({"operation": "update", "calendar_id": calendar_id, "event_id": event_id, "event": updated, "input": event})
            return results

        event_id = str(plan.get("targetEventId") or "").strip()
        if not event_id:
            raise RuntimeError("Calendar update requested without targetEventId")
        if not events:
            raise RuntimeError("Calendar update requested without event details")
        updated = run_gws(
            vault_root,
            ["calendar", "events", "patch", "--params", json.dumps({"calendarId": calendar_id, "eventId": event_id})],
            calendar_event_body(events[0]),
        )
        results.append({"operation": "update", "calendar_id": calendar_id, "event_id": event_id, "event": updated, "input": events[0]})
        return results

    if operation == "delete":
        event_id = str(plan.get("targetEventId") or "").strip()
        if not event_id:
            raise RuntimeError("Calendar delete requested without targetEventId")
        deleted = run_gws(vault_root, ["calendar", "events", "delete", "--params", json.dumps({"calendarId": calendar_id, "eventId": event_id})])
        results.append({"operation": "delete", "calendar_id": calendar_id, "event_id": event_id, "event": deleted})
        return results

    raise RuntimeError(f"Unsupported calendar operation: {operation}")


def format_calendar_confirmation(plan: dict[str, Any]) -> str:
    explicit = str(plan.get("confirmationText") or "").strip()
    if explicit:
        return explicit
    events = plan.get("events") or []
    if not events:
        return "I need a bit more detail before I can add this to your calendar."
    lines = ["Please confirm these calendar details:"]
    for index, event in enumerate(events, start=1):
        summary = str(event.get("summary") or "Untitled event").strip()
        start = str(event.get("start") or "").strip()
        end = str(event.get("end") or "").strip()
        location = str(event.get("location") or "").strip()
        lines.append(f"{index}. {summary}")
        lines.append(f"   When: {start} to {end}")
        if location:
            lines.append(f"   Where: {location}")
        recurrence = ", ".join(event.get("recurrence") or [])
        if recurrence:
            lines.append(f"   Repeats: {recurrence}")
    lines.append("Reply yes to save, or send corrections.")
    return "\n".join(lines)


def format_calendar_success(results: list[dict[str, Any]]) -> str:
    if not results:
        return "Calendar updated."
    lines = []
    for result in results:
        operation = result.get("operation")
        event = result.get("event") or {}
        title = event.get("summary") or (result.get("input") or {}).get("summary") or "event"
        if operation == "create":
            lines.append(f"Added: {title}")
        elif operation == "update":
            lines.append(f"Updated: {title}")
        elif operation == "delete":
            lines.append("Deleted the calendar event.")
    return "\n".join(lines)


def extract_event_id(result: dict[str, Any]) -> str:
    event = result.get("event") or {}
    if isinstance(event, dict):
        return str(event.get("id") or "").strip()
    return ""


def confirmed_calendar_plan_from_pending(pending_request: dict[str, Any]) -> dict[str, Any]:
    plan = dict((pending_request or {}).get("plan") or {})
    plan["calendarIntent"] = True
    plan["needsClarification"] = False
    plan["needsConfirmation"] = False
    plan["userConfirmed"] = True
    return plan


def invoke_vault_query_agent(
    vault_root: Path,
    openai_api_key: str,
    question: str,
    model: str,
    reasoning_effort: str,
    prior_thread_id: str | None,
    recent_conversation_context: str = "",
) -> dict[str, Any]:
    script_path = vault_root / "tools" / "telegram_vault_query.mjs"
    resume_threads = os.environ.get("VAULT_DISABLE_CODEX_THREAD_RESUME", "").strip() != "1"
    payload = {
        "model": model,
        "reasoningEffort": reasoning_effort,
        "workingDirectory": str(vault_root),
        "question": question,
        "threadId": prior_thread_id if resume_threads else None,
        "includeWebSearch": True,
        "recentConversationContext": recent_conversation_context,
    }
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = openai_api_key
    result = subprocess.run(
        [js_runtime(), str(script_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=vault_root,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Vault query agent process failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Vault query agent returned invalid JSON: {exc}") from exc


def format_telegram_query_response(query_result: dict[str, Any]) -> str:
    answer = query_result.get("answer") or {}
    body = str(answer.get("concise_answer") or answer.get("answer_markdown") or "").strip()
    body = strip_markdown_for_telegram(body)

    citations = []
    for citation in answer.get("citations") or []:
        source_url = str(citation.get("source_url") or "").strip()
        path = str(citation.get("path") or "").strip()
        title = str(citation.get("title") or path or "Source").strip()
        if source_url:
            citations.append((title, source_url))
        elif path:
            citations.append((title, f"vault:{path}"))

    lines = [body] if body else ["No answer available."]
    if citations:
        lines.append("")
        lines.append("Receipts 🔗")
        for index, (title, url) in enumerate(citations[:8], start=1):
            lines.append(f"{index}. {title}")
            lines.append(url)

    gaps = [str(gap).strip() for gap in answer.get("gaps") or [] if str(gap).strip()]
    if gaps:
        lines.append("")
        lines.append("Caveats / gaps 🕳️")
        for gap in gaps[:3]:
            lines.append(f"- {gap}")

    follow_ups = [str(item).strip() for item in answer.get("follow_up_questions") or [] if str(item).strip()]
    if follow_ups:
        lines.append("")
        lines.append(f"If useful, ask me: {follow_ups[0]}")

    return "\n".join(lines).strip()


def strip_markdown_for_telegram(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r" \1 ", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("*", "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def split_message_chunks(text: str, limit: int = 3500) -> list[str]:
    source = str(text or "").strip()
    if not source:
        return [""]
    chunks: list[str] = []
    remaining = source
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def append_agent_event(vault_root: Path, event: dict[str, Any]) -> None:
    payload = {"surface": "telegram", **event}
    append_vault_event(vault_root, payload)


def run_vault_cache_compile(vault_root: Path) -> None:
    script = vault_root / "tools" / "vault_compile_cache.py"
    if not script.exists():
        return
    proc = subprocess.run(
        ["python3", str(script), "--vault-root", str(vault_root), "--quiet"],
        text=True,
        capture_output=True,
        cwd=vault_root,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "vault cache compile failed")


def coalesce_update_groups(updates: list[dict[str, Any]], processed_update_ids: set[int]) -> list[list[dict[str, Any]]]:
    window_seconds = int(os.environ.get("VAULT_TELEGRAM_COLLECT_WINDOW_SECONDS", "45"))
    max_group = int(os.environ.get("VAULT_TELEGRAM_COLLECT_MAX", "6"))
    if window_seconds <= 0:
        return [[update] for update in updates]
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for update in sorted(updates, key=lambda item: int(item.get("update_id") or 0)):
        try:
            update_id = int(update["update_id"])
        except Exception:
            groups.append([update])
            continue
        if update_id in processed_update_ids:
            groups.append([update])
            continue
        normalized = normalize_message(update)
        if normalized is None or not is_collectable_update(update, normalized):
            if current:
                groups.append(current)
                current = []
            groups.append([update])
            continue
        if not current:
            current = [update]
            continue
        previous = normalize_message(current[-1])
        if (
            previous
            and int(previous["chat_id"]) == int(normalized["chat_id"])
            and abs(parse_timestamp_seconds(normalized) - parse_timestamp_seconds(previous)) <= window_seconds
            and len(current) < max_group
        ):
            current.append(update)
        else:
            groups.append(current)
            current = [update]
    if current:
        groups.append(current)
    return groups


def is_collectable_update(update: dict[str, Any], normalized: dict[str, Any]) -> bool:
    message = update.get("message") or update.get("edited_message") or update.get("channel_post") or update.get("edited_channel_post") or {}
    if collect_message_attachments(message):
        return False
    raw_text = str(normalized.get("raw_text") or "")
    if CALENDAR_INTENT_RE.search(raw_text) or CALENDAR_CONFIRM_RE.search(raw_text) or CALENDAR_CANCEL_RE.search(raw_text):
        return False
    return True


def parse_timestamp_seconds(normalized: dict[str, Any]) -> int:
    try:
        return int(datetime.fromisoformat(str(normalized["timestamp_iso"])).timestamp())
    except Exception:
        return 0


def merge_normalized_message_group(update_group: list[dict[str, Any]], latest_normalized: dict[str, Any]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for update in update_group:
        normalized = normalize_message(update)
        if normalized:
            parts.append(normalized)
    if len(parts) <= 1:
        return latest_normalized
    merged = dict(latest_normalized)
    merged["raw_text"] = "\n\n".join(
        f"[Message {idx + 1} at {part.get('timestamp_iso')}]\n{part.get('raw_text')}"
        for idx, part in enumerate(parts)
    )
    merged["export_line"] = "\n".join(str(part.get("export_line") or "").strip() for part in parts if str(part.get("export_line") or "").strip())
    merged["collected_update_ids"] = [int(update["update_id"]) for update in update_group]
    return merged


def invoke_codex_agent(
    vault_root: Path,
    session_name: str,
    agent_model: str,
    agent_reasoning_effort: str,
    openai_api_key: str,
    message: dict[str, Any],
    known_chats: dict[str, Any],
    prior_thread_id: str | None,
    recent_conversation_context: str = "",
) -> dict[str, Any]:
    script_path = vault_root / "tools" / "telegram_codex_agent.mjs"
    resume_threads = os.environ.get("VAULT_DISABLE_CODEX_THREAD_RESUME", "").strip() != "1"
    payload = {
        "model": agent_model,
        "reasoningEffort": agent_reasoning_effort,
        "workingDirectory": str(vault_root),
        "additionalDirectories": [str(vault_root / "raw"), str(vault_root / "imports"), str(vault_root / "items")],
        "threadId": prior_thread_id if resume_threads else None,
        "instructions": {
            "ack_rule": "Send a thumbs up only after the message was successfully ingested and all requested local actions completed.",
            "message_context_rule": "Preserve user-written context or instructions that accompany links.",
            "tool_scope_rule": "Only request the provided local actions; do not invent tools.",
            "future_capture_rule": "Assume the user is sending future material that should usually be filed unless it is clearly ignorable.",
            "task_completion_rule": "If the user says they completed, applied to, submitted, read, skipped, cancelled, or handled something, request update_task_ledger and treat it as authoritative. Infer references like 'there', 'that one', and 'Anthropic' from the recent conversation where possible.",
            "priority_rule": "If the user explicitly says something is low, medium, high, or critical priority, request update_task_ledger so the matching vault note/task can be reprioritized.",
        },
        "message": message,
        "knownChats": known_chats,
        "recentConversationContext": recent_conversation_context,
    }
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = openai_api_key
    result = subprocess.run(
        [js_runtime(), str(script_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=vault_root,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Codex agent process failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex agent returned invalid JSON: {exc}") from exc


def execute_agent_actions(
    vault_root: Path,
    session_name: str,
    normalized_message: dict[str, Any],
    decision: dict[str, Any],
    actions: list[dict[str, str]],
    state: dict[str, Any],
    openai_api_key: str,
    agent_model: str,
    agent_reasoning_effort: str,
) -> list[dict[str, Any]]:
    inbox_dir, _ = ensure_directories(vault_root)
    stream_file = stream_path(inbox_dir, session_name)
    jobs_recent_cutoff = (date.today() - timedelta(days=30)).isoformat()
    results: list[dict[str, Any]] = []

    for action in actions:
        tool = action["tool"]
        reason = action["reason"]
        if tool == "append_message_to_stream":
            append_stream(stream_file, [normalized_message["export_line"]])
            results.append({"tool": tool, "status": "ok", "reason": reason, "stream_file": str(stream_file)})
            continue

        if tool == "run_vault_ingest":
            ingest(vault_root, stream_file)
            sync_proc = subprocess.run(
                ["python3", str(vault_root / "tools" / "vault_tasks.py"), "sync-from-vault", "--vault-root", str(vault_root)],
                text=True,
                capture_output=True,
                cwd=vault_root,
                check=False,
            )
            if sync_proc.returncode != 0:
                raise RuntimeError(sync_proc.stderr.strip() or sync_proc.stdout.strip() or "task ledger sync failed")
            results.append({"tool": tool, "status": "ok", "reason": reason})
            continue

        if tool == "rebuild_artifact_capture_queue":
            artifact_script = vault_root / "tools" / "artifact_context.py"
            proc = subprocess.run(
                ["python3", str(artifact_script), "queue", "--vault-root", str(vault_root)],
                text=True,
                capture_output=True,
                cwd=vault_root,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "artifact_context queue failed")
            results.append({"tool": tool, "status": "ok", "reason": reason})
            continue

        if tool == "refresh_live_metadata_jobs_recent":
            enrich_script = vault_root / "tools" / "enrich_live_metadata.py"
            proc = subprocess.run(
                [
                    "python3",
                    str(enrich_script),
                    "--vault-root",
                    str(vault_root),
                    "--cutoff",
                    jobs_recent_cutoff,
                    "--mode",
                    "jobs_recent",
                ],
                text=True,
                capture_output=True,
                cwd=vault_root,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "jobs_recent enrichment failed")
            results.append({"tool": tool, "status": "ok", "reason": reason})
            continue

        if tool == "refresh_live_metadata_knowledge_all":
            enrich_script = vault_root / "tools" / "enrich_live_metadata.py"
            proc = subprocess.run(
                [
                    "python3",
                    str(enrich_script),
                    "--vault-root",
                    str(vault_root),
                    "--mode",
                    "knowledge_all",
                ],
                text=True,
                capture_output=True,
                cwd=vault_root,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "knowledge_all enrichment failed")
            results.append({"tool": tool, "status": "ok", "reason": reason})
            continue

        if tool == "refresh_live_metadata_current_links":
            urls = extract_message_urls(str(normalized_message.get("raw_text") or ""))
            if not urls:
                results.append({"tool": tool, "status": "skipped", "reason": "No URL found in message."})
                continue
            enrich_script = vault_root / "tools" / "enrich_live_metadata.py"
            command = [
                "python3",
                str(enrich_script),
                "--vault-root",
                str(vault_root),
                "--mode",
                "targeted",
            ]
            for url in urls:
                command.extend(["--url", url])
            proc = subprocess.run(
                command,
                text=True,
                capture_output=True,
                cwd=vault_root,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "targeted live metadata enrichment failed")
            results.append({"tool": tool, "status": "ok", "reason": reason, "urls": urls})
            continue

        if tool == "answer_vault_query":
            prior_thread_id = (state.get("query_threads") or {}).get(str(normalized_message["chat_id"]))
            recent_context = build_recent_telegram_context(
                vault_root=vault_root,
                inbox_dir=inbox_dir,
                session_name=session_name,
                chat_id=int(normalized_message["chat_id"]),
                normalized_message=normalized_message,
            )
            query_result = invoke_vault_query_agent(
                vault_root=vault_root,
                openai_api_key=openai_api_key,
                question=normalized_message["raw_text"],
                model=agent_model,
                reasoning_effort=agent_reasoning_effort,
                prior_thread_id=prior_thread_id,
                recent_conversation_context=recent_context,
            )
            thread_id = str(query_result.get("threadId") or "").strip()
            if thread_id:
                state.setdefault("query_threads", {})
                state["query_threads"][str(normalized_message["chat_id"])] = thread_id
            results.append(
                {
                    "tool": tool,
                    "status": "ok",
                    "reason": reason,
                    "thread_id": thread_id,
                    "reply_text": format_telegram_query_response(query_result),
                    "usage": query_result.get("usage"),
                    "cost": query_result.get("cost"),
                }
            )
            continue

        if tool == "update_task_ledger":
            context_text = recent_context_for_task_update(stream_file, normalized_message)
            task_proc = subprocess.run(
                [
                    "python3",
                    str(vault_root / "tools" / "vault_tasks.py"),
                    "update-from-message",
                    "--vault-root",
                    str(vault_root),
                    "--message-text",
                    str(normalized_message.get("raw_text") or ""),
                    "--context-text",
                    context_text,
                    "--source-id",
                    f"telegram:{normalized_message.get('update_id')}",
                    "--source",
                    "telegram",
                ],
                text=True,
                capture_output=True,
                cwd=vault_root,
                check=False,
            )
            if task_proc.returncode != 0:
                raise RuntimeError(task_proc.stderr.strip() or task_proc.stdout.strip() or "task ledger update failed")
            try:
                task_payload = json.loads(task_proc.stdout or "{}")
            except json.JSONDecodeError:
                task_payload = {"raw_output": task_proc.stdout}
            results.append({"tool": tool, "status": "ok", "reason": reason, "task_update": task_payload})
            continue

        raise RuntimeError(f"Unsupported agent tool: {tool}")

    if any(
        result.get("status") == "ok"
        and result.get("tool")
        in {
            "append_message_to_stream",
            "run_vault_ingest",
            "rebuild_artifact_capture_queue",
            "refresh_live_metadata_jobs_recent",
            "refresh_live_metadata_knowledge_all",
            "refresh_live_metadata_current_links",
            "update_task_ledger",
        }
        for result in results
    ):
        run_vault_cache_compile(vault_root)
        results.append({"tool": "compile_vault_cache", "status": "ok", "reason": "Refresh machine-facing digest, claims, source index, and search index."})

    return results


def handle_calendar_flow(
    *,
    vault_root: Path,
    token: str,
    session_name: str,
    normalized_message: dict[str, Any],
    state: dict[str, Any],
    openai_api_key: str,
    agent_model: str,
    agent_reasoning_effort: str,
) -> dict[str, Any] | None:
    if not should_run_calendar_agent(normalized_message, state):
        return None

    inbox_dir, _ = ensure_directories(vault_root)
    chat_id = int(normalized_message["chat_id"])
    pending_by_chat = state.setdefault("calendar_pending", {})
    pending_request = pending_by_chat.get(str(chat_id))
    raw_text = str(normalized_message.get("raw_text") or "").strip()
    calendar_history = recent_calendar_history(state, chat_id)
    calendar_result: dict[str, Any] = {}
    if pending_request and CALENDAR_CANCEL_RE.match(raw_text):
        pending_by_chat.pop(str(chat_id), None)
        telegram_send_message(token, chat_id=chat_id, text="Cancelled the pending calendar request.", reply_to_message_id=normalized_message["message_id"])
        append_jsonl(
            calendar_actions_path(inbox_dir, session_name),
            [
                {
                    "logged_at": datetime.now().isoformat(),
                    "chat_id": chat_id,
                    "message_id": normalized_message["message_id"],
                    "update_id": normalized_message["update_id"],
                    "message": normalized_message,
                    "pending_before": pending_request,
                    "status": "cancelled",
                    "reason": "deterministic_cancel_reply",
                    "results": [],
                }
            ],
        )
        return {"status": "cancelled", "operation": "cancel", "answered": True, "acked": False}

    if pending_request and CALENDAR_CONFIRM_RE.match(raw_text):
        plan = confirmed_calendar_plan_from_pending(pending_request)
        calendar_result = {"threadId": "", "plan": plan, "deterministic": "confirm_reply"}
    else:
        calendar_result = invoke_calendar_agent(
            vault_root=vault_root,
            openai_api_key=openai_api_key,
            message=normalized_message,
            pending_request=pending_request,
            history=calendar_history,
            model=agent_model,
            reasoning_effort=agent_reasoning_effort,
        )
        plan = calendar_result.get("plan") or {}
    if not plan.get("calendarIntent") and not pending_request:
        return None

    plan = resolve_calendar_target_event(plan, pending_request, calendar_history)
    if calendar_target_missing(plan):
        plan = plan_calendar_target_clarification(plan, calendar_history)

    action_log: dict[str, Any] = {
        "logged_at": datetime.now().isoformat(),
        "chat_id": chat_id,
        "message_id": normalized_message["message_id"],
        "update_id": normalized_message["update_id"],
        "message": normalized_message,
        "pending_before": pending_request,
        "plan": plan,
        "thread_id": calendar_result.get("threadId"),
        "results": [],
    }

    operation = str(plan.get("operation") or "none")
    if operation == "cancel":
        pending_by_chat.pop(str(chat_id), None)
        telegram_send_message(token, chat_id=chat_id, text="Cancelled the pending calendar request.", reply_to_message_id=normalized_message["message_id"])
        action_log["status"] = "cancelled"
        append_jsonl(calendar_actions_path(inbox_dir, session_name), [action_log])
        return {"status": "cancelled", "operation": operation, "answered": True, "acked": False}

    if plan.get("needsClarification"):
        question = str(plan.get("clarificationQuestion") or "What date and time should I use?").strip()
        pending_by_chat[str(chat_id)] = {
            "created_at": datetime.now().isoformat(),
            "source_message": normalized_message,
            "plan": plan,
        }
        telegram_send_message(token, chat_id=chat_id, text=question, reply_to_message_id=normalized_message["message_id"])
        action_log["status"] = "clarification_requested"
        append_jsonl(calendar_actions_path(inbox_dir, session_name), [action_log])
        return {"status": "clarification_requested", "operation": operation, "answered": True, "acked": False}

    if plan.get("needsConfirmation") or not plan.get("userConfirmed"):
        pending_by_chat[str(chat_id)] = {
            "created_at": datetime.now().isoformat(),
            "source_message": normalized_message,
            "plan": plan,
        }
        telegram_send_message(token, chat_id=chat_id, text=format_calendar_confirmation(plan), reply_to_message_id=normalized_message["message_id"])
        action_log["status"] = "confirmation_requested"
        append_jsonl(calendar_actions_path(inbox_dir, session_name), [action_log])
        return {"status": "confirmation_requested", "operation": operation, "answered": True, "acked": False}

    try:
        results = execute_calendar_plan(vault_root, plan)
    except Exception as exc:
        if "targetEventId" in str(exc):
            plan = plan_calendar_target_clarification(plan, calendar_history)
            pending_by_chat[str(chat_id)] = {
                "created_at": datetime.now().isoformat(),
                "source_message": normalized_message,
                "plan": plan,
                "last_error": f"{type(exc).__name__}: {exc}",
            }
            telegram_send_message(token, chat_id=chat_id, text=plan["clarificationQuestion"], reply_to_message_id=normalized_message["message_id"])
            action_log["status"] = "clarification_requested"
            action_log["error"] = f"{type(exc).__name__}: {exc}"
            append_jsonl(calendar_actions_path(inbox_dir, session_name), [action_log])
            return {"status": "clarification_requested", "operation": operation, "answered": True, "acked": False}
        pending_by_chat[str(chat_id)] = {
            "created_at": datetime.now().isoformat(),
            "source_message": normalized_message,
            "plan": plan,
            "last_error": f"{type(exc).__name__}: {exc}",
        }
        message = (
            "I could not update Google Calendar yet.\n"
            f"Reason: {type(exc).__name__}: {exc}\n"
            "The request is still pending, so you can retry after calendar auth is fixed or send corrections."
        )
        telegram_send_message(token, chat_id=chat_id, text=message, reply_to_message_id=normalized_message["message_id"])
        action_log["status"] = "execution_failed"
        action_log["error"] = f"{type(exc).__name__}: {exc}"
        append_jsonl(calendar_actions_path(inbox_dir, session_name), [action_log])
        return {"status": "execution_failed", "operation": operation, "answered": True, "acked": False}

    pending_by_chat.pop(str(chat_id), None)
    history = state.setdefault("calendar_history", [])
    for result in results:
        event_id = extract_event_id(result)
        input_event = result.get("input") or {}
        history.append(
            {
                "created_at": datetime.now().isoformat(),
                "chat_id": chat_id,
                "operation": result.get("operation"),
                "calendar_id": result.get("calendar_id") or plan.get("targetCalendarId") or DEFAULT_CALENDAR_ID,
                "event_id": event_id or result.get("event_id") or "",
                "summary": (result.get("event") or {}).get("summary") or input_event.get("summary") or "",
                "start": (result.get("event") or {}).get("start") or input_event.get("start") or "",
                "end": (result.get("event") or {}).get("end") or input_event.get("end") or "",
                "source_update_id": normalized_message["update_id"],
            }
        )
    state["calendar_history"] = history[-50:]
    telegram_send_message(token, chat_id=chat_id, text=format_calendar_success(results), reply_to_message_id=normalized_message["message_id"])
    action_log["status"] = "executed"
    action_log["results"] = results
    append_jsonl(calendar_actions_path(inbox_dir, session_name), [action_log])
    return {"status": "executed", "operation": operation, "answered": True, "acked": True, "results": results}


def append_stream(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for line in lines:
            handle.write(line.rstrip() + "\n")


def recent_context_for_task_update(stream_file: Path, normalized_message: dict[str, Any], max_lines: int = 80) -> str:
    chunks: list[str] = []
    if stream_file.exists():
        try:
            chunks.append("\n".join(stream_file.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]))
        except Exception:
            pass
    export_line = str(normalized_message.get("export_line") or "").strip()
    raw_text = str(normalized_message.get("raw_text") or "").strip()
    if export_line:
        chunks.append(export_line)
    elif raw_text:
        chunks.append(raw_text)
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def build_recent_telegram_context(
    *,
    vault_root: Path,
    inbox_dir: Path,
    session_name: str,
    chat_id: int,
    normalized_message: dict[str, Any],
    max_stream_lines: int = 100,
    max_decisions: int = 12,
) -> str:
    stream_file = stream_path(inbox_dir, session_name)
    decisions_file = agent_decisions_path(inbox_dir, session_name)
    sections: list[str] = []

    sections.append(
        render_context_block(
            "Current Telegram message",
            {
                "update_id": normalized_message.get("update_id"),
                "message_id": normalized_message.get("message_id"),
                "chat_id": chat_id,
                "timestamp": normalized_message.get("timestamp_iso"),
                "sender": normalized_message.get("sender"),
                "urls": extract_message_urls(str(normalized_message.get("raw_text") or "")),
                "text": truncate_context(str(normalized_message.get("raw_text") or ""), 2400),
                "attachments": summarize_attachments_for_context(normalized_message.get("attachments") or []),
            },
        )
    )

    if stream_file.exists():
        try:
            stream_tail = "\n".join(stream_file.read_text(encoding="utf-8", errors="replace").splitlines()[-max_stream_lines:])
            if stream_tail.strip():
                sections.append("## Recent normalized Telegram stream\n" + truncate_context(stream_tail, 9000))
        except Exception:
            pass

    decision_summaries = recent_decision_summaries(decisions_file, chat_id=chat_id, limit=max_decisions)
    if decision_summaries:
        sections.append("## Recent agent decisions and artifacts\n" + "\n\n".join(decision_summaries))

    return "\n\n".join(section for section in sections if section.strip()).strip()[:20_000]


def render_context_block(title: str, payload: dict[str, Any]) -> str:
    lines = [f"## {title}"]
    for key, value in payload.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, indent=2, sort_keys=True)
            lines.append(f"{key}:\n{rendered}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def summarize_attachments_for_context(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for attachment in attachments[:6]:
        summarized.append(
            {
                "kind": attachment.get("kind"),
                "artifact_path": attachment.get("artifact_path"),
                "mime_type": attachment.get("mime_type"),
                "summary": attachment.get("summary"),
                "extracted_text": truncate_context(str(attachment.get("extracted_text") or ""), 800),
                "urls": attachment.get("urls") or [],
                "qr_values": attachment.get("qr_values") or [],
                "event_clues": attachment.get("event_clues") or [],
                "job_clues": attachment.get("job_clues") or [],
                "reminder_clues": attachment.get("reminder_clues") or [],
                "analysis_error": attachment.get("analysis_error"),
            }
        )
    return summarized


def recent_decision_summaries(path: Path, *, chat_id: int, limit: int) -> list[str]:
    summaries: list[str] = []
    for record in reversed(load_jsonl(path)):
        if int(record.get("chat_id") or 0) != int(chat_id):
            continue
        message = record.get("message") or {}
        decision = record.get("decision") or {}
        tool_results = record.get("tool_results") or []
        summary = {
            "logged_at": record.get("logged_at"),
            "update_id": record.get("update_id"),
            "message_id": record.get("message_id"),
            "text": truncate_context(str(message.get("raw_text") or message.get("export_line") or ""), 1800),
            "urls": extract_message_urls(str(message.get("raw_text") or "")),
            "attachments": summarize_attachments_for_context(message.get("attachments") or []),
            "classification": decision.get("classification"),
            "stored": bool(decision.get("storeInVault")),
            "priority": decision.get("priority"),
            "summary": decision.get("summary"),
            "actions": [str(action.get("tool") or "") for action in record.get("actions") or [] if isinstance(action, dict)],
            "tool_results": summarize_tool_results_for_context(tool_results),
        }
        summaries.append(render_context_block(f"Recent turn {len(summaries) + 1}", summary))
        if len(summaries) >= limit:
            break
    return summaries


def summarize_tool_results_for_context(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for result in tool_results[:12]:
        entry: dict[str, Any] = {
            "tool": result.get("tool"),
            "status": result.get("status"),
            "reason": result.get("reason"),
        }
        if result.get("urls"):
            entry["urls"] = result.get("urls")
        if result.get("task_update"):
            entry["task_update"] = result.get("task_update")
        if result.get("reply_text"):
            entry["reply_text"] = truncate_context(str(result.get("reply_text") or ""), 900)
        summarized.append({key: value for key, value in entry.items() if value not in (None, "", [], {})})
    return summarized


def looks_like_contextual_followup_question(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(QUESTION_INTENT_RE.search(value) and FOLLOWUP_REFERENCE_RE.search(value))


def truncate_context(value: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def sync_once(
    vault_root: Path,
    token: str,
    session_name: str,
    allowed_chat_ids: set[int],
    poll_timeout: int,
    ingest_after_sync: bool,
    agent_model: str,
    agent_reasoning_effort: str,
    openai_api_key: str,
    mode: str = "sync",
) -> dict[str, Any]:
    inbox_dir, raw_updates_dir = ensure_directories(vault_root)
    drain_telegram_delivery_queue(vault_root=vault_root, token=token, budget_seconds=8)
    state = load_state(inbox_dir)
    offset = state.get("last_update_id")
    if offset is not None:
        offset = int(offset) + 1

    updates = get_updates(token, offset=offset, poll_timeout=poll_timeout)
    return process_update_batch(
        vault_root=vault_root,
        token=token,
        session_name=session_name,
        allowed_chat_ids=allowed_chat_ids,
        ingest_after_sync=ingest_after_sync,
        agent_model=agent_model,
        agent_reasoning_effort=agent_reasoning_effort,
        openai_api_key=openai_api_key,
        updates=updates,
        mode=mode,
    )


def process_update_batch(
    vault_root: Path,
    token: str,
    session_name: str,
    allowed_chat_ids: set[int],
    ingest_after_sync: bool,
    agent_model: str,
    agent_reasoning_effort: str,
    openai_api_key: str,
    updates: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    inbox_dir, raw_updates_dir = ensure_directories(vault_root)
    state = load_state(inbox_dir)
    processed_updates_file = processed_updates_path(inbox_dir, session_name)
    decisions_file = agent_decisions_path(inbox_dir, session_name)
    processed_update_ids = load_processed_update_ids(processed_updates_file)
    raw_file = raw_updates_path(raw_updates_dir, session_name)
    accepted = 0
    acked = 0
    agent_runs = 0
    ingested = 0
    answered = 0
    action_records: list[dict[str, Any]] = []
    for update_group in coalesce_update_groups(updates, processed_update_ids):
        update = update_group[-1]
        update_ids = [int(item["update_id"]) for item in update_group]
        update_id = int(update["update_id"])
        if all(item_id in processed_update_ids for item_id in update_ids):
            state["last_update_id"] = update_id
            continue

        callback_chat = callback_chat_id(update)
        if callback_chat is not None:
            if allowed_chat_ids and callback_chat not in allowed_chat_ids:
                state["last_update_id"] = update_id
                continue
            append_jsonl(raw_file, update_group)
            accepted += 1
            append_agent_event(
                vault_root,
                {
                    "event": "telegram.callback.accepted",
                    "run_id": f"telegram-{update_id}",
                    "update_id": update_id,
                    "chat_id": callback_chat,
                    "mode": mode,
                },
            )
            callback_result = handle_vault_callback_update(vault_root=vault_root, token=token, update=update) or {
                "status": "ignored",
                "answered": False,
                "acked": False,
            }
            answered += 1 if callback_result.get("answered") else 0
            acked += 1 if callback_result.get("acked") else 0
            for item_id in update_ids:
                append_processed_update(
                    processed_updates_file,
                    {
                        "processed_at": datetime.now().isoformat(),
                        "update_id": item_id,
                        "chat_id": callback_chat,
                        "message_id": None,
                        "stored": False,
                        "acked": bool(callback_result.get("acked")),
                        "callback_status": callback_result.get("status"),
                        "callback_action": callback_result.get("action"),
                        "collected_update_ids": update_ids,
                    },
                )
                processed_update_ids.add(item_id)
            state["last_update_id"] = max(update_ids)
            action_records.append(
                {
                    "update_id": update_id,
                    "collected_update_ids": update_ids,
                    "classification": "telegram_callback",
                    "stored": False,
                    "acked": bool(callback_result.get("acked")),
                    "answered": bool(callback_result.get("answered")),
                    "actions": [str(callback_result.get("action") or callback_result.get("status") or "callback")],
                }
            )
            append_agent_event(
                vault_root,
                {
                    "event": "telegram.callback.processed",
                    "run_id": f"telegram-{update_id}",
                    "update_id": update_id,
                    "chat_id": callback_chat,
                    "result": callback_result,
                },
            )
            continue

        normalized = normalize_message(update)
        if normalized is None:
            state["last_update_id"] = update_id
            continue
        if len(update_group) > 1:
            normalized = merge_normalized_message_group(update_group, normalized)

        chat_id = int(normalized["chat_id"])
        state.setdefault("known_chats", {})
        state["known_chats"][str(chat_id)] = {
            "chat_type": normalized["chat_type"],
            "chat_title": normalized["chat_title"],
            "last_seen": normalized["timestamp_iso"],
            "sender": normalized["sender"],
        }

        if allowed_chat_ids and chat_id not in allowed_chat_ids:
            state["last_update_id"] = update_id
            continue

        accepted += 1
        typing = TelegramTypingHeartbeat(token, chat_id)
        typing.start()
        append_agent_event(
            vault_root,
            {
                "event": "telegram.update.accepted",
                "run_id": f"telegram-{update_id}",
                "update_id": update_id,
                "chat_id": chat_id,
                "message_id": normalized["message_id"],
                "mode": mode,
            },
        )
        command_result = handle_vault_command(
            vault_root=vault_root,
            token=token,
            inbox_dir=inbox_dir,
            session_name=session_name,
            state=state,
            normalized_message=normalized,
        )
        if command_result is not None:
            append_jsonl(raw_file, update_group)
            answered += 1 if command_result.get("answered") else 0
            acked += 1 if command_result.get("acked") else 0
            for item_id in update_ids:
                append_processed_update(
                    processed_updates_file,
                    {
                        "processed_at": datetime.now().isoformat(),
                        "update_id": item_id,
                        "chat_id": chat_id,
                        "message_id": normalized["message_id"],
                        "stored": False,
                        "acked": bool(command_result.get("acked")),
                        "command": command_result.get("command"),
                        "collected_update_ids": update_ids,
                    },
                )
                processed_update_ids.add(item_id)
            state["last_update_id"] = max(update_ids)
            action_records.append(
                {
                    "update_id": update_id,
                    "collected_update_ids": update_ids,
                    "classification": "telegram_command",
                    "stored": False,
                    "acked": bool(command_result.get("acked")),
                    "answered": bool(command_result.get("answered")),
                    "actions": [f"command_{command_result.get('command')}"],
                }
            )
            append_agent_event(
                vault_root,
                {
                    "event": "telegram.command.processed",
                    "run_id": f"telegram-{update_id}",
                    "update_id": update_id,
                    "chat_id": chat_id,
                    "message_id": normalized["message_id"],
                    "result": command_result,
                },
            )
            typing.stop()
            continue

        normalized = enrich_message_attachments(
            vault_root=vault_root,
            token=token,
            openai_api_key=openai_api_key,
            attachment_model=agent_model,
            update=update,
            normalized_message=normalized,
        )
        append_jsonl(raw_file, update_group)

        calendar_result = handle_calendar_flow(
            vault_root=vault_root,
            token=token,
            session_name=session_name,
            normalized_message=normalized,
            state=state,
            openai_api_key=openai_api_key,
            agent_model=agent_model,
            agent_reasoning_effort=agent_reasoning_effort,
        )
        if calendar_result is not None:
            answered += 1 if calendar_result.get("answered") else 0
            acked += 1 if calendar_result.get("acked") else 0
            for item_id in update_ids:
                append_processed_update(
                    processed_updates_file,
                    {
                        "processed_at": datetime.now().isoformat(),
                        "update_id": item_id,
                        "chat_id": chat_id,
                        "message_id": normalized["message_id"],
                        "stored": False,
                        "acked": bool(calendar_result.get("acked")),
                        "calendar_status": calendar_result.get("status"),
                        "collected_update_ids": update_ids,
                    },
                )
                processed_update_ids.add(item_id)
            state["last_update_id"] = max(update_ids)
            action_records.append(
                {
                    "update_id": update_id,
                    "collected_update_ids": update_ids,
                    "classification": "calendar_request",
                    "stored": False,
                    "acked": bool(calendar_result.get("acked")),
                    "answered": True,
                    "actions": [f"calendar_{calendar_result.get('status')}"],
                }
            )
            typing.stop()
            continue

        preview = TelegramPreviewMessage(token, chat_id, normalized["message_id"])
        preview.update("Working on it — reading this and checking the vault…", force=True)
        prior_thread_id = (state.get("agent_threads") or {}).get(str(chat_id))
        recent_conversation_context = build_recent_telegram_context(
            vault_root=vault_root,
            inbox_dir=inbox_dir,
            session_name=session_name,
            chat_id=chat_id,
            normalized_message=normalized,
        )
        preview.update("Deciding whether to save, enrich, answer, or take an action…")
        agent_result = invoke_codex_agent(
            vault_root=vault_root,
            session_name=session_name,
            agent_model=agent_model,
            agent_reasoning_effort=agent_reasoning_effort,
            openai_api_key=openai_api_key,
            message=normalized,
            known_chats=state.get("known_chats", {}),
            prior_thread_id=prior_thread_id,
            recent_conversation_context=recent_conversation_context,
        )
        agent_runs += 1
        decision = normalize_agent_decision(agent_result.get("decision") or {})
        append_agent_event(
            vault_root,
            {
                "event": "telegram.agent.decision",
                "run_id": f"telegram-{update_id}",
                "stream": "plan",
                "update_id": update_id,
                "chat_id": chat_id,
                "message_id": normalized["message_id"],
                "classification": decision.get("classification"),
                "store_in_vault": bool(decision.get("storeInVault")),
                "summary": decision.get("summary"),
                "actions": [action.get("tool") for action in decision.get("actions", []) if isinstance(action, dict)],
            },
        )
        preview.update("Plan decided. Updating the relevant vault pieces…")
        actions = ensure_action_order(
            [action for action in decision.get("actions", []) if isinstance(action, dict)],
            store_in_vault=bool(decision.get("storeInVault")),
            include_ingest=ingest_after_sync,
        )
        if decision.get("classification") == "vault_query" and not any(action["tool"] == "answer_vault_query" for action in actions):
            actions = ensure_action_order(
                actions + [{"tool": "answer_vault_query", "reason": "Answer the user's vault question directly in Telegram."}],
                store_in_vault=bool(decision.get("storeInVault")),
                include_ingest=ingest_after_sync,
            )
        if looks_like_contextual_followup_question(str(normalized.get("raw_text") or "")) and not any(action["tool"] == "answer_vault_query" for action in actions):
            actions = ensure_action_order(
                actions
                + [
                    {
                        "tool": "answer_vault_query",
                        "reason": "The message is a follow-up question that likely refers to a recent Telegram link, image, or task.",
                    }
                ],
                store_in_vault=bool(decision.get("storeInVault")),
                include_ingest=ingest_after_sync,
            )
        if TASK_COMPLETION_RE.search(str(normalized.get("raw_text") or "")) and not any(action["tool"] == "update_task_ledger" for action in actions):
            actions = ensure_action_order(
                actions
                + [
                    {
                        "tool": "update_task_ledger",
                        "reason": "The user stated that an open task was completed, applied, skipped, cancelled, or handled.",
                    }
                ],
                store_in_vault=bool(decision.get("storeInVault")),
                include_ingest=ingest_after_sync,
            )
        if TASK_PRIORITY_RE.search(str(normalized.get("raw_text") or "")) and not any(action["tool"] == "update_task_ledger" for action in actions):
            actions = ensure_action_order(
                actions
                + [
                    {
                        "tool": "update_task_ledger",
                        "reason": "The user explicitly reprioritized a recent or named vault item.",
                    }
                ],
                store_in_vault=bool(decision.get("storeInVault")),
                include_ingest=ingest_after_sync,
            )
        if (
            decision.get("storeInVault")
            and ingest_after_sync
            and extract_message_urls(str(normalized.get("raw_text") or ""))
            and not any(str(action.get("tool") or "").startswith("refresh_live_metadata_") for action in actions)
        ):
            actions = ensure_action_order(
                actions
                + [
                    {
                        "tool": "refresh_live_metadata_current_links",
                        "reason": "Try immediate lightweight metadata extraction for the exact link the user just saved.",
                    }
                ],
                store_in_vault=bool(decision.get("storeInVault")),
                include_ingest=ingest_after_sync,
            )
        tool_results: list[dict[str, Any]] = []
        if decision.get("storeInVault") or any(action.get("tool") in {"answer_vault_query", "update_task_ledger"} for action in actions):
            if any(action.get("tool") == "answer_vault_query" for action in actions):
                preview.update("Searching the compiled vault cache and building the answer…", force=True)
            elif any(action.get("tool") == "update_task_ledger" for action in actions):
                preview.update("Updating the task ledger…", force=True)
            tool_results = execute_agent_actions(
                vault_root=vault_root,
                session_name=session_name,
                normalized_message=normalized,
                decision=decision,
                actions=actions,
                state=state,
                openai_api_key=openai_api_key,
                agent_model=agent_model,
                agent_reasoning_effort=agent_reasoning_effort,
            )
            append_agent_event(
                vault_root,
                {
                    "event": "telegram.agent.actions_completed",
                    "run_id": f"telegram-{update_id}",
                    "stream": "tool",
                    "update_id": update_id,
                    "chat_id": chat_id,
                    "message_id": normalized["message_id"],
                    "results": tool_results,
                },
            )
            if any(result.get("tool") == "run_vault_ingest" for result in tool_results):
                ingested += 1
                state["last_ingest_at"] = datetime.now().isoformat()
                state["stream_file"] = stream_path(inbox_dir, session_name).relative_to(vault_root).as_posix()

        if agent_result.get("threadId"):
            state.setdefault("agent_threads", {})
            state["agent_threads"][str(chat_id)] = agent_result["threadId"]

        append_jsonl(
            decisions_file,
            [
                {
                    "logged_at": datetime.now().isoformat(),
                    "update_id": update_id,
                    "collected_update_ids": update_ids,
                    "chat_id": chat_id,
                    "message_id": normalized["message_id"],
                    "message": normalized,
                    "decision": decision,
                    "actions": actions,
                    "tool_results": tool_results,
                    "thread_id": agent_result.get("threadId"),
                }
            ],
        )
        try:
            append_memory(
                vault_root,
                surface="telegram",
                summary=str(decision.get("summary") or decision.get("classification") or "Telegram message processed."),
                raw_text=str(normalized.get("raw_text") or ""),
                metadata={
                    "update_id": update_id,
                    "chat_id": chat_id,
                    "classification": decision.get("classification"),
                    "stored": bool(decision.get("storeInVault")),
                    "actions": [action["tool"] for action in actions],
                    "tool_results": [
                        {
                            "tool": result.get("tool"),
                            "status": result.get("status"),
                            "task_update": result.get("task_update"),
                        }
                        for result in tool_results
                    ],
                },
            )
        except Exception:
            pass

        query_result = next((result for result in tool_results if result.get("tool") == "answer_vault_query" and result.get("status") == "ok"), None)
        if query_result and str(query_result.get("reply_text") or "").strip():
            telegram_deliver_long_message(
                token,
                chat_id=chat_id,
                text=str(query_result["reply_text"]),
                reply_to_message_id=normalized["message_id"],
                preview=preview,
            )
            answered += 1

        ack_allowed = bool(decision.get("storeInVault")) and bool(decision.get("sendAck", True)) and ingest_after_sync
        if ack_allowed:
            acknowledgement = str(decision.get("acknowledgement") or "👍")
            if not query_result or not str(query_result.get("reply_text") or "").strip():
                if not preview.finish(acknowledgement):
                    telegram_send_message(token, chat_id=chat_id, text=acknowledgement, reply_to_message_id=normalized["message_id"])
            acked += 1

        for item_id in update_ids:
            append_processed_update(
                processed_updates_file,
                {
                    "processed_at": datetime.now().isoformat(),
                    "update_id": item_id,
                    "chat_id": chat_id,
                    "message_id": normalized["message_id"],
                    "stored": bool(decision.get("storeInVault")),
                    "acked": ack_allowed,
                    "collected_update_ids": update_ids,
                },
            )
            processed_update_ids.add(item_id)
        state["last_update_id"] = max(update_ids)
        action_records.append(
            {
                "update_id": update_id,
                "collected_update_ids": update_ids,
                "classification": decision.get("classification"),
                "stored": bool(decision.get("storeInVault")),
                "acked": ack_allowed,
                "answered": bool(query_result),
                "actions": [action["tool"] for action in actions],
            }
        )
        append_agent_event(
            vault_root,
            {
                "event": "telegram.update.processed",
                "run_id": f"telegram-{update_id}",
                "update_id": update_id,
                "collected_update_ids": update_ids,
                "chat_id": chat_id,
                "message_id": normalized["message_id"],
                "classification": decision.get("classification"),
                "stored": bool(decision.get("storeInVault")),
                "answered": bool(query_result),
                "actions": [action["tool"] for action in actions],
                "cost": query_result.get("cost") if query_result else None,
            },
        )
        append_trajectory_event(
            vault_root,
            f"telegram-{update_id}",
            {
                "surface": "telegram",
                "event": "telegram.update.processed",
                "update_id": update_id,
                "collected_update_ids": update_ids,
                "chat_id": chat_id,
                "message_id": normalized["message_id"],
                "message": normalized,
                "decision": decision,
                "actions": actions,
                "tool_results": tool_results,
                "thread_id": agent_result.get("threadId"),
            },
        )
        typing.stop()

    save_state(inbox_dir, state)

    return {
        "mode": mode,
        "updates_seen": len(updates),
        "accepted_messages": accepted,
        "agent_runs": agent_runs,
        "ingested_messages": ingested,
        "answered_messages": answered,
        "acked_messages": acked,
        "stream_file": str(stream_path(inbox_dir, session_name)),
        "raw_updates_file": str(raw_file),
        "known_chats": state.get("known_chats", {}),
        "last_update_id": state.get("last_update_id"),
        "actions": action_records[-20:],
    }


def run_loop(
    vault_root: Path,
    token: str,
    session_name: str,
    allowed_chat_ids: set[int],
    poll_timeout: int,
    loop_interval: int,
    ingest_after_sync: bool,
    agent_model: str,
    agent_reasoning_effort: str,
    openai_api_key: str,
) -> None:
    while True:
        result = sync_once(
            vault_root=vault_root,
            token=token,
            session_name=session_name,
            allowed_chat_ids=allowed_chat_ids,
            poll_timeout=poll_timeout,
            ingest_after_sync=ingest_after_sync,
            agent_model=agent_model,
            agent_reasoning_effort=agent_reasoning_effort,
            openai_api_key=openai_api_key,
            mode="run",
        )
        print(json.dumps(result, indent=2), flush=True)
        time.sleep(loop_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Always-on Telegram bot intake for the vault.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault-root", type=Path, default=Path.cwd())
    common.add_argument("--session-name", default=DEFAULT_SESSION_NAME)
    common.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    common.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    common.add_argument("--agent-model", default=DEFAULT_AGENT_MODEL)
    common.add_argument("--agent-reasoning-effort", default="medium")
    common.add_argument(
        "--allowed-chat-ids",
        default=os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        help="Comma-separated Telegram chat IDs allowed for ingestion. Empty means accept all chats the bot can see.",
    )
    common.add_argument("--poll-timeout", type=int, default=DEFAULT_POLL_TIMEOUT_SECONDS)
    common.add_argument(
        "--no-ingest",
        action="store_true",
        help="Fetch and persist updates without re-running the vault ingest.",
    )

    sync_parser = subparsers.add_parser("sync", parents=[common], help="Fetch available updates once and ingest them.")
    sync_parser.add_argument("--verbose", action="store_true")

    run_parser = subparsers.add_parser("run", parents=[common], help="Continuously poll Telegram and ingest new messages.")
    run_parser.add_argument("--loop-interval", type=int, default=DEFAULT_LOOP_INTERVAL_SECONDS)

    webhook_parser = subparsers.add_parser(
        "webhook",
        parents=[common],
        help="Process one or more Telegram webhook updates from stdin without polling Telegram.",
    )
    webhook_parser.add_argument(
        "--update-json",
        default="",
        help="Telegram update JSON. If omitted, JSON is read from stdin.",
    )

    args = parser.parse_args()
    vault_root = args.vault_root.resolve()
    load_local_env(vault_root)
    token = str(args.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        raise SystemExit("Missing Telegram bot token. Set TELEGRAM_BOT_TOKEN or pass --bot-token.")
    openai_api_key = str(args.openai_api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not openai_api_key:
        raise SystemExit("Missing OpenAI API key. Set OPENAI_API_KEY or pass --openai-api-key.")

    allowed_chat_ids = parse_allowed_chat_ids(args.allowed_chat_ids)
    ingest_after_sync = not args.no_ingest

    if args.command == "sync":
        result = sync_once(
            vault_root=vault_root,
            token=token,
            session_name=args.session_name,
            allowed_chat_ids=allowed_chat_ids,
            poll_timeout=args.poll_timeout,
            ingest_after_sync=ingest_after_sync,
            agent_model=args.agent_model,
            agent_reasoning_effort=args.agent_reasoning_effort,
            openai_api_key=openai_api_key,
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "webhook":
        raw_update = str(args.update_json or sys.stdin.read() or "").strip()
        if not raw_update:
            raise SystemExit("Missing webhook update JSON on --update-json or stdin.")
        payload = json.loads(raw_update)
        updates = payload if isinstance(payload, list) else [payload]
        result = process_update_batch(
            vault_root=vault_root,
            token=token,
            session_name=args.session_name,
            allowed_chat_ids=allowed_chat_ids,
            ingest_after_sync=ingest_after_sync,
            agent_model=args.agent_model,
            agent_reasoning_effort=args.agent_reasoning_effort,
            openai_api_key=openai_api_key,
            updates=updates,
            mode="webhook",
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "run":
        run_loop(
            vault_root=vault_root,
            token=token,
            session_name=args.session_name,
            allowed_chat_ids=allowed_chat_ids,
            poll_timeout=args.poll_timeout,
            loop_interval=args.loop_interval,
            ingest_after_sync=ingest_after_sync,
            agent_model=args.agent_model,
            agent_reasoning_effort=args.agent_reasoning_effort,
            openai_api_key=openai_api_key,
        )


if __name__ == "__main__":
    main()
