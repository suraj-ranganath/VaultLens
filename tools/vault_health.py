#!/usr/bin/env python3

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml


CANONICAL_FOLDERS = [
    "items/articles",
    "items/decisions",
    "items/events",
    "items/jobs",
    "items/misc",
    "items/opportunities",
    "items/reminders",
    "items/resources",
    "items/systems",
    "items/thoughts",
    "items/tweets",
]
REQUIRED_FIELDS = ["type", "title", "discovered_on", "status", "priority", "why_saved"]
RECENT_ACTION_WINDOW_DAYS = 30


def parse_day(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def load_note(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(errors="ignore")
    if not text.startswith("---\n"):
        raise ValueError(f"{path} missing frontmatter")
    _, rest = text.split("---\n", 1)
    frontmatter, body = rest.split("\n---\n", 1)
    data = yaml.safe_load(frontmatter) or {}
    return data, body.lstrip("\n")


def section_bullets(body: str, heading: str) -> list[str]:
    match = re.search(rf"\n## {re.escape(heading)}\n\n([\s\S]*?)(?=\n## |\Z)", "\n" + body)
    if not match:
        return []
    bullets = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return [bullet for bullet in bullets if bullet]


def thin_context(body: str) -> bool:
    bullets = section_bullets(body, "Retrieved Context")
    if not bullets:
        return True
    joined = " ".join(bullets).lower()
    return len(bullets) < 2 or "(403)." in joined or "(404)." in joined or "reference page saved as:" in joined


def missing_summary(body: str) -> bool:
    return not section_bullets(body, "Summary")


def issue_line(link: str, message: str, discovered_day: date | None = None) -> str:
    prefix = f"`{discovered_day.isoformat()}` " if discovered_day else ""
    return f"- {prefix}{link}: {message}"


def issue_sort_key(line: str) -> tuple[str, str]:
    match = re.match(r"- `(\d{4}-\d{2}-\d{2})` ", line)
    return (match.group(1) if match else "", line)


def build_health_report(vault_root: Path) -> tuple[str, dict[str, int]]:
    pages_scanned = 0
    critical: list[str] = []
    recent_action: list[str] = []
    browser_queue: list[str] = []
    older_backlog: list[str] = []
    topic_counter: Counter[str] = Counter()
    recent_cutoff = date.today() - timedelta(days=RECENT_ACTION_WINDOW_DAYS)

    for folder in CANONICAL_FOLDERS:
        for path in sorted((vault_root / folder).glob("*.md")):
            pages_scanned += 1
            try:
                data, body = load_note(path)
            except Exception as exc:
                critical.append(f"- [[{path.relative_to(vault_root).as_posix()}]]: invalid note structure ({exc}).")
                continue

            title = data.get("title") or path.stem
            link = f"[[{path.relative_to(vault_root).as_posix()}|{title}]]"
            discovered_day = parse_day(data.get("discovered_on"))
            is_recent = discovered_day is not None and discovered_day >= recent_cutoff
            for field in REQUIRED_FIELDS:
                value = data.get(field)
                if value in ("", None, []):
                    critical.append(issue_line(link, f"missing required field `{field}`.", discovered_day))

            topics = list(data.get("topics") or [])
            for topic in topics:
                topic_counter[topic] += 1

            note_type = str(data.get("type") or "")
            if note_type in {"decision", "system"} and missing_summary(body):
                target = recent_action if is_recent else older_backlog
                target.append(issue_line(link, "missing a usable `## Summary` section.", discovered_day))
            if note_type in {"article", "resource", "tweet"} and thin_context(body):
                target = browser_queue if is_recent else older_backlog
                target.append(issue_line(link, "retrieval context is thin; run browser enrichment or save a cleaner clip.", discovered_day))
            if note_type in {"thought", "resource", "article", "tweet"} and not topics:
                target = recent_action if is_recent else older_backlog
                target.append(issue_line(link, "no topical tags assigned, which weakens retrieval.", discovered_day))
            if note_type == "job":
                if not data.get("company") or not data.get("role"):
                    target = recent_action if is_recent else older_backlog
                    target.append(issue_line(link, "job note is missing `company` or `role`.", discovered_day))
                if not data.get("posted_on"):
                    target = recent_action if is_recent else older_backlog
                    target.append(issue_line(link, "posted date is still unknown; keep checking the source page or supporting artifact.", discovered_day))
            if note_type in {"decision", "system"} and not data.get("topics"):
                target = recent_action if is_recent else older_backlog
                target.append(issue_line(link, "add topics so the note is surfaced during related queries.", discovered_day))

    for topic, count in topic_counter.most_common(10):
        project_path = vault_root / "projects" / f"{topic}.md"
        topic_path = vault_root / "topics" / f"{topic}.md"
        if count >= 5 and not topic_path.exists():
            older_backlog.append(f"- Topic `{topic}` appears in {count} notes but has no durable topic page yet.")
        if count >= 8 and not project_path.exists():
            older_backlog.append(f"- Topic `{topic}` appears in {count} notes and may deserve a project page if it becomes active work.")

    critical.sort(key=issue_sort_key, reverse=True)
    recent_action.sort(key=issue_sort_key, reverse=True)
    browser_queue.sort(key=issue_sort_key, reverse=True)
    older_backlog.sort(key=issue_sort_key, reverse=True)

    summary = {
        "pages_scanned": pages_scanned,
        "critical": len(critical),
        "recent_action": len(recent_action),
        "browser_queue": len(browser_queue),
        "older_backlog": len(older_backlog),
    }
    lines = [
        "# Vault Health",
        "",
        "Operational health check for canonical notes, retrieval quality, and maintenance gaps.",
        "",
        "## Summary",
        "",
        f"- Pages scanned: {pages_scanned}",
        f"- Critical issues: {len(critical)}",
        f"- Recent action queue: {len(recent_action)}",
        f"- Browser enrichment queue: {len(browser_queue)}",
        f"- Older backlog: {len(older_backlog)}",
        f"- Recent queue window: last {RECENT_ACTION_WINDOW_DAYS} days",
        "",
        "## Critical",
        "",
    ]
    lines.extend(critical or ["- No critical issues detected."])
    lines.extend(["", "## Recent Action Queue", ""])
    lines.extend(recent_action or ["- No recent action items detected."])
    lines.extend(["", "## Browser Enrichment Queue", ""])
    lines.extend(browser_queue or ["- No recent browser-enrichment candidates detected."])
    lines.extend(["", "## Older Backlog", ""])
    lines.extend(older_backlog or ["- No older backlog detected."])
    lines.append("")
    return "\n".join(lines), summary


def main() -> None:
    vault_root = Path.cwd().resolve()
    report, summary = build_health_report(vault_root)
    target = vault_root / "dashboards" / "vault-health.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report)
    print(json.dumps({"path": str(target), **summary}, indent=2))


if __name__ == "__main__":
    main()
