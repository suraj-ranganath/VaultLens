#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import urllib.request
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
EMBEDDING_CACHE_SQLITE = CACHE_DIR / "embedding-cache.sqlite"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
CLAIM_HEALTH_REPORT = REPORTS_DIR / "claim-health.md"
OPEN_QUESTIONS_REPORT = REPORTS_DIR / "open-questions.md"
CONTRADICTIONS_REPORT = REPORTS_DIR / "contradictions.md"
LOW_CONFIDENCE_REPORT = REPORTS_DIR / "low-confidence.md"
STALE_CLAIMS_REPORT = REPORTS_DIR / "stale-claims.md"
MEMORY_PALACE_REPORT = REPORTS_DIR / "memory-palace.md"


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
    claim_health = build_claim_health(pages, claims)
    sources = build_source_index(pages)
    browser_queue = build_browser_queue(pages)
    write_cache(vault_root, pages, claims, sources, browser_queue, claim_health)
    build_search_index(vault_root, pages)
    write_reports(vault_root, pages, claims, claim_health)

    if not args.quiet:
        print(
            json.dumps(
                {
                    "ok": True,
                    "pages": len(pages),
                    "claims": len(claims),
                    "sources": len(sources),
                    "browser_queue": len(browser_queue),
                    "claim_health": claim_health["summary"],
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
            "claim_key": claim_key(page, source_field, text),
            "last_touched": last_touched,
            "freshness": freshness_label(last_touched),
            "evidence_count": len(evidence),
        }


def claim_key(page: Page, source_field: str, text: str) -> str:
    if not source_field.startswith("body:"):
        return f"{page.note_type}:{normalize_key(page.title)}:{source_field}"
    return f"body:{normalize_key(text)[:80]}"


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def build_claim_health(pages: list[Page], claims: list[dict[str, Any]]) -> dict[str, Any]:
    low_confidence = [claim for claim in claims if float(claim.get("confidence") or 0) < 0.6]
    stale = [claim for claim in claims if claim.get("freshness") in {"stale", "unknown"}]
    missing_evidence = [claim for claim in claims if int(claim.get("evidence_count") or 0) == 0]
    open_questions = [{"path": path, "question": question} for path, question in collect_open_questions(pages)]
    contradiction_clusters = build_contradiction_clusters(claims)
    stale_pages = [
        {
            "path": page.path,
            "title": page.title,
            "type": page.note_type,
            "freshness": freshness_label(newest_date(page.published_on, page.discovered_on) or ""),
            "last_touched": newest_date(page.published_on, page.discovered_on) or None,
        }
        for page in pages
        if page.note_type not in {"dashboard", "output"} and freshness_label(newest_date(page.published_on, page.discovered_on) or "") in {"stale", "unknown"}
    ]
    duplicate_urls = build_duplicate_url_clusters(pages)
    return {
        "schema": "my-vault-claim-health-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "low_confidence_claims": len(low_confidence),
            "stale_or_unknown_claims": len(stale),
            "missing_evidence_claims": len(missing_evidence),
            "open_questions": len(open_questions),
            "contradiction_clusters": len(contradiction_clusters),
            "stale_or_unknown_pages": len(stale_pages),
            "duplicate_url_clusters": len(duplicate_urls),
        },
        "low_confidence": low_confidence,
        "stale": stale,
        "missing_evidence": missing_evidence,
        "open_questions": open_questions,
        "contradiction_clusters": contradiction_clusters,
        "stale_pages": stale_pages,
        "duplicate_urls": duplicate_urls,
    }


def build_contradiction_clusters(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        key = str(claim.get("claim_key") or "")
        if not key or key.startswith("body:"):
            continue
        by_key.setdefault(key, []).append(claim)
    clusters = []
    for key, entries in by_key.items():
        values = {normalize_claim_value(str(entry.get("text") or "")) for entry in entries}
        page_paths = {str(entry.get("page_path") or "") for entry in entries}
        if len(entries) < 2 or len(values) < 2 or len(page_paths) < 2:
            continue
        clusters.append(
            {
                "key": key,
                "claim_count": len(entries),
                "page_count": len(page_paths),
                "entries": [
                    {
                        "claim_id": entry.get("id"),
                        "page_path": entry.get("page_path"),
                        "page_title": entry.get("page_title"),
                        "text": entry.get("text"),
                        "confidence": entry.get("confidence"),
                        "freshness": entry.get("freshness"),
                    }
                    for entry in entries[:12]
                ],
            }
        )
    return sorted(clusters, key=lambda item: (-int(item["claim_count"]), str(item["key"])))[:100]


def normalize_claim_value(text: str) -> str:
    text = re.sub(r"^[^:]+:\s+[^:]+ is\s+", "", text, flags=re.I)
    return normalize_key(text)


def build_duplicate_url_clusters(pages: list[Page]) -> list[dict[str, Any]]:
    by_url: dict[str, list[Page]] = {}
    for page in pages:
        if page.url:
            by_url.setdefault(page.url.strip().lower(), []).append(page)
    clusters = []
    for url, matches in by_url.items():
        if len(matches) < 2:
            continue
        clusters.append(
            {
                "url": url,
                "count": len(matches),
                "pages": [{"path": page.path, "title": page.title, "type": page.note_type} for page in matches],
            }
        )
    return sorted(clusters, key=lambda item: (-int(item["count"]), str(item["url"])))[:100]


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
    claim_health: dict[str, Any],
) -> None:
    cache_dir = vault_root / CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    digest = {
        "schema": "my-vault-agent-digest-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(pages),
        "claim_count": len(claims),
        "claim_health": claim_health["summary"],
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
    write_json(cache_dir / "claim-health.json", claim_health)
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
    fd, tmp_name = tempfile.mkstemp(prefix=f"{db_path.name}.", suffix=".tmp", dir=db_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
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
        conn.execute("DROP TABLE IF EXISTS document_embeddings")
        conn.execute(
            """
            CREATE TABLE document_embeddings (
              path TEXT PRIMARY KEY,
              model TEXT NOT NULL,
              text_hash TEXT NOT NULL,
              embedding TEXT NOT NULL
            )
            """
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
        build_embedding_index(vault_root, conn, pages)
        conn.commit()
    finally:
        conn.close()
    atomic_replace_sqlite(tmp_path, db_path)


def build_embedding_index(vault_root: Path, conn: sqlite3.Connection, pages: list[Page]) -> None:
    if os.environ.get("VAULT_EMBEDDINGS_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY") or ""
    if not api_key.strip():
        return
    model = os.environ.get("VAULT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip() or DEFAULT_EMBEDDING_MODEL
    cache_db = vault_root / EMBEDDING_CACHE_SQLITE
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    cache_conn = sqlite3.connect(cache_db)
    try:
        cache_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
              model TEXT NOT NULL,
              text_hash TEXT NOT NULL,
              embedding TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (model, text_hash)
            )
            """
        )
        entries = [
            {
                "path": page.path,
                "text": embedding_text(page),
            }
            for page in pages
        ]
        entries = [{**entry, "hash": hashlib.sha256(entry["text"].encode("utf-8")).hexdigest()} for entry in entries if entry["text"]]
        cached = load_embedding_cache(cache_conn, model, [entry["hash"] for entry in entries])
        missing = [entry for entry in entries if entry["hash"] not in cached]
        for batch in chunks(missing, 32):
            try:
                embeddings = fetch_openai_embeddings(api_key=api_key, model=model, inputs=[entry["text"] for entry in batch])
            except Exception:
                break
            now = datetime.now(timezone.utc).isoformat()
            for entry, embedding in zip(batch, embeddings):
                cached[entry["hash"]] = embedding
                cache_conn.execute(
                    """
                    INSERT INTO embedding_cache (model, text_hash, embedding, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(model, text_hash) DO UPDATE SET
                      embedding=excluded.embedding,
                      updated_at=excluded.updated_at
                    """,
                    (model, entry["hash"], json.dumps(embedding), now),
                )
            cache_conn.commit()
        for entry in entries:
            embedding = cached.get(entry["hash"])
            if not embedding:
                continue
            conn.execute(
                "INSERT INTO document_embeddings(path, model, text_hash, embedding) VALUES (?, ?, ?, ?)",
                (entry["path"], model, entry["hash"], json.dumps(embedding)),
            )
    finally:
        cache_conn.close()


def embedding_text(page: Page) -> str:
    body = strip_frontmatter(page.text)
    text = "\n".join(
        [
            page.title,
            f"type: {page.note_type}",
            f"tags: {', '.join(page.tags)}",
            f"topics: {', '.join(page.topics)}",
            page.summary,
            page.why_saved,
            body[:2500],
        ]
    )
    return truncate(text, 3600)


def load_embedding_cache(conn: sqlite3.Connection, model: str, hashes: list[str]) -> dict[str, list[float]]:
    if not hashes:
        return {}
    out: dict[str, list[float]] = {}
    unique = list(dict.fromkeys(hashes))
    for batch in chunks(unique, 400):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT text_hash, embedding FROM embedding_cache WHERE model = ? AND text_hash IN ({placeholders})",
            [model, *batch],
        ).fetchall()
        for text_hash, raw_embedding in rows:
            try:
                embedding = json.loads(raw_embedding)
            except Exception:
                continue
            if isinstance(embedding, list):
                out[str(text_hash)] = [float(value) for value in embedding]
    return out


def fetch_openai_embeddings(*, api_key: str, model: str, inputs: list[str]) -> list[list[float]]:
    payload = json.dumps({"model": model, "input": inputs}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read().decode("utf-8"))
    data = body.get("data") or []
    by_index = sorted((item for item in data if isinstance(item, dict)), key=lambda item: int(item.get("index") or 0))
    return [[float(value) for value in item.get("embedding", [])] for item in by_index]


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def write_reports(vault_root: Path, pages: list[Page], claims: list[dict[str, Any]], claim_health: dict[str, Any]) -> None:
    reports_dir = vault_root / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)

    low_conf = list(claim_health.get("low_confidence") or [])
    stale = list(claim_health.get("stale") or [])
    missing_url = [page for page in pages if page.note_type in {"article", "tweet", "job", "resource"} and not page.url]
    open_questions = [(item["path"], item["question"]) for item in claim_health.get("open_questions") or []]
    contradictions = list(claim_health.get("contradiction_clusters") or [])
    stale_pages = list(claim_health.get("stale_pages") or [])
    duplicate_urls = list(claim_health.get("duplicate_urls") or [])
    summary = claim_health.get("summary") or {}

    health_lines = [
        "# Claim Health",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Pages indexed: {len(pages)}",
        f"- Claims indexed: {len(claims)}",
        f"- Low-confidence claims: {summary.get('low_confidence_claims', len(low_conf))}",
        f"- Stale or unknown claims: {summary.get('stale_or_unknown_claims', len(stale))}",
        f"- Contradiction clusters: {summary.get('contradiction_clusters', len(contradictions))}",
        f"- Open questions: {summary.get('open_questions', len(open_questions))}",
        f"- Duplicate URL clusters: {summary.get('duplicate_url_clusters', len(duplicate_urls))}",
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
    atomic_write_text(vault_root / CLAIM_HEALTH_REPORT, "\n".join(health_lines) + "\n")

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
    atomic_write_text(vault_root / OPEN_QUESTIONS_REPORT, "\n".join(question_lines) + "\n")

    contradiction_lines = [
        "# Contradictions",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Contradiction clusters: {len(contradictions)}",
        f"- Duplicate URL clusters: {len(duplicate_urls)}",
        "",
        "## Claim Conflicts",
        "",
    ]
    if contradictions:
        for cluster in contradictions[:40]:
            contradiction_lines.append(f"- `{cluster['key']}` ({cluster['claim_count']} claims across {cluster['page_count']} pages)")
            for entry in cluster.get("entries", [])[:4]:
                contradiction_lines.append(f"  - `{entry.get('page_path')}`: {entry.get('text')}")
    else:
        contradiction_lines.append("- None.")
    contradiction_lines.extend(["", "## Duplicate URLs", ""])
    if duplicate_urls:
        for cluster in duplicate_urls[:40]:
            contradiction_lines.append(f"- {cluster['url']} ({cluster['count']} notes)")
            for page in cluster.get("pages", [])[:4]:
                contradiction_lines.append(f"  - `{page.get('path')}`: {page.get('title')}")
    else:
        contradiction_lines.append("- None.")
    atomic_write_text(vault_root / CONTRADICTIONS_REPORT, "\n".join(contradiction_lines) + "\n")

    low_conf_lines = [
        "# Low Confidence",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Low-confidence claims: {len(low_conf)}",
        "",
        *[f"- `{claim['page_path']}` ({claim.get('confidence')}): {claim['text']}" for claim in low_conf[:100]],
        "" if low_conf else "- None.",
        "",
    ]
    atomic_write_text(vault_root / LOW_CONFIDENCE_REPORT, "\n".join(low_conf_lines) + "\n")

    stale_lines = [
        "# Stale Claims",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Stale or unknown claims: {len(stale)}",
        f"- Stale or unknown pages: {len(stale_pages)}",
        "",
        "## Claims",
        "",
        *[f"- `{claim['page_path']}` ({claim.get('freshness')}): {claim['text']}" for claim in stale[:100]],
        "" if stale else "- None.",
        "",
        "## Pages",
        "",
        *[f"- `{page['path']}` ({page.get('freshness')}): {page['title']}" for page in stale_pages[:100]],
        "" if stale_pages else "- None.",
        "",
    ]
    atomic_write_text(vault_root / STALE_CLAIMS_REPORT, "\n".join(stale_lines) + "\n")

    atomic_write_text(vault_root / MEMORY_PALACE_REPORT, render_memory_palace(pages, claims, claim_health))


def render_memory_palace(pages: list[Page], claims: list[dict[str, Any]], claim_health: dict[str, Any]) -> str:
    by_type: dict[str, list[Page]] = {}
    for page in pages:
        by_type.setdefault(page.note_type, []).append(page)
    lines = [
        "# Vault Memory Palace",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Pages: {len(pages)}",
        f"- Claims: {len(claims)}",
        f"- Health: {json.dumps(claim_health.get('summary') or {}, sort_keys=True)}",
        "",
    ]
    for note_type, matches in sorted(by_type.items(), key=lambda item: (-len(item[1]), item[0])):
        claim_count = sum(1 for claim in claims if claim.get("page_type") == note_type)
        lines.extend([f"## {note_type.title()} ({len(matches)})", "", f"- Claims: {claim_count}"])
        for page in sorted(matches, key=lambda page: (page.discovered_on or page.published_on or "", page.title), reverse=True)[:12]:
            date_label = page.deadline or page.discovered_on or page.published_on or "undated"
            source = page.url or page.path
            lines.append(f"- {date_label}: [{page.title}]({source})")
        lines.append("")
    return "\n".join(lines)


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
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    atomic_write_text(path, text)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def atomic_replace_sqlite(tmp_path: Path, db_path: Path) -> None:
    for suffix in ["-wal", "-shm"]:
        try:
            Path(f"{tmp_path}{suffix}").unlink()
        except OSError:
            pass
    for suffix in ["-wal", "-shm"]:
        try:
            Path(f"{db_path}{suffix}").unlink()
        except OSError:
            pass
    os.replace(tmp_path, db_path)


if __name__ == "__main__":
    main()
