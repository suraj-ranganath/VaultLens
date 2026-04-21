#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shutil
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning


CURRENT_DATE = date.today()
RECENT_JOB_WINDOW_DAYS = 30
TEXT_EXTENSIONS = {".md", ".txt", ".html", ".htm", ".json", ".csv", ".xml"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DOC_EXTENSIONS = {".pdf", ".docx", ".pptx"}

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


@dataclass
class QueueRecord:
    path: Path
    title: str
    item_type: str
    priority: str
    discovered_on: str
    status_code: str
    host: str
    issue: str
    recommendation: str
    score: int


def load_note(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"{path} does not start with frontmatter")
    _, rest = text.split("---\n", 1)
    frontmatter, body = rest.split("\n---\n", 1)
    data = yaml.safe_load(frontmatter) or {}
    return data, body.lstrip("\n")


def dump_frontmatter(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()


def clean_text(text: Any) -> str:
    text = str(text or "").encode("utf-8", "ignore").decode("utf-8", "ignore")
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = text.replace("**", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate_text(text: str, limit: int = 220) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    clipped = text[: limit - 3].rsplit(" ", 1)[0].strip()
    return f"{clipped}..."


def split_sentences(text: str) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    return [clean_text(part) for part in parts if clean_text(part)]


def dedupe_lines(lines: list[str], *, max_items: int, min_length: int = 24, limit: int = 220) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for line in lines:
        cleaned = truncate_text(line, limit=limit)
        if len(cleaned) < min_length:
            continue
        key_base = re.sub(r"^[a-z /-]+:\s*", "", cleaned, flags=re.IGNORECASE)
        key = re.sub(r"[^a-z0-9]+", " ", key_base.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(cleaned)
        if len(kept) >= max_items:
            break
    return kept


def extract_section_lines(body: str, heading: str) -> list[str]:
    pattern = re.compile(rf"^## {re.escape(heading)}\n\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(body)
    if not match:
        return []
    lines: list[str] = []
    for raw_line in match.group(1).strip().splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            lines.append(stripped[2:].strip())
    return lines


def replace_or_insert_section(body: str, heading: str, bullets: list[str], before_heading: str = "Source Context") -> str:
    section = ""
    if bullets:
        lines = [f"## {heading}", ""]
        for bullet in bullets:
            lines.append(f"- {bullet}")
        lines.append("")
        section = "\n".join(lines)

    pattern = re.compile(rf"\n## {re.escape(heading)}\n\n.*?(?=\n## |\Z)", re.DOTALL)
    if pattern.search(body):
        body = pattern.sub(f"\n{section.rstrip()}" if section else "", body)
        body = re.sub(r"\n{3,}", "\n\n", body)
        return body.strip() + "\n"

    if not section:
        return body.strip() + "\n"

    anchor = f"\n## {before_heading}\n"
    if anchor in body:
        body = body.replace(anchor, f"\n{section}{anchor}", 1)
    else:
        body = body.rstrip() + "\n\n" + section
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip() + "\n"


def render_note(data: dict[str, Any], body: str) -> str:
    return f"---\n{dump_frontmatter(data)}\n---\n\n{body.lstrip()}"


def status_code_from_body(body: str) -> str:
    match = re.search(r"Checked live page on \d{4}-\d{2}-\d{2} \((\d+)\)\.", body)
    if match:
        return match.group(1)
    if "fetch_failed:" in body:
        return "fetch_failed"
    return ""


def host_from_url(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "")
    return (match.group(1).lower() if match else "").removeprefix("www.")


def recommended_artifact(data: dict[str, Any], host: str) -> str:
    item_type = data.get("type", "")
    topics = set(data.get("topics") or [])
    if item_type == "job":
        return "Save the official job description as a web clip or PDF, or paste the role text into raw/docs/."
    if item_type == "event":
        return "Save a screenshot or web clip that includes date, venue, registration link, and agenda."
    if item_type == "article":
        return "Save a web clip, PDF, or pasted notes so the vault stores the actual content instead of only the URL."
    if item_type == "resource" and "linkedin.com" in host:
        return "Save a screenshot or pasted summary of the profile/post, since LinkedIn is often thin or blocked."
    if item_type == "resource":
        return "Save a web clip, PDF, or pasted notes so the vault stores the actual content instead of only the URL."
    if item_type == "opportunity":
        if "job-search" in topics:
            return "Save the official job description as a web clip or PDF, or paste the role text into raw/docs/."
        return "Save the full opportunity page or paste the key eligibility/deadline details into raw/docs/."
    if "job-search" in topics:
        return "Save the official job description as a web clip or PDF, or paste the role text into raw/docs/."
    return "Attach a supporting clip, screenshot, or pasted notes so the note has first-party context."


def classify_issue(data: dict[str, Any], body: str, context_lines: list[str]) -> tuple[str, int]:
    score = 0
    status_code = status_code_from_body(body)
    if status_code in {"403", "404", "429", "fetch_failed"}:
        return f"blocked_or_missing_live_page ({status_code})", 5

    if any(line.startswith("Reference page saved as:") for line in context_lines):
        score = max(score, 4)
    if any(line.startswith("GitHub reference to repository") for line in context_lines):
        score = max(score, 3)
    if len(context_lines) <= 1:
        score = max(score, 3)

    title = str(data.get("title") or "")
    if title.lower() in {"home", "view", "edit", "cold join"} or re.fullmatch(r"[a-z0-9-]{6,}", title.lower()):
        score = max(score, 4)

    item_type = data.get("type", "")
    topics = set(data.get("topics") or [])
    raw_date = data.get("discovered_on") or ""
    recent_cutoff = CURRENT_DATE - timedelta(days=RECENT_JOB_WINDOW_DAYS)
    try:
        discovered_on = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except Exception:
        discovered_on = None
    if (item_type == "job" or "job-search" in topics) and discovered_on and discovered_on >= recent_cutoff:
        score += 2
    if item_type in {"article", "resource"} and data.get("priority") in {"high", "critical"}:
        score += 1

    if score >= 5:
        issue = "high_value_note_needs_first_party_artifact"
    elif score >= 4:
        issue = "weak_context"
    elif score >= 3:
        issue = "thin_context"
    else:
        issue = ""
    return issue, score


def collect_queue(vault_root: Path) -> list[QueueRecord]:
    records: list[QueueRecord] = []
    for path in sorted((vault_root / "items").rglob("*.md")):
        if path.name == "README.md":
            continue
        data, body = load_note(path)
        context_lines = extract_section_lines(body, "Retrieved Context") or extract_section_lines(body, "Role Context")
        if not context_lines:
            continue
        issue, score = classify_issue(data, body, context_lines)
        if not issue:
            continue
        record = QueueRecord(
            path=path,
            title=str(data.get("title") or "Untitled"),
            item_type=str(data.get("type") or "misc"),
            priority=str(data.get("priority") or "medium"),
            discovered_on=str(data.get("discovered_on") or ""),
            status_code=status_code_from_body(body),
            host=host_from_url(str(data.get("url") or "")),
            issue=issue,
            recommendation=recommended_artifact(data, host_from_url(str(data.get("url") or ""))),
            score=score,
        )
        records.append(record)
    records.sort(key=lambda record: (-record.score, record.discovered_on), reverse=False)
    return sorted(records, key=lambda record: (-record.score, record.discovered_on), reverse=False)


def write_queue_outputs(vault_root: Path, records: list[QueueRecord]) -> tuple[Path, Path]:
    dashboard_path = vault_root / "dashboards" / "artifact-capture-queue.md"
    output_path = vault_root / "outputs" / f"{CURRENT_DATE.isoformat()} artifact-capture-queue.md"

    jobish = [record for record in records if record.item_type == "job" or "job-search" in record.recommendation.lower()]
    evergreen = [record for record in records if record.item_type in {"article", "resource", "opportunity"}]
    host_counts: dict[str, int] = {}
    for record in records:
        host_counts[record.host] = host_counts.get(record.host, 0) + 1

    dashboard_lines = [
        "# Artifact Capture Queue",
        "",
        "This page surfaces notes that still need first-party source artifacts because live URLs were blocked, thin, or too low-signal.",
        "",
        "## Why This Exists",
        "",
        "- Live pages often rate-limit or hide the actual content behind JavaScript, logins, or dynamic UI.",
        "- The vault gets much better when it stores the actual source text, screenshots, or PDFs instead of only canonical URLs.",
        "- Use this queue to decide what to clip next into `raw/`.",
        "",
        "## How To Add Support",
        "",
        "1. Save a clip, PDF, screenshot, or pasted notes into `raw/web-clips/`, `raw/docs/`, or `raw/images/`.",
        "2. Attach it back to the canonical note with:",
        "",
        "```bash",
        "python3 tools/artifact_context.py attach --vault-root . --note '<note-path>' --artifact '<artifact-path>'",
        "```",
        "",
        "## Priority Capture Targets",
        "",
    ]
    for record in records[:25]:
        rel_path = record.path.relative_to(vault_root).as_posix()
        details = f"{record.item_type}, issue: {record.issue}"
        if record.status_code:
            details += f", live status: {record.status_code}"
        dashboard_lines.append(f"- [[{rel_path}|{record.title}]] ({details})")
        dashboard_lines.append(f"  Recommendation: {record.recommendation}")

    dashboard_lines.extend(["", "## Recent Job-Oriented Targets", ""])
    for record in jobish[:20]:
        rel_path = record.path.relative_to(vault_root).as_posix()
        dashboard_lines.append(f"- [[{rel_path}|{record.title}]] ({record.issue})")

    dashboard_lines.extend(["", "## Evergreen Knowledge Targets", ""])
    for record in evergreen[:25]:
        rel_path = record.path.relative_to(vault_root).as_posix()
        dashboard_lines.append(f"- [[{rel_path}|{record.title}]] ({record.issue})")

    dashboard_lines.append("")
    dashboard_path.write_text("\n".join(dashboard_lines))

    output_lines = [
        "---",
        f'title: "Artifact Capture Queue"',
        f'created_on: "{CURRENT_DATE.isoformat()}"',
        "---",
        "",
        "# Artifact Capture Queue",
        "",
        "## Scope",
        "",
        f"- Notes needing stronger first-party artifacts: {len(records)}",
        f"- Job-oriented targets: {len(jobish)}",
        f"- Knowledge/resource targets: {len(evergreen)}",
        "",
        "## Host Coverage",
        "",
    ]
    for host, count in sorted(host_counts.items(), key=lambda pair: (-pair[1], pair[0])):
        output_lines.append(f"- {host}: {count}")
    output_lines.extend(["", "## Targets", ""])
    for record in records[:100]:
        rel_path = record.path.relative_to(vault_root).as_posix()
        bits = [record.item_type, record.issue]
        if record.status_code:
            bits.append(f"status={record.status_code}")
        output_lines.append(f"- [[{rel_path}|{record.title}]] ({', '.join(bits)})")
        output_lines.append(f"  Recommendation: {record.recommendation}")
    output_lines.append("")
    output_path.write_text("\n".join(output_lines))
    return dashboard_path, output_path


def append_log_entry(vault_root: Path, dashboard_path: Path, output_path: Path) -> None:
    log_path = vault_root / "log.md"
    existing = log_path.read_text()
    entry = (
        f"\n## [{CURRENT_DATE.isoformat()}] artifacts | Built supporting-artifact workflow and capture queue\n\n"
        f"- Added a script for attaching first-party artifacts back into canonical notes.\n"
        f"- Generated [[{dashboard_path.relative_to(vault_root).as_posix()}]] and [[{output_path.relative_to(vault_root).as_posix()}]].\n"
        f"- Prioritized notes whose live URLs are blocked, thin, or too low-signal for reliable retrieval.\n"
    )
    if entry not in existing:
        log_path.write_text(existing.rstrip() + "\n" + entry)


def detect_artifact_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in DOC_EXTENSIONS:
        return "document"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "file"


def target_raw_path(vault_root: Path, artifact_path: Path) -> Path:
    ext = artifact_path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        folder = vault_root / "raw" / "images"
    elif ext in DOC_EXTENSIONS:
        folder = vault_root / "raw" / "docs"
    else:
        folder = vault_root / "raw" / "web-clips"
    return folder / artifact_path.name


def extract_text_from_artifact(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        return ""
    if ext in {".html", ".htm"}:
        return clean_text(path.read_text(errors="ignore"))
    return clean_text(path.read_text(errors="ignore"))


def summarize_artifact_text(text: str, artifact_kind: str) -> list[str]:
    if not text:
        return []
    candidates: list[str] = []
    for sentence in split_sentences(text)[:10]:
        candidates.append(sentence)
    if artifact_kind == "text":
        return dedupe_lines(candidates, max_items=5, min_length=30, limit=220)
    return dedupe_lines(candidates, max_items=3, min_length=30, limit=220)


def attach_artifact(vault_root: Path, note_path_arg: str, artifact_path_arg: str, label: str, dry_run: bool) -> dict[str, Any]:
    note_path = Path(note_path_arg)
    if not note_path.is_absolute():
        note_path = (vault_root / note_path).resolve()
    artifact_path = Path(artifact_path_arg)
    if not artifact_path.is_absolute():
        artifact_path = (vault_root / artifact_path).resolve()

    if not note_path.exists():
        raise FileNotFoundError(f"Missing note: {note_path}")
    if not artifact_path.exists():
        raise FileNotFoundError(f"Missing artifact: {artifact_path}")

    copied_path = artifact_path
    if "raw" not in artifact_path.parts:
        copied_path = target_raw_path(vault_root, artifact_path)
        if copied_path.resolve() != artifact_path.resolve() and not dry_run:
            copied_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact_path, copied_path)

    data, body = load_note(note_path)
    artifact_kind = detect_artifact_kind(copied_path)
    artifact_rel = copied_path.relative_to(vault_root).as_posix()
    artifact_label = label or copied_path.name
    supporting = extract_section_lines(body, "Supporting Artifacts")
    support_line = f"[[{artifact_rel}|{artifact_label}]] ({artifact_kind}, added {CURRENT_DATE.isoformat()})"
    if support_line not in supporting:
        supporting.append(support_line)

    artifact_text = extract_text_from_artifact(copied_path)
    artifact_context = extract_section_lines(body, "Artifact Context")
    artifact_context = dedupe_lines(
        artifact_context + summarize_artifact_text(artifact_text, artifact_kind),
        max_items=8,
        min_length=24,
        limit=220,
    )

    updated_body = replace_or_insert_section(body, "Supporting Artifacts", supporting)
    updated_body = replace_or_insert_section(updated_body, "Artifact Context", artifact_context)
    new_text = render_note(data, updated_body)

    if not dry_run:
        note_path.write_text(new_text)

    return {
        "note": note_path.relative_to(vault_root).as_posix(),
        "artifact": artifact_rel,
        "artifact_kind": artifact_kind,
        "copied": copied_path.resolve() != artifact_path.resolve(),
        "artifact_context_lines": len(artifact_context),
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a capture queue for weak notes and attach first-party artifacts back into canonical notes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    queue_parser = subparsers.add_parser("queue", help="Generate the artifact capture queue dashboard and output summary.")
    queue_parser.add_argument("--vault-root", type=Path, default=Path.cwd())

    attach_parser = subparsers.add_parser("attach", help="Attach a raw artifact to a canonical note.")
    attach_parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    attach_parser.add_argument("--note", required=True, help="Canonical note path, absolute or relative to the vault root.")
    attach_parser.add_argument("--artifact", required=True, help="Artifact path, absolute or relative to the vault root.")
    attach_parser.add_argument("--label", default="", help="Optional display label for the artifact link.")
    attach_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    vault_root = args.vault_root.resolve()

    if args.command == "queue":
        records = collect_queue(vault_root)
        dashboard_path, output_path = write_queue_outputs(vault_root, records)
        append_log_entry(vault_root, dashboard_path, output_path)
        print(
            json.dumps(
                {
                    "records": len(records),
                    "dashboard": str(dashboard_path),
                    "output": str(output_path),
                },
                indent=2,
            )
        )
        return

    if args.command == "attach":
        result = attach_artifact(vault_root, args.note, args.artifact, args.label, args.dry_run)
        print(json.dumps(result, indent=2))
        return


if __name__ == "__main__":
    main()
