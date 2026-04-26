#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCAN_ROOTS = ["items", "topics", "projects", "dashboards", "outputs"]
ROOT_FILES = ["hot.md", "index.md", "AGENTS.md", "WIKI.md", "README.md", "log.md"]
CACHE_DIR = Path(".vault") / "cache"
REPORTS_DIR = Path(".vault") / "reports"
AGENT_DIGEST = CACHE_DIR / "agent-digest.json"
CLAIMS_JSONL = CACHE_DIR / "claims.jsonl"
SOURCE_INDEX_JSONL = CACHE_DIR / "source-index.jsonl"
BROWSER_QUEUE_JSONL = CACHE_DIR / "browser-enrichment-queue.jsonl"
SEARCH_SQLITE = CACHE_DIR / "search.sqlite"
MANIFEST_JSON = CACHE_DIR / "manifest.json"
CLAIM_HEALTH_REPORT = REPORTS_DIR / "claim-health.md"
OPEN_QUESTIONS_REPORT = REPORTS_DIR / "open-questions.md"


@dataclass
class Page:
    path: str
    title: str
    note_type: str
    url: str
    status: str
    priority: str
    discovered_on: str
    published_on: str
    deadline: str
    tags: list[str]
    topics: list[str]
    summary: str
    why_saved: str
    mtime: float
    size: int
    sha256: str
    frontmatter: dict[str, Any]
    text: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile the vault into machine-facing cache files.")
    parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    pages = collect_pages(vault_root)
    claims = build_claims(pages)
    sources = build_source_index(pages)
    browser_queue = build_browser_queue(pages)
    write_cache(vault_root, pages, claims, sources, browser_queue)
    build_search_index(vault_root, pages)
    write_reports(vault_root, pages, claims)

    if not args.quiet:
        print(
            json.dumps(
                {
                    "ok": True,
                    "pages": len(pages),
                    "claims": len(claims),
                    "sources": len(sources),
                    "browser_queue": len(browser_queue),
                    "cache_dir": str((vault_root / CACHE_DIR).relative_to(vault_root)),
                },
                indent=2,
            )
        )


def collect_pages(vault_root: Path) -> list[Page]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = vault_root / root
        if base.exists():
            files.extend(sorted(base.rglob("*.md")))
    for root_file in ROOT_FILES:
        path = vault_root / root_file
        if path.exists():
            files.append(path)

    pages: list[Page] = []
    for file_path in sorted(set(files)):
        if ".vault" in file_path.parts:
            continue
        try:
            stat = file_path.stat()
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative_path = file_path.relative_to(vault_root).as_posix()
        frontmatter_text, body = split_frontmatter(text)
        frontmatter = parse_frontmatter(frontmatter_text)
        title = str(frontmatter.get("title") or first_heading(body) or file_path.stem).strip()
        note_type = str(frontmatter.get("type") or infer_type(relative_path)).strip()
        summary = extract_summary(body, frontmatter)
        pages.append(
            Page(
                path=relative_path,
                title=title,
                note_type=note_type,
                url=str(frontmatter.get("url") or "").strip(),
                status=str(frontmatter.get("status") or "").strip(),
                priority=str(frontmatter.get("priority") or "").strip(),
                discovered_on=str(frontmatter.get("discovered_on") or "").strip(),
                published_on=str(frontmatter.get("published_on") or "").strip(),
                deadline=str(frontmatter.get("deadline") or "").strip(),
                tags=as_string_list(frontmatter.get("tags")),
                topics=as_string_list(frontmatter.get("topics")),
                summary=summary,
                why_saved=str(frontmatter.get("why_saved") or "").strip(),
                mtime=stat.st_mtime,
                size=stat.st_size,
                sha256=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
                frontmatter=frontmatter,
                text=text,
            )
        )
    return pages


def build_claims(pages: list[Page]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for page in pages:
        evidence = [{"path": page.path, "title": page.title, "url": page.url or None}]
        for key in ["why_saved", "deadline", "status", "priority", "published_on", "discovered_on", "url"]:
            value = str(page.frontmatter.get(key) or "").strip()
            if not value:
                continue
            claims.append(
                claim_record(
                    page=page,
                    text=f"{page.title}: {key.replace('_', ' ')} is {value}.",
                    evidence=evidence,
                    confidence=confidence_for_field(page, key),
                    source_field=key,
                )
            )

        for idx, line in enumerate(extract_claim_lines(page.text), start=1):
            claims.append(
                claim_record(
                    page=page,
                    text=line,
                    evidence=evidence,
                    confidence=0.72,
                    source_field=f"body:{idx}",
                )
            )
    return claims


def claim_record(
    page: Page,
    text: str,
    evidence: list[dict[str, Any]],
    confidence: float,
    source_field: str,
) -> dict[str, Any]:
    claim_id = hashlib.sha1(f"{page.path}\n{source_field}\n{text}".encode("utf-8")).hexdigest()[:16]
    last_touched = newest_date(page.published_on, page.discovered_on) or datetime.fromtimestamp(page.mtime, timezone.utc).date().isoformat()
    return {
        "id": f"claim.{claim_id}",
        "page_path": page.path,
        "page_title": page.title,
        "page_type": page.note_type,
        "text": text,
        "status": "active",
        "confidence": confidence,
        "evidence": evidence,
        "source_field": source_field,
        "last_touched": last_touched,
        "freshness": freshness_label(last_touched),
    }


def build_source_index(pages: list[Page]) -> list[dict[str, Any]]:
    entries = []
    for page in pages:
        if not page.url:
            continue
        entries.append(
            {
                "url": page.url,
                "path": page.path,
                "title": page.title,
                "type": page.note_type,
                "published_on": page.published_on or None,
                "discovered_on": page.discovered_on or None,
                "deadline": page.deadline or None,
                "tags": page.tags,
                "topics": page.topics,
                "summary": page.summary,
            }
        )
    return entries


def build_browser_queue(pages: list[Page]) -> list[dict[str, Any]]:
    queue = []
    today = date.today()
    for page in pages:
        if not page.url:
            continue
        item_date = parse_iso_date(page.discovered_on or page.published_on)
        if item_date is None or (today - item_date).days > 30:
            continue
        weak_summary = len(page.summary) < 80 or "blocked" in page.summary.lower() or "unable to fetch" in page.text.lower()
        browser_first = any(domain in page.url.lower() for domain in ["x.com/", "twitter.com/", "linkedin.com/", "instagram.com/"])
        if not weak_summary and not browser_first:
            continue
        queue.append(
            {
                "path": page.path,
                "title": page.title,
                "url": page.url,
                "reason": "browser-first source" if browser_first else "weak extracted context",
                "discovered_on": page.discovered_on or None,
                "published_on": page.published_on or None,
                "type": page.note_type,
            }
        )
    return queue


def parse_iso_date(value: str) -> date | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1)).date()
    except ValueError:
        return None


def write_cache(
    vault_root: Path,
    pages: list[Page],
    claims: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    browser_queue: list[dict[str, Any]],
) -> None:
    cache_dir = vault_root / CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    digest = {
        "schema": "my-vault-agent-digest-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(pages),
        "claim_count": len(claims),
        "source_count": len(sources),
        "browser_queue_count": len(browser_queue),
        "pages": [
            {
                "path": page.path,
                "title": page.title,
                "type": page.note_type,
                "url": page.url or None,
                "status": page.status or None,
                "priority": page.priority or None,
                "published_on": page.published_on or None,
                "discovered_on": page.discovered_on or None,
                "deadline": page.deadline or None,
                "tags": page.tags,
                "topics": page.topics,
                "summary": page.summary,
                "why_saved": page.why_saved or None,
                "mtime": page.mtime,
                "size": page.size,
                "sha256": page.sha256,
            }
            for page in pages
        ],
    }
    write_json(cache_dir / "agent-digest.json", digest)
    write_jsonl(vault_root / CLAIMS_JSONL, claims)
    write_jsonl(vault_root / SOURCE_INDEX_JSONL, sources)
    write_jsonl(vault_root / BROWSER_QUEUE_JSONL, browser_queue)
    write_json(
        vault_root / MANIFEST_JSON,
        {
            "schema": "my-vault-cache-manifest-v1",
            "generated_at": digest["generated_at"],
            "files": {page.path: {"sha256": page.sha256, "mtime": page.mtime, "size": page.size} for page in pages},
        },
    )


def build_search_index(vault_root: Path, pages: list[Page]) -> None:
    db_path = vault_root / SEARCH_SQLITE
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DROP TABLE IF EXISTS documents")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
              path TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              type TEXT NOT NULL,
              url TEXT,
              published_on TEXT,
              discovered_on TEXT,
              deadline TEXT,
              priority TEXT,
              tags TEXT,
              topics TEXT,
              summary TEXT,
              body TEXT,
              mtime REAL,
              sha256 TEXT
            )
            """
        )
        conn.execute("DROP TABLE IF EXISTS documents_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE documents_fts USING fts5(path UNINDEXED, title, summary, body, tags, topics)"
        )
        conn.execute("DELETE FROM documents")
        for page in pages:
            body = strip_frontmatter(page.text)
            conn.execute(
                """
                INSERT INTO documents (
                  path, title, type, url, published_on, discovered_on, deadline, priority,
                  tags, topics, summary, body, mtime, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page.path,
                    page.title,
                    page.note_type,
                    page.url,
                    page.published_on,
                    page.discovered_on,
                    page.deadline,
                    page.priority,
                    json.dumps(page.tags),
                    json.dumps(page.topics),
                    page.summary,
                    body,
                    page.mtime,
                    page.sha256,
                ),
            )
            conn.execute(
                "INSERT INTO documents_fts(path, title, summary, body, tags, topics) VALUES (?, ?, ?, ?, ?, ?)",
                (page.path, page.title, page.summary, body[:8000], " ".join(page.tags), " ".join(page.topics)),
            )
        conn.commit()
    finally:
        conn.close()


def write_reports(vault_root: Path, pages: list[Page], claims: list[dict[str, Any]]) -> None:
    reports_dir = vault_root / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)

    low_conf = [claim for claim in claims if float(claim.get("confidence") or 0) < 0.6]
    stale = [claim for claim in claims if claim.get("freshness") in {"stale", "unknown"}]
    missing_url = [page for page in pages if page.note_type in {"article", "tweet", "job", "resource"} and not page.url]
    open_questions = collect_open_questions(pages)

    health_lines = [
        "# Claim Health",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Pages indexed: {len(pages)}",
        f"- Claims indexed: {len(claims)}",
        f"- Low-confidence claims: {len(low_conf)}",
        f"- Stale or unknown claims: {len(stale)}",
        f"- URL-backed item notes missing URL: {len(missing_url)}",
        "",
        "## Low Confidence",
        "",
        *[f"- `{claim['page_path']}`: {claim['text']}" for claim in low_conf[:30]],
        "" if low_conf else "- None.",
        "",
        "## Stale Or Unknown",
        "",
        *[f"- `{claim['page_path']}`: {claim['text']}" for claim in stale[:30]],
        "" if stale else "- None.",
        "",
        "## Missing URLs",
        "",
        *[f"- `{page.path}`: {page.title}" for page in missing_url[:30]],
        "" if missing_url else "- None.",
        "",
    ]
    (vault_root / CLAIM_HEALTH_REPORT).write_text("\n".join(health_lines), encoding="utf-8")

    question_lines = [
        "# Open Questions",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Open questions detected: {len(open_questions)}",
        "",
        *[f"- `{path}`: {question}" for path, question in open_questions[:80]],
        "" if open_questions else "- None.",
        "",
    ]
    (vault_root / OPEN_QUESTIONS_REPORT).write_text("\n".join(question_lines), encoding="utf-8")


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return "", text
    return text[4:end], text[end + 5 :]


def strip_frontmatter(text: str) -> str:
    return split_frontmatter(text)[1].strip()


def parse_frontmatter(frontmatter: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key = ""
    for raw_line in frontmatter.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and current_key:
            result.setdefault(current_key, [])
            if isinstance(result[current_key], list):
                result[current_key].append(parse_value(item.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            current_key = ""
            continue
        key, raw_value = match.group(1), match.group(2).strip()
        if raw_value == "":
            result[key] = []
            current_key = key
            continue
        result[key] = parse_value(raw_value)
        current_key = ""
    return result


def parse_value(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_value(part.strip()) for part in inner.split(",") if part.strip()]
    return value


def as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def first_heading(body: str) -> str:
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+)$", line.strip())
        if match:
            return match.group(1).strip()
    return ""


def infer_type(relative_path: str) -> str:
    parts = relative_path.split("/")
    if len(parts) >= 2 and parts[0] == "items":
        return parts[1].rstrip("s")
    if parts and parts[0] in {"topics", "projects", "dashboards", "outputs"}:
        return parts[0].rstrip("s")
    return "note"


def extract_summary(body: str, frontmatter: dict[str, Any]) -> str:
    for key in ["summary", "description", "why_saved", "source_excerpt"]:
        value = str(frontmatter.get(key) or "").strip()
        if value:
            return truncate(clean_line(value), 420)
    lines = [
        clean_line(line)
        for line in strip_frontmatter(body).splitlines()
        if clean_line(line) and not clean_line(line).startswith("#")
    ]
    return truncate(" ".join(lines[:4]), 420)


def extract_claim_lines(text: str) -> list[str]:
    body = strip_frontmatter(text)
    lines = []
    capture = False
    for raw_line in body.splitlines():
        line = clean_line(raw_line)
        if re.match(r"^#{1,3}\s+(claims|known facts|facts|decisions|verdict|notes)\b", line, re.I):
            capture = True
            continue
        if capture and line.startswith("#"):
            capture = False
        if not capture:
            continue
        line = re.sub(r"^[-*]\s+", "", line).strip()
        if len(line) >= 24 and not line.startswith("![]("):
            lines.append(truncate(line, 320))
        if len(lines) >= 5:
            break
    return lines


def collect_open_questions(pages: list[Page]) -> list[tuple[str, str]]:
    questions: list[tuple[str, str]] = []
    for page in pages:
        for line in page.text.splitlines():
            stripped = clean_line(line)
            if stripped.endswith("?") and 12 <= len(stripped) <= 240:
                questions.append((page.path, stripped.lstrip("-* ").strip()))
                if len(questions) >= 100:
                    return questions
    return questions


def confidence_for_field(page: Page, key: str) -> float:
    if key in {"url", "deadline", "published_on", "discovered_on"}:
        confidence = str(page.frontmatter.get("date_confidence") or "").strip()
        if confidence == "unknown":
            return 0.45
        if confidence == "estimated":
            return 0.65
    return 0.86


def newest_date(*values: str) -> str:
    candidates = [value[:10] for value in values if re.match(r"^\d{4}-\d{2}-\d{2}", str(value))]
    return max(candidates) if candidates else ""


def freshness_label(value: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value or ""):
        return "unknown"
    try:
        then = datetime.fromisoformat(value).date()
        days = (datetime.now(timezone.utc).date() - then).days
    except ValueError:
        return "unknown"
    if days > 365:
        return "stale"
    if days > 120:
        return "aging"
    return "fresh"


def clean_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def truncate(text: str, limit: int) -> str:
    text = clean_line(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
