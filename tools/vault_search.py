#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_DIR = Path(".vault") / "cache"
SEARCH_SQLITE = CACHE_DIR / "search.sqlite"
DIGEST_JSON = CACHE_DIR / "agent-digest.json"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def main() -> None:
    parser = argparse.ArgumentParser(description="Search the compiled vault cache.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search")
    search.add_argument("--vault-root", type=Path, default=Path.cwd())
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=12)
    search.add_argument("--compile", action="store_true", default=True)
    search.add_argument("--no-compile", dest="compile", action="store_false")
    search.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args()
    if args.command == "search":
        vault_root = args.vault_root.resolve()
        if args.compile:
            ensure_compiled(vault_root)
        results = search_vault(vault_root, args.query, args.limit)
        print(json.dumps({"ok": True, "query": args.query, "results": results}, indent=2))


def ensure_compiled(vault_root: Path) -> None:
    if (vault_root / SEARCH_SQLITE).exists() and (vault_root / DIGEST_JSON).exists():
        return
    script = vault_root / "tools" / "vault_compile_cache.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--vault-root", str(vault_root), "--quiet"],
        text=True,
        capture_output=True,
        cwd=vault_root,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "vault cache compile failed")


def search_vault(vault_root: Path, query: str, limit: int) -> list[dict[str, Any]]:
    db_path = vault_root / SEARCH_SQLITE
    if not db_path.exists():
        return fallback_digest_search(vault_root, query, limit)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row_limit = max(limit * 4, limit)
        rows = []
        fts_query = build_fts_query(query, "AND")
        if fts_query:
            rows = query_fts(conn, fts_query, row_limit)
            if len(rows) < row_limit:
                seen_paths = {row["path"] for row in rows}
                for row in query_fts(conn, build_fts_query(query, "OR"), row_limit):
                    if row["path"] not in seen_paths:
                        rows.append(row)
                        seen_paths.add(row["path"])
                    if len(rows) >= row_limit:
                        break
        vector_results = query_embeddings(conn, query, row_limit)
    except sqlite3.Error:
        return fallback_digest_search(vault_root, query, limit)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    terms = query_terms(query)
    scored_by_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_score = bm25_rank_to_score(float(row["rank"]))
        recency = temporal_multiplier(row["published_on"] or row["discovered_on"], row["type"])
        priority = priority_multiplier(row["priority"] if "priority" in row.keys() else "")
        title_path = f"{row['title']} {row['path']}".lower()
        title_hits = sum(1 for term in terms if term in title_path)
        title_boost = 1.0 + min(0.8, 0.18 * title_hits)
        score = raw_score * recency * priority * title_boost
        scored_by_path[row["path"]] = {
                "path": row["path"],
                "title": row["title"],
                "type": row["type"],
                "url": row["url"] or None,
                "published_on": row["published_on"] or None,
                "discovered_on": row["discovered_on"] or None,
                "deadline": row["deadline"] or None,
                "score": score,
                "text_score": raw_score,
                "snippet": best_snippet(row["summary"] or row["body"] or "", terms),
                "tags": parse_json_list(row["tags"]),
                "topics": parse_json_list(row["topics"]),
                "retrieval_sources": ["fts"],
            }

    for item in vector_results:
        existing = scored_by_path.get(item["path"])
        if existing:
            existing["score"] = max(float(existing.get("score") or 0), float(existing.get("score") or 0) * 0.65 + item["score"] * 0.55)
            existing["vector_score"] = item["vector_score"]
            existing.setdefault("retrieval_sources", []).append("embedding")
            if not existing.get("snippet") and item.get("snippet"):
                existing["snippet"] = item["snippet"]
            continue
        scored_by_path[item["path"]] = item

    scored = list(scored_by_path.values())

    scored.sort(key=lambda item: item["score"], reverse=True)
    return mmr_rerank(scored, limit=limit, lambda_=0.72)


def query_embeddings(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    if os.environ.get("VAULT_EMBEDDINGS_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return []
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY") or ""
    if not api_key.strip() or not table_exists(conn, "document_embeddings"):
        return []
    model = os.environ.get("VAULT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip() or DEFAULT_EMBEDDING_MODEL
    query_vec = fetch_query_embedding(api_key=api_key, model=model, query=query)
    if not query_vec:
        return []
    rows = conn.execute(
        """
        SELECT d.*, e.embedding
        FROM document_embeddings e
        JOIN documents d ON d.path = e.path
        WHERE e.model = ?
        """,
        (model,),
    ).fetchall()
    results = []
    for row in rows:
        try:
            doc_vec = [float(value) for value in json.loads(row["embedding"])]
        except Exception:
            continue
        vector_score = cosine_similarity(query_vec, doc_vec)
        if not math.isfinite(vector_score):
            continue
        recency = temporal_multiplier(row["published_on"] or row["discovered_on"], row["type"])
        priority = priority_multiplier(row["priority"] if "priority" in row.keys() else "")
        score = vector_score * recency * priority
        results.append(
            {
                "path": row["path"],
                "title": row["title"],
                "type": row["type"],
                "url": row["url"] or None,
                "published_on": row["published_on"] or None,
                "discovered_on": row["discovered_on"] or None,
                "deadline": row["deadline"] or None,
                "score": score,
                "text_score": 0,
                "vector_score": vector_score,
                "snippet": best_snippet(row["summary"] or row["body"] or "", query_terms(query)),
                "tags": parse_json_list(row["tags"]),
                "topics": parse_json_list(row["topics"]),
                "retrieval_sources": ["embedding"],
            }
        )
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
    return row is not None


def fetch_query_embedding(*, api_key: str, model: str, query: str) -> list[float]:
    payload = json.dumps({"model": model, "input": query}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    data = body.get("data") or []
    if not data or not isinstance(data[0], dict):
        return []
    return [float(value) for value in data[0].get("embedding", [])]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def fallback_digest_search(vault_root: Path, query: str, limit: int) -> list[dict[str, Any]]:
    digest_path = vault_root / DIGEST_JSON
    if not digest_path.exists():
        return []
    try:
        pages = json.loads(digest_path.read_text(encoding="utf-8")).get("pages") or []
    except Exception:
        return []
    terms = query_terms(query)
    results = []
    for page in pages:
        haystack = " ".join(
            str(page.get(key) or "")
            for key in ["path", "title", "type", "summary", "why_saved", "tags", "topics"]
        ).lower()
        hits = sum(1 for term in terms if term in haystack)
        if hits == 0:
            continue
        results.append(
            {
                "path": page.get("path"),
                "title": page.get("title"),
                "type": page.get("type"),
                "url": page.get("url"),
                "published_on": page.get("published_on"),
                "discovered_on": page.get("discovered_on"),
                "deadline": page.get("deadline"),
                "score": hits,
                "text_score": hits,
                "snippet": page.get("summary") or "",
                "tags": page.get("tags") or [],
                "topics": page.get("topics") or [],
            }
        )
    results.sort(key=lambda item: item["score"], reverse=True)
    return mmr_rerank(results, limit=limit, lambda_=0.72)


def query_fts(conn: sqlite3.Connection, fts_query: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.*, bm25(documents_fts, 6.0, 4.0, 2.0, 1.0, 1.0) AS rank
        FROM documents_fts
        JOIN documents d ON d.path = documents_fts.path
        WHERE documents_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, limit),
    ).fetchall()


def build_fts_query(raw: str, operator: str) -> str:
    terms = query_terms(raw)
    joiner = f" {operator if operator in {'AND', 'OR'} else 'AND'} "
    return joiner.join(f'"{term.replace(chr(34), "")}"' for term in terms)


def query_terms(raw: str) -> list[str]:
    stop = {
        "about",
        "after",
        "again",
        "also",
        "and",
        "are",
        "based",
        "can",
        "could",
        "does",
        "for",
        "from",
        "have",
        "how",
        "into",
        "just",
        "know",
        "like",
        "more",
        "that",
        "the",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
        "would",
        "you",
        "your",
    }
    words = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", raw.lower())
    return list(dict.fromkeys(word for word in words if word not in stop))[:16]


def bm25_rank_to_score(rank: float) -> float:
    if not math.isfinite(rank):
        return 0.0
    relevance = -rank if rank < 0 else 1 / (1 + rank)
    return relevance / (1 + relevance) if relevance > 1 else relevance


def temporal_multiplier(date_value: str | None, note_type: str | None) -> float:
    if not date_value or note_type in {"topic", "project", "system", "decision"}:
        return 1.0
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(date_value))
    if not match:
        return 1.0
    try:
        then = datetime.fromisoformat(match.group(1)).date()
    except ValueError:
        return 1.0
    days = max(0, (datetime.now(timezone.utc).date() - then).days)
    half_life = 30 if note_type in {"job", "event", "reminder", "opportunity"} else 180
    return max(0.35, math.exp(-(math.log(2) / half_life) * days))


def priority_multiplier(priority: str) -> float:
    return {
        "critical": 1.4,
        "high": 1.22,
        "medium": 1.0,
        "low": 0.92,
    }.get(str(priority or "").lower(), 1.0)


def best_snippet(text: str, terms: list[str]) -> str:
    lines = [clean_line(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    lowered_terms = [term.lower() for term in terms]
    best = max(lines[:80], key=lambda line: sum(term in line.lower() for term in lowered_terms))
    return truncate(best, 500)


def mmr_rerank(results: list[dict[str, Any]], limit: int, lambda_: float) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    remaining = list(results)
    while remaining and len(selected) < limit:
        best_idx = 0
        best_score = -10**9
        for idx, item in enumerate(remaining):
            relevance = float(item.get("score") or 0)
            similarity = max((jaccard(tokens(item), tokens(chosen)) for chosen in selected), default=0.0)
            mmr = lambda_ * relevance - (1 - lambda_) * similarity
            if mmr > best_score:
                best_score = mmr
                best_idx = idx
        selected.append(remaining.pop(best_idx))
    return selected


def tokens(item: dict[str, Any]) -> set[str]:
    text = " ".join(str(item.get(key) or "") for key in ["title", "snippet", "tags", "topics"])
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def parse_json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
        return [str(item) for item in value] if isinstance(value, list) else []
    except Exception:
        return []


def clean_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def truncate(text: str, limit: int) -> str:
    text = clean_line(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


if __name__ == "__main__":
    main()
