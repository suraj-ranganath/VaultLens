#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from vault_compile_cache import main as _compile_main  # noqa: F401
except Exception:
    _compile_main = None


API_ROOT = "https://api.telegram.org"


def main() -> None:
    parser = argparse.ArgumentParser(description="Surface time-sensitive vault items when useful.")
    parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    parser.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    alerts = collect_alerts(vault_root)
    payload = {"ok": True, "alert_count": len(alerts), "alerts": alerts}

    if alerts and not args.dry_run and args.telegram_token and args.chat_id:
        send_telegram(args.telegram_token, args.chat_id, render_alerts(alerts))

    print(json.dumps(payload, indent=2))


def collect_alerts(vault_root: Path) -> list[dict[str, Any]]:
    today = date.today()
    soon = today + timedelta(days=7)
    alerts: list[dict[str, Any]] = []
    for path in sorted((vault_root / "items").rglob("*.md")) if (vault_root / "items").exists() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        note_type = str(fm.get("type") or infer_type(path, vault_root)).strip()
        title = str(fm.get("title") or path.stem).strip()
        deadline = parse_date(str(fm.get("deadline") or ""))
        status = str(fm.get("status") or "").strip()
        application_status = str(fm.get("application_status") or "").strip()
        if deadline and today <= deadline <= soon and status not in {"done", "closed", "archived"}:
            alerts.append(
                {
                    "kind": "deadline",
                    "path": path.relative_to(vault_root).as_posix(),
                    "title": title,
                    "date": deadline.isoformat(),
                    "type": note_type,
                    "status": status or application_status or None,
                }
            )
        if note_type == "job" and application_status in {"to_apply", "to_review"}:
            posted = parse_date(str(fm.get("posted_on") or fm.get("published_on") or fm.get("discovered_on") or ""))
            if posted and today - timedelta(days=14) <= posted <= today:
                alerts.append(
                    {
                        "kind": "job_action",
                        "path": path.relative_to(vault_root).as_posix(),
                        "title": title,
                        "date": posted.isoformat(),
                        "type": note_type,
                        "status": application_status,
                    }
                )
    alerts.sort(key=lambda item: (item.get("date") or "9999", item.get("kind") or ""))
    return alerts[:12]


def render_alerts(alerts: list[dict[str, Any]]) -> str:
    lines = ["Vault heartbeat: things that may need attention now"]
    for alert in alerts:
        lines.append("")
        lines.append(f"- {alert['kind']}: {alert['title']}")
        lines.append(f"  date: {alert.get('date')}")
        lines.append(f"  status: {alert.get('status') or 'unknown'}")
        lines.append(f"  note: {alert['path']}")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    response = requests.post(f"{API_ROOT}/bot{token}/sendMessage", data={"chat_id": chat_id, "text": text}, timeout=30)
    response.raise_for_status()


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    result: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            result[match.group(1)] = match.group(2).strip().strip("\"'")
    return result


def parse_date(value: str) -> date | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1)).date()
    except ValueError:
        return None


def infer_type(path: Path, vault_root: Path) -> str:
    rel = path.relative_to(vault_root).as_posix()
    parts = rel.split("/")
    return parts[1].rstrip("s") if len(parts) > 1 and parts[0] == "items" else "note"


if __name__ == "__main__":
    main()
