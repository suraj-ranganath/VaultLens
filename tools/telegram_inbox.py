#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
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
DEFAULT_CALENDAR_ID = "primary"
DEFAULT_TIMEZONE = "America/Los_Angeles"
URL_RE = re.compile(r"https?://\S+")
CALENDAR_INTENT_RE = re.compile(
    r"\b(calendar|cal|gcal|schedule|scheduled|save (?:this )?(?:event|class|meeting)|add (?:this )?(?:event|class|meeting)|"
    r"put (?:this )?on (?:my )?calendar|reschedule|move (?:the )?(?:event|class|meeting)|modify (?:the )?(?:event|class|meeting)|"
    r"update (?:the )?(?:event|class|meeting)|cancel (?:the )?(?:event|class|meeting)|delete (?:the )?(?:event|class|meeting))\b",
    re.IGNORECASE,
)
CALENDAR_CONFIRM_RE = re.compile(
    r"^\s*(yes|yes please|yep|yeah|yup|confirm|confirmed|looks good|correct|save it|do it|add it|please save it)\s*[.!]?\s*$",
    re.IGNORECASE,
)
CALENDAR_CANCEL_RE = re.compile(r"^\s*(no|nope|cancel|stop|never mind|nevermind|discard|don't|do not)\s*[.!]?\s*$", re.IGNORECASE)


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


def telegram_send_message(token: str, chat_id: int, text: str, reply_to_message_id: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        params["reply_to_message_id"] = reply_to_message_id
    return telegram_api(token, "sendMessage", **params)


def telegram_edit_message_text(token: str, chat_id: int, message_id: int, text: str) -> dict[str, Any]:
    return telegram_api(token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text)


def telegram_send_chat_action(token: str, chat_id: int, action: str = "typing") -> dict[str, Any]:
    return telegram_api(token, "sendChatAction", chat_id=chat_id, action=action)


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
        "allowed_updates": json.dumps(["message", "edited_message", "channel_post", "edited_channel_post"]),
    }
    if offset is not None:
        params["offset"] = offset
    payload = telegram_api(token, "getUpdates", **params)
    return list(payload.get("result") or [])


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
        "currentDate": CURRENT_DATE.isoformat(),
    }
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = openai_api_key
    result = subprocess.run(
        ["node", str(script_path)],
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
                return json.dumps(parsed, separators=(",", ":"))
            if isinstance(parsed, str):
                current = parsed.strip()
                continue
            break

    raise RuntimeError("GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON could not be parsed as credentials JSON")


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
        event_id = str(plan.get("targetEventId") or "").strip()
        if not event_id:
            raise RuntimeError("Calendar update requested without targetEventId")
        events = plan.get("events") or []
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
    }
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = openai_api_key
    result = subprocess.run(
        ["node", str(script_path)],
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
    target = vault_root / ".vault" / "events" / AGENT_EVENTS_FILE_NAME
    payload = {"logged_at": datetime.now(timezone.utc).isoformat(), **event}
    append_jsonl(target, [payload])


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
        },
        "message": message,
        "knownChats": known_chats,
    }
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = openai_api_key
    result = subprocess.run(
        ["node", str(script_path)],
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
            query_result = invoke_vault_query_agent(
                vault_root=vault_root,
                openai_api_key=openai_api_key,
                question=normalized_message["raw_text"],
                model=agent_model,
                reasoning_effort=agent_reasoning_effort,
                prior_thread_id=prior_thread_id,
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
            history=recent_calendar_history(state, chat_id),
            model=agent_model,
            reasoning_effort=agent_reasoning_effort,
        )
        plan = calendar_result.get("plan") or {}
    if not plan.get("calendarIntent") and not pending_request:
        return None

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
                "update_id": update_id,
                "chat_id": chat_id,
                "message_id": normalized["message_id"],
                "mode": mode,
            },
        )
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
        )
        agent_runs += 1
        decision = normalize_agent_decision(agent_result.get("decision") or {})
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
        if decision.get("storeInVault") or any(action.get("tool") == "answer_vault_query" for action in actions):
            if any(action.get("tool") == "answer_vault_query" for action in actions):
                preview.update("Searching the compiled vault cache and building the answer…", force=True)
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
