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
RECENT_WARNING_WINDOW_DAYS = 60


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


def build_health_report(vault_root: Path) -> tuple[str, dict[str, int]]:
    pages_scanned = 0
    critical: list[str] = []
    warnings: list[str] = []
    suggestions: list[str] = []
    topic_counter: Counter[str] = Counter()
    warning_cutoff = date.today() - timedelta(days=RECENT_WARNING_WINDOW_DAYS)

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
            is_recent = discovered_day is not None and discovered_day >= warning_cutoff
            for field in REQUIRED_FIELDS:
                value = data.get(field)
                if value in ("", None, []):
                    critical.append(f"- {link}: missing required field `{field}`.")

            topics = list(data.get("topics") or [])
            for topic in topics:
                topic_counter[topic] += 1

            note_type = str(data.get("type") or "")
            if note_type in {"decision", "system"} and missing_summary(body):
                warnings.append(f"- {link}: missing a usable `## Summary` section.")
            if note_type in {"article", "resource", "tweet"} and thin_context(body):
                target = warnings if is_recent else suggestions
                target.append(f"- {link}: retrieval context is thin and should be enriched or clipped.")
            if note_type in {"thought", "resource", "article", "tweet"} and not topics:
                target = warnings if is_recent else suggestions
                target.append(f"- {link}: no topical tags assigned, which weakens retrieval.")
            if note_type == "job":
                if not data.get("company") or not data.get("role"):
                    warnings.append(f"- {link}: job note is missing `company` or `role`.")
                if not data.get("posted_on"):
                    suggestions.append(f"- {link}: posted date is still unknown; keep checking the source page or supporting artifact.")
            if note_type in {"decision", "system"} and not data.get("topics"):
                suggestions.append(f"- {link}: add topics so the note is surfaced during related queries.")

    for topic, count in topic_counter.most_common(10):
        project_path = vault_root / "projects" / f"{topic}.md"
        topic_path = vault_root / "topics" / f"{topic}.md"
        if count >= 5 and not topic_path.exists():
            suggestions.append(f"- Topic `{topic}` appears in {count} notes but has no durable topic page yet.")
        if count >= 8 and not project_path.exists():
            suggestions.append(f"- Topic `{topic}` appears in {count} notes and may deserve a project page if it becomes active work.")

    summary = {
        "pages_scanned": pages_scanned,
        "critical": len(critical),
        "warnings": len(warnings),
        "suggestions": len(suggestions),
    }
    lines = [
        "# Vault Health",
        "",
        "Lint-style health check for canonical notes, retrieval quality, and maintenance gaps.",
        "",
        "## Summary",
        "",
        f"- Pages scanned: {pages_scanned}",
        f"- Critical issues: {len(critical)}",
        f"- Warnings: {len(warnings)}",
        f"- Suggestions: {len(suggestions)}",
        "",
        "## Critical",
        "",
    ]
    lines.extend(critical or ["- No critical issues detected."])
    lines.extend(["", "## Warnings", ""])
    lines.extend(warnings or ["- No warnings detected."])
    lines.extend(["", "## Suggestions", ""])
    lines.extend(suggestions or ["- No suggestions detected."])
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
