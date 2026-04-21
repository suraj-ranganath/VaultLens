#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ingest_chat_export import (
    Item,
    render_deadlines,
    render_decisions_ledger,
    render_followups,
    render_jobs_ledger,
    render_jobs_urgent,
    render_reading_queue,
    render_recent_items,
    render_relevant_now,
    render_thoughts_radar,
)


def load_note(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"{path} does not start with frontmatter")
    _, rest = text.split("---\n", 1)
    frontmatter, body = rest.split("\n---\n", 1)
    data = yaml.safe_load(frontmatter) or {}
    return data, body


def item_from_note(data: dict) -> Item:
    return Item(
        key="rebuild",
        type=data.get("type", "misc"),
        title=data.get("title", "Untitled"),
        discovered_on=data.get("discovered_on", ""),
        last_relevant_on=data.get("last_relevant_on", data.get("discovered_on", "")),
        url=data.get("url", ""),
        source_excerpt=data.get("source_excerpt", ""),
        source_sender=data.get("source_sender", ""),
        tags=list(data.get("tags") or []),
        topics=list(data.get("topics") or []),
        why_saved=data.get("why_saved", ""),
        timeliness=data.get("timeliness", "evergreen"),
        priority=data.get("priority", "medium"),
        status=data.get("status", "open"),
        date_confidence=data.get("date_confidence", "unknown"),
        source_dates=list(data.get("source_dates") or []),
        interest_signals=list(data.get("interest_signals") or []),
        body_notes=[],
        company=data.get("company", ""),
        role=data.get("role", ""),
        location=data.get("location", ""),
        employment_type=data.get("employment_type", ""),
        posted_on=data.get("posted_on", ""),
        application_status=data.get("application_status", "to_review"),
        deadline=data.get("deadline", ""),
        deadline_type=data.get("deadline_type", "unknown"),
        requires_referral=bool(data.get("requires_referral", False)),
    )


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = content.encode("utf-8", "ignore").decode("utf-8", "ignore")
    path.write_text(safe)


def rebuild(vault_root: Path) -> dict:
    items: list[tuple[Item, Path]] = []
    for folder in [
        "items/articles",
        "items/decisions",
        "items/events",
        "items/jobs",
        "items/misc",
        "items/opportunities",
        "items/reminders",
        "items/resources",
        "items/thoughts",
        "items/tweets",
    ]:
        for path in sorted((vault_root / folder).glob("*.md")):
            data, _ = load_note(path)
            items.append((item_from_note(data), path))

    project_pages = sorted((vault_root / "projects").glob("*.md"))
    write(vault_root / "dashboards" / "relevant-now.md", render_relevant_now(items))
    write(vault_root / "dashboards" / "jobs-urgent.md", render_jobs_urgent(items))
    write(vault_root / "dashboards" / "jobs-ledger.md", render_jobs_ledger(items))
    write(vault_root / "dashboards" / "deadlines.md", render_deadlines(items))
    write(vault_root / "dashboards" / "reading-queue.md", render_reading_queue(items))
    write(vault_root / "dashboards" / "thoughts-radar.md", render_thoughts_radar(items))
    write(vault_root / "dashboards" / "recent-items.md", render_recent_items(items))
    write(vault_root / "dashboards" / "followups.md", render_followups(items))
    write(vault_root / "dashboards" / "decisions-ledger.md", render_decisions_ledger(project_pages))
    return {"items": len(items), "projects": len(project_pages)}


def main() -> None:
    vault_root = Path.cwd().resolve()
    result = rebuild(vault_root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
