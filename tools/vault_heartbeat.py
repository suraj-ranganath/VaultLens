#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from vault_trajectory import append_trajectory_event
from telegram_delivery_queue import drain_telegram_delivery_queue, send_or_queue_telegram_message
from vault_tasks import list_tasks, sync_from_vault

try:
    import yaml
except Exception:  # pragma: no cover - yaml is present in Lambda, fallback stays cheap locally.
    yaml = None


API_ROOT = "https://api.telegram.org"
DONE_STATUSES = {"done", "closed", "archived", "applied", "rejected"}
TIMELY_TYPES = {"job", "opportunity", "event", "reminder"}
READING_TYPES = {"article", "resource", "tweet"}
PRIORITY_SCORE = {"critical": 5, "high": 4, "medium": 2, "low": 1}
DEFAULT_TIMEZONE_LABEL = "America/Los_Angeles"
DISPLAY_TITLE_LIMIT = 120


@dataclass
class Note:
    path: Path
    rel_path: str
    frontmatter: dict[str, Any]
    body: str

    @property
    def title(self) -> str:
        return clean_scalar(self.frontmatter.get("title")) or self.path.stem

    @property
    def note_type(self) -> str:
        return clean_scalar(self.frontmatter.get("type")) or infer_type(self.rel_path)

    @property
    def status(self) -> str:
        return clean_scalar(self.frontmatter.get("status")).lower()

    @property
    def priority(self) -> str:
        return clean_scalar(self.frontmatter.get("priority")).lower() or "medium"

    @property
    def url(self) -> str:
        return clean_scalar(self.frontmatter.get("url"))

    @property
    def why_saved(self) -> str:
        return clean_scalar(self.frontmatter.get("why_saved"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a focused daily vault brief.")
    parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    parser.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--today", default=os.environ.get("VAULT_BRIEF_TODAY", ""))
    parser.add_argument("--max-actions", type=int, default=int(os.environ.get("VAULT_BRIEF_MAX_ACTIONS", "5")))
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    today = parse_date(args.today) or date.today()
    try:
        sync_from_vault(vault_root)
    except Exception:
        pass
    notes = load_notes(vault_root)
    candidate_actions = collect_action_items(notes, today=today, max_items=max(args.max_actions * 3, 12))
    candidate_actions.extend(collect_task_ledger_actions(vault_root, today=today, max_items=max(args.max_actions * 2, 8)))
    candidate_actions = dedupe_candidates(candidate_actions)[: max(args.max_actions * 4, 16)]
    candidate_readings = collect_recommended_readings(notes, today=today, max_items=8)
    agent_result = run_morning_brief_agent(
        vault_root=vault_root,
        today=today,
        candidate_actions=candidate_actions,
        candidate_readings=candidate_readings,
        max_actions=args.max_actions,
    )
    should_send = bool(agent_result.get("should_send"))
    text = clean_scalar(agent_result.get("telegram_text"))
    payload = {
        "ok": True,
        "mode": "agentic_daily_brief",
        "date": today.isoformat(),
        "should_send": should_send,
        "action_count": len(agent_result.get("selected_actions") or []),
        "recommended_reading": agent_result.get("recommended_reading"),
        "actions": agent_result.get("selected_actions") or [],
        "agent_rationale": agent_result.get("rationale"),
        "candidate_action_count": len(candidate_actions),
        "candidate_reading_count": len(candidate_readings),
        "candidate_actions": candidate_actions,
        "candidate_readings": candidate_readings,
        "text": text,
    }
    append_trajectory_event(
        vault_root,
        f"morning-brief-{today.isoformat()}",
        {
            "surface": "telegram",
            "event": "morning_brief.completed",
            "date": today.isoformat(),
            "should_send": should_send,
            "agent_result": agent_result,
            "candidate_action_count": len(candidate_actions),
            "candidate_reading_count": len(candidate_readings),
        },
    )

    if should_send and not args.dry_run and args.telegram_token and args.chat_id:
        drain_telegram_delivery_queue(vault_root=vault_root, token=args.telegram_token, budget_seconds=8)
        send_telegram(vault_root, args.telegram_token, args.chat_id, text)

    print(json.dumps(payload, indent=2))


def load_notes(vault_root: Path) -> list[Note]:
    notes: list[Note] = []
    items_root = vault_root / "items"
    if not items_root.exists():
        return notes
    for path in sorted(items_root.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = parse_frontmatter(text)
        notes.append(
            Note(
                path=path,
                rel_path=path.relative_to(vault_root).as_posix(),
                frontmatter=frontmatter,
                body=body,
            )
        )
    return notes


def collect_action_items(notes: list[Note], *, today: date, max_items: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for note in notes:
        if is_done(note):
            continue
        note_type = note.note_type
        if note_type not in TIMELY_TYPES:
            continue
        deadline = parse_date(clean_scalar(note.frontmatter.get("deadline")))
        revisit_after = parse_date(clean_scalar(note.frontmatter.get("revisit_after")))
        discovered = parse_date(clean_scalar(note.frontmatter.get("discovered_on")))
        posted = parse_date(clean_scalar(note.frontmatter.get("posted_on") or note.frontmatter.get("published_on")))
        app_status = clean_scalar(note.frontmatter.get("application_status")).lower()

        if deadline and today <= deadline <= today + timedelta(days=7):
            candidates.append(
                action_payload(
                    note,
                    kind="deadline",
                    date_value=deadline,
                    score=100 - (deadline - today).days * 8 + priority_points(note),
                    reason=f"deadline in {(deadline - today).days} day(s)",
                )
            )
            continue

        if revisit_after and today <= revisit_after <= today + timedelta(days=7):
            candidates.append(
                action_payload(
                    note,
                    kind="reminder",
                    date_value=revisit_after,
                    score=85 - (revisit_after - today).days * 6 + priority_points(note),
                    reason="explicit reminder/revisit date",
                )
            )
            continue

        if note_type in {"job", "opportunity"} and app_status in {"to_apply", "to_review", "watching", ""}:
            recency_date = posted or discovered
            high_impact = note.priority in {"high", "critical"} or has_interest_signal(note, {"ai", "agent", "research", "ml", "job-search"})
            if recency_date:
                age = (today - recency_date).days
            else:
                age = 999
            if recency_date and (0 <= age <= 10 or (high_impact and 0 <= age <= 21)):
                candidates.append(
                    action_payload(
                        note,
                        kind="apply_early",
                        date_value=recency_date,
                        score=70 - min(age, 21) * 2 + priority_points(note) + (10 if high_impact else 0),
                        reason="fresh/high-impact opportunity; applying early matters",
                    )
                )
                continue

        if note.priority in {"critical", "high"}:
            anchor_date = deadline or revisit_after or posted or discovered or today
            candidates.append(
                action_payload(
                    note,
                    kind="explicit_priority",
                    date_value=anchor_date,
                    score=(92 if note.priority == "critical" else 82) + interest_points(note),
                    reason=f"explicitly marked {note.priority} priority by the user or vault",
                )
            )

    candidates.sort(key=lambda item: (-int(item["score"]), item.get("date") or "9999-99-99", item["title"]))
    deduped = dedupe_by_path(candidates)
    return deduped[: max(1, max_items)]


def collect_recommended_readings(notes: list[Note], *, today: date, max_items: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for note in notes:
        if is_done(note) or note.note_type not in READING_TYPES:
            continue
        discovered = parse_date(clean_scalar(note.frontmatter.get("discovered_on")))
        if not discovered:
            continue
        age = (today - discovered).days
        if age < 0 or age > 30:
            continue
        interest_bonus = interest_points(note)
        high_value = note.priority in {"critical", "high"} or interest_bonus >= 8
        if not high_value:
            continue
        score = 50 - age + priority_points(note) + interest_bonus
        candidates.append(
            {
                "kind": "recommended_reading",
                "title": note.title,
                "path": note.rel_path,
                "url": note.url or None,
                "date": discovered.isoformat(),
                "priority": note.priority,
                "score": score,
                "reason": reading_reason(note, age),
            }
        )
    if not candidates:
        return []
    candidates.sort(key=lambda item: (-int(item["score"]), -date_sort_value(item.get("date"))))
    return candidates[: max(1, max_items)]


def collect_task_ledger_actions(vault_root: Path, *, today: date, max_items: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for task in list_tasks(vault_root, status="open", limit=max_items * 3):
        due = parse_date(clean_scalar(task.get("due_on")))
        score = 55
        reason = "open task from the task ledger"
        if due and today <= due <= today + timedelta(days=7):
            score = 100 - (due - today).days * 8
            reason = f"task ledger item due in {(due - today).days} day(s)"
        elif clean_scalar(task.get("priority")).lower() in {"critical", "high"}:
            score = 78
            reason = "high-priority task ledger item"
        candidates.append(
            {
                "kind": clean_scalar(task.get("task_type")) or "task",
                "title": clean_scalar(task.get("title")),
                "path": clean_scalar(task.get("note_path")),
                "url": task.get("source_url") or None,
                "date": due.isoformat() if due else clean_scalar(task.get("due_on")) or None,
                "type": "task",
                "status": clean_scalar(task.get("status")) or "open",
                "priority": clean_scalar(task.get("priority")) or "medium",
                "score": score,
                "reason": reason,
            }
        )
    candidates.sort(key=lambda item: (-int(item["score"]), item.get("date") or "9999-99-99", item["title"]))
    return candidates[: max(1, max_items)]


def pick_recommended_reading(notes: list[Note], *, today: date) -> dict[str, Any] | None:
    readings = collect_recommended_readings(notes, today=today, max_items=1)
    return readings[0] if readings else None


def run_morning_brief_agent(
    *,
    vault_root: Path,
    today: date,
    candidate_actions: list[dict[str, Any]],
    candidate_readings: list[dict[str, Any]],
    max_actions: int,
) -> dict[str, Any]:
    mock_json = os.environ.get("VAULT_MORNING_BRIEF_AGENT_MOCK_JSON", "").strip()
    if mock_json:
        parsed = json.loads(mock_json)
        if not isinstance(parsed, dict):
            raise RuntimeError("VAULT_MORNING_BRIEF_AGENT_MOCK_JSON must be a JSON object")
        return parsed

    script = vault_root / "tools" / "vault_morning_brief_agent.mjs"
    if not script.exists():
        raise RuntimeError(f"Missing morning brief agent script: {script}")

    payload = {
        "workingDirectory": str(vault_root),
        "today": today.isoformat(),
        "maxActions": max_actions,
        "candidateActions": candidate_actions,
        "candidateReadings": candidate_readings,
        "model": os.environ.get("VAULT_MORNING_BRIEF_MODEL") or os.environ.get("VAULT_AGENT_MODEL") or "gpt-5.4",
        "reasoningEffort": os.environ.get("VAULT_MORNING_BRIEF_REASONING_EFFORT")
        or os.environ.get("VAULT_AGENT_REASONING_EFFORT")
        or "medium",
    }
    env = os.environ.copy()
    result = subprocess.run(
        ["node", str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=vault_root,
        env=env,
        timeout=int(os.environ.get("VAULT_MORNING_BRIEF_TIMEOUT_SECONDS", "240")),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "morning brief agent failed")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"morning brief agent returned invalid JSON: {result.stdout[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("morning brief agent returned a non-object JSON payload")
    return parsed


def action_payload(note: Note, *, kind: str, date_value: date, score: int, reason: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "title": note.title,
        "path": note.rel_path,
        "url": note.url or None,
        "date": date_value.isoformat(),
        "type": note.note_type,
        "status": note.status or clean_scalar(note.frontmatter.get("application_status")) or None,
        "priority": note.priority,
        "score": score,
        "reason": reason,
    }


def render_daily_brief(actions: list[dict[str, Any]], reading: dict[str, Any] | None, *, today: date) -> str:
    title = today.strftime("%a, %b %-d") if os.name != "nt" else today.strftime("%a, %b %#d")
    lines = [
        f"Morning vault brief - {title}",
        "Only urgent or high-impact items. No filler.",
    ]
    if actions:
        lines.extend(["", "Must do / decide this week:"])
        for idx, item in enumerate(actions, start=1):
            lines.append(f"{idx}. {brief_title(item['title'])}")
            lines.append(f"   Why: {item['reason']} ({item['type']}, {item.get('priority')})")
            lines.append(f"   Date: {item.get('date')} | Status: {item.get('status') or 'unknown'}")
            source = item.get("url") or item.get("path")
            lines.append(f"   Source: {source}")
    if reading:
        lines.extend(["", "Recommended reading:"])
        lines.append(f"- {brief_title(reading['title'])}")
        lines.append(f"  Why: {reading['reason']}")
        lines.append(f"  Source: {reading.get('url') or reading.get('path')}")
    if not actions and not reading:
        lines.extend(["", "No urgent next-7-day items or high-value recent reading found."])
    return "\n".join(lines)


def send_telegram(vault_root: Path, token: str, chat_id: str, text: str) -> None:
    send_or_queue_telegram_message(vault_root=vault_root, token=token, chat_id=chat_id, text=text)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw) or {}
            if isinstance(loaded, dict):
                return loaded, body
        except Exception:
            pass
    result: dict[str, Any] = {}
    for line in raw.splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            result[match.group(1)] = match.group(2).strip().strip("\"'")
    return result, body


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value)
    return str(value).strip().strip("\"'")


def clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [clean_scalar(item) for item in value if clean_scalar(item)]
    if isinstance(value, str):
        return [part.strip().strip("\"'") for part in re.split(r"[,;]", value.strip("[]")) if part.strip()]
    return [clean_scalar(value)]


def parse_date(value: str) -> date | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or "").strip())
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1)).date()
    except ValueError:
        return None


def infer_type(rel_path: str) -> str:
    parts = rel_path.split("/")
    return parts[1].rstrip("s") if len(parts) > 1 and parts[0] == "items" else "note"


def priority_points(note: Note) -> int:
    return PRIORITY_SCORE.get(note.priority, 2) * 4


def interest_points(note: Note) -> int:
    topics = " ".join(clean_list(note.frontmatter.get("topics"))).lower()
    tags = " ".join(clean_list(note.frontmatter.get("tags"))).lower()
    signals = " ".join(clean_list(note.frontmatter.get("interest_signals"))).lower()
    haystack = " ".join([topics, tags, signals, note.title.lower(), note.why_saved.lower()])
    score = 0
    for keyword in ["agent", "llm", "ai", "ml", "research", "systems", "coding", "startup", "job-search"]:
        if keyword in haystack:
            score += 3
    if "saved_2_times" in haystack or "high-impact" in haystack:
        score += 4
    return score


def is_done(note: Note) -> bool:
    statuses = {
        note.status,
        clean_scalar(note.frontmatter.get("application_status")).lower(),
    }
    return bool(statuses & DONE_STATUSES)


def has_interest_signal(note: Note, keywords: set[str]) -> bool:
    haystack = " ".join(
        [
            note.title,
            note.why_saved,
            clean_scalar(note.frontmatter.get("topics")),
            clean_scalar(note.frontmatter.get("tags")),
            clean_scalar(note.frontmatter.get("interest_signals")),
        ]
    ).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def reading_reason(note: Note, age: int) -> str:
    if note.priority in {"critical", "high"}:
        return f"recently saved {age} day(s) ago and marked {note.priority} priority"
    return f"recently saved {age} day(s) ago and strongly matches recurring interests"


def brief_title(value: Any) -> str:
    title = clean_scalar(value)
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) <= DISPLAY_TITLE_LIMIT:
        return title
    return title[: DISPLAY_TITLE_LIMIT - 3].rstrip() + "..."


def dedupe_by_path(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        path = str(item.get("path") or "")
        if path in seen:
            continue
        seen.add(path)
        output.append(item)
    return output


def dedupe_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda entry: (-int(entry.get("score") or 0), entry.get("date") or "9999-99-99", entry.get("title") or "")):
        key = str(item.get("path") or item.get("url") or item.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def date_sort_value(value: Any) -> int:
    parsed = parse_date(clean_scalar(value))
    if not parsed:
        return 0
    return parsed.year * 10000 + parsed.month * 100 + parsed.day


if __name__ == "__main__":
    main()
