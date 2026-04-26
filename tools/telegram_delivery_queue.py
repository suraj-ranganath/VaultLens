#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


API_ROOT = "https://api.telegram.org"
QUEUE_DIR = Path(".vault") / "telegram-delivery-queue"
FAILED_DIR = QUEUE_DIR / "failed"
MAX_RETRIES = 5
BACKOFF_SECONDS = [5, 25, 120, 600]


def send_or_queue_telegram_message(
    *,
    vault_root: Path,
    token: str,
    chat_id: int | str,
    text: str,
    reply_to_message_id: int | None = None,
    disable_web_page_preview: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    try:
        return send_telegram_payload(token, payload)
    except Exception as exc:
        entry_id = enqueue_delivery(
            vault_root=vault_root,
            payload=payload,
            idempotency_key=idempotency_key or default_idempotency_key(payload),
            error=str(exc),
        )
        return {"ok": False, "queued": True, "queue_id": entry_id, "error": str(exc)}


def drain_telegram_delivery_queue(*, vault_root: Path, token: str, budget_seconds: float = 20.0) -> dict[str, int]:
    ensure_queue_dirs(vault_root)
    summary = {"sent": 0, "failed": 0, "deferred": 0, "moved_to_failed": 0}
    deadline = time.time() + budget_seconds
    for path in sorted((vault_root / QUEUE_DIR).glob("*.json")):
        if time.time() >= deadline:
            break
        entry = read_entry(path)
        if not entry:
            continue
        if int(entry.get("retry_count") or 0) >= MAX_RETRIES:
            move_to_failed(vault_root, path)
            summary["moved_to_failed"] += 1
            continue
        if not eligible_for_retry(entry):
            summary["deferred"] += 1
            continue
        try:
            send_telegram_payload(token, entry["payload"])
            path.unlink(missing_ok=True)
            summary["sent"] += 1
        except Exception as exc:
            entry["retry_count"] = int(entry.get("retry_count") or 0) + 1
            entry["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            entry["last_error"] = str(exc)
            write_entry(path, entry)
            summary["failed"] += 1
    return summary


def send_telegram_payload(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(f"{API_ROOT}/bot{token}/sendMessage", data=payload, timeout=30)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {body}")
    return body


def enqueue_delivery(*, vault_root: Path, payload: dict[str, Any], idempotency_key: str, error: str) -> str:
    ensure_queue_dirs(vault_root)
    entry_id = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    path = vault_root / QUEUE_DIR / f"{entry_id}.json"
    if path.exists():
        return entry_id
    write_entry(
        path,
        {
            "id": entry_id,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "retry_count": 0,
            "last_error": error,
            "payload": payload,
        },
    )
    return entry_id


def default_idempotency_key(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def ensure_queue_dirs(vault_root: Path) -> None:
    (vault_root / QUEUE_DIR).mkdir(parents=True, exist_ok=True)
    (vault_root / FAILED_DIR).mkdir(parents=True, exist_ok=True)


def read_entry(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_entry(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def eligible_for_retry(entry: dict[str, Any]) -> bool:
    retry_count = int(entry.get("retry_count") or 0)
    if retry_count <= 0:
        return True
    last_attempt = parse_timestamp(str(entry.get("last_attempt_at") or entry.get("enqueued_at") or ""))
    if last_attempt <= 0:
        return True
    backoff = BACKOFF_SECONDS[min(retry_count - 1, len(BACKOFF_SECONDS) - 1)]
    return time.time() >= last_attempt + backoff


def parse_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def move_to_failed(vault_root: Path, path: Path) -> None:
    target = vault_root / FAILED_DIR / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, target)
