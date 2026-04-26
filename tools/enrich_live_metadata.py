#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
import warnings

from x_content import fetch_x_post, is_x_post_url

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)


CURRENT_DATE = date.today()
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vault-enricher/1.0)"}
REQUEST_TIMEOUT_SECONDS = 10
FETCH_WORKERS = 12
OFFICIAL_JOB_HOST_HINTS = (
    "greenhouse.io",
    "ashbyhq.com",
    "lever.co",
    "myworkdayjobs.com",
    "oraclecloud.com",
    "amazon.jobs",
    "icims.com",
    "eightfold.ai",
    "workstory.io",
    "careers.",
    "jobs.",
)
LOW_SIGNAL_TITLE_PATTERNS = (
    "just a moment",
    "checking your connection",
    "checking your browser",
    "access denied",
    "page not found",
    "sign up | linkedin",
    "linkedin login",
    "file not found",
)
LOW_SIGNAL_BLOCK_PATTERNS = (
    "navigation menu",
    "you must be signed in to change notification settings",
    "change notification settings",
    "directory actions",
    "more options",
    "files expand file tree",
    "enterprise platform",
    "customer support",
    "community forum",
    "pricing",
    "sign in",
    "log in",
    "instant dev environments",
    "write better code with ai",
)


def load_note(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---\n"):
        raise ValueError(f"{path} does not start with frontmatter")
    _, rest = text.split("---\n", 1)
    frontmatter, body = rest.split("\n---\n", 1)
    data = yaml.safe_load(frontmatter) or {}
    return data, body.lstrip("\n")


def dump_frontmatter(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()


def iter_note_paths(folder: Path) -> list[Path]:
    return [path for path in sorted(folder.glob("*.md")) if not path.name.startswith("._")]


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme or "https", parsed.netloc.lower(), parsed.path, "", parsed.query, ""))


def url_match_key(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    return urlunparse((parsed.scheme or "https", parsed.netloc.lower().removeprefix("www."), parsed.path.rstrip("/"), "", "", ""))


def prettify_text(text: str) -> str:
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\bR(\d{4,})\b", "", text).strip()
    words = []
    for token in text.split():
        if token.upper() in {"AI", "ML", "LLM", "NLP", "RL", "GPU", "IOS"}:
            words.append(token.upper())
        elif re.fullmatch(r"[A-Z]{2,5}", token):
            words.append(token)
        else:
            words.append(token.capitalize())
    return " ".join(words).strip()


def clean_title(title: str) -> str:
    title = str(title).encode("utf-8", "ignore").decode("utf-8", "ignore")
    title = BeautifulSoup(title, "html.parser").get_text(" ", strip=True)
    title = re.sub(r"\s+", " ", title).strip(" -|")
    return title


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
    if len(text) <= 180:
        return [text]
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    cleaned = [clean_text(part) for part in parts if clean_text(part)]
    if cleaned:
        return cleaned
    return [text]


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


def parse_structured_date(raw: Any) -> str:
    text = clean_text(raw)
    if not text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    match = re.search(r"\d{4}/\d{2}/\d{2}", text)
    if match:
        return match.group(0).replace("/", "-")
    return text[:10]


def is_low_signal_title(title: str) -> bool:
    lowered = clean_title(title).lower()
    if not lowered:
        return True
    return any(pattern in lowered for pattern in LOW_SIGNAL_TITLE_PATTERNS)


def is_placeholder_title(title: str) -> bool:
    cleaned = clean_title(title)
    if is_low_signal_title(cleaned):
        return True
    if re.fullmatch(r"\d+", cleaned):
        return True
    return False


def is_low_signal_block(text: str) -> bool:
    lowered = clean_text(text).lower()
    if not lowered:
        return True
    if any(pattern in lowered for pattern in LOW_SIGNAL_BLOCK_PATTERNS):
        return True
    if lowered.startswith("platform ai code creation"):
        return True
    return False


def fallback_title_from_url(item_type: str, url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    parts = [part for part in parsed.path.split("/") if part]
    if item_type == "article":
        if parts:
            return prettify_text(parts[-1])
        return f"Article from {prettify_text(host.split('.')[0])}"
    if item_type == "resource":
        if "github.com" in host and len(parts) >= 2:
            return f"GitHub repo {parts[0]}/{parts[1]}"
        if "linkedin.com" in host and parts and parts[0] == "posts":
            handle = parts[1] if len(parts) > 1 else "unknown"
            return f"LinkedIn post from {prettify_text(handle)}"
        if "linkedin.com" in host and "in/" in parsed.path:
            return f"LinkedIn profile {prettify_text(parts[-1])}"
        if parts:
            return prettify_text(parts[-1])
        return f"Resource from {prettify_text(host.split('.')[0])}"
    if item_type == "event":
        if parts:
            return prettify_text(parts[-1])
        return f"Event from {prettify_text(host.split('.')[0])}"
    if item_type == "opportunity":
        if "linkedin.com" in host and parts and parts[0] == "posts":
            handle = parts[1] if len(parts) > 1 else "unknown"
            return f"LinkedIn opportunity post from {prettify_text(handle)}"
        if parts:
            return prettify_text(parts[-1])
        return f"Opportunity from {prettify_text(host.split('.')[0])}"
    return prettify_text(parts[-1]) if parts else prettify_text(host.split(".")[0])


def parse_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue

        stack = [parsed]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if "@type" in current:
                    found.append(current)
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
    return found


def select_json_ld_object(json_ld: list[dict[str, Any]], candidates: list[str]) -> dict[str, Any] | None:
    lowered_candidates = {candidate.lower() for candidate in candidates}
    for obj in json_ld:
        obj_type = obj.get("@type")
        if isinstance(obj_type, list):
            types = {str(value).lower() for value in obj_type}
        else:
            types = {str(obj_type).lower()}
        if types & lowered_candidates:
            return obj
    return None


def extract_page_text_blocks(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    root = soup.find("main") or soup.find("article") or soup.body or soup
    working = BeautifulSoup(str(root), "html.parser")
    for tag_name in ["script", "style", "noscript", "svg", "form"]:
        for tag in working.find_all(tag_name):
            tag.decompose()
    for selector in ["nav", "footer", "header", "aside"]:
        for tag in working.select(selector):
            tag.decompose()

    headings_raw: list[str] = []
    for tag in working.find_all(["h1", "h2", "h3"], limit=60):
        text = clean_text(tag.get_text(" ", strip=True))
        if 4 <= len(text) <= 120 and not is_low_signal_title(text) and not is_low_signal_block(text):
            headings_raw.append(text)

    blocks_raw: list[str] = []
    for tag in working.find_all(["p", "li"], limit=320):
        text = clean_text(tag.get_text(" ", strip=True))
        if len(text) >= 40 and not is_low_signal_title(text) and not is_low_signal_block(text):
            blocks_raw.append(text)

    headings = dedupe_lines(headings_raw, max_items=12, min_length=4, limit=120)
    blocks = dedupe_lines(blocks_raw, max_items=80, min_length=40, limit=260)
    return headings, blocks


def pick_keyword_blocks(blocks: list[str], keywords: list[str], *, limit: int = 2) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    for block in blocks:
        lowered = block.lower()
        score = sum(1 for keyword in keywords if keyword in lowered)
        if score:
            ranked.append((score, -len(block), block))
    ranked.sort(reverse=True)
    return dedupe_lines([block for _, _, block in ranked], max_items=limit, min_length=40, limit=220)


def extract_existing_section_lines(body: str, heading: str) -> list[str]:
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


def host_from_url(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def is_recent(data: dict[str, Any], cutoff: date) -> bool:
    raw = data.get("discovered_on") or ""
    try:
        discovered = datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return False
    return discovered >= cutoff


def extract_jobposting_metadata(obj: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    title = clean_title(obj.get("title") or "")
    if title:
        meta["role"] = title
    org = obj.get("hiringOrganization") or {}
    if isinstance(org, dict):
        company = clean_title(org.get("name") or "")
        if company:
            meta["company"] = company
    if obj.get("datePosted"):
        meta["posted_on"] = parse_structured_date(obj["datePosted"])
        meta["date_confidence"] = "exact"
    if obj.get("validThrough"):
        meta["deadline"] = parse_structured_date(obj["validThrough"])
        meta["deadline_type"] = "explicit"
    emp = obj.get("employmentType")
    if emp:
        meta["employment_type"] = str(emp).replace("_", " ").replace("-", " ").title()
    location = obj.get("jobLocation")
    if isinstance(location, list) and location:
        location = location[0]
    if isinstance(location, dict):
        address = location.get("address") or {}
        if isinstance(address, dict):
            country = address.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name") or country.get("addressCountry")
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                country,
            ]
            loc = ", ".join(part for part in parts if part)
            if loc:
                meta["location"] = loc
    return meta


def extract_event_metadata(obj: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    title = clean_title(obj.get("name") or obj.get("title") or "")
    if title:
        meta["title"] = title
    if obj.get("startDate"):
        meta["deadline"] = parse_structured_date(obj["startDate"])
    return meta


def extract_article_metadata(obj: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    title = clean_title(obj.get("headline") or obj.get("name") or obj.get("title") or "")
    if title:
        meta["title"] = title
    if obj.get("datePublished"):
        meta["published_on"] = parse_structured_date(obj["datePublished"])
    return meta


def build_job_context(meta: dict[str, Any], job_obj: dict[str, Any] | None, headings: list[str], blocks: list[str]) -> tuple[list[str], list[str]]:
    context_candidates: list[str] = []
    signal_candidates: list[str] = []

    if meta.get("page_description"):
        for sentence in split_sentences(meta["page_description"])[:2]:
            context_candidates.append(f"Role summary: {sentence}")

    if job_obj:
        for label, key in [
            ("Role scope", "description"),
            ("Responsibilities", "responsibilities"),
            ("Requirements", "qualifications"),
            ("Requirements", "skills"),
            ("Requirements", "experienceRequirements"),
        ]:
            value = clean_text(job_obj.get(key) or "")
            if value:
                sentences = split_sentences(value)
                if sentences:
                    target = context_candidates if label == "Role scope" else signal_candidates
                    target.append(f"{label}: {sentences[0]}")

    team_blocks = pick_keyword_blocks(blocks, ["about us", "about the team", "our team", "mission", "we are"], limit=1)
    responsibility_blocks = pick_keyword_blocks(
        blocks,
        ["responsibilities", "what you'll do", "you will", "you'll", "build", "design", "develop", "work with"],
        limit=2,
    )
    requirement_blocks = pick_keyword_blocks(
        blocks,
        ["requirements", "qualifications", "experience", "preferred", "minimum", "must have", "nice to have"],
        limit=2,
    )

    for block in team_blocks:
        context_candidates.append(f"Team / company context: {block}")
    for block in responsibility_blocks:
        signal_candidates.append(f"Role focus: {block}")
    for block in requirement_blocks:
        signal_candidates.append(f"Requirements signal: {block}")
    for heading in headings[:4]:
        signal_candidates.append(f"Page section: {heading}")

    context_summary = dedupe_lines(context_candidates, max_items=4, min_length=30, limit=220)
    role_signals = dedupe_lines(signal_candidates, max_items=6, min_length=20, limit=220)
    return context_summary, role_signals


def build_generic_context(
    note: dict[str, Any],
    meta: dict[str, Any],
    article_obj: dict[str, Any] | None,
    event_obj: dict[str, Any] | None,
    headings: list[str],
    blocks: list[str],
) -> tuple[list[str], list[str]]:
    summary_candidates: list[str] = []
    detail_candidates: list[str] = []

    if meta.get("page_description"):
        for sentence in split_sentences(meta["page_description"])[:2]:
            summary_candidates.append(sentence)

    if article_obj:
        description = clean_text(article_obj.get("description") or "")
        if description:
            for sentence in split_sentences(description)[:2]:
                summary_candidates.append(sentence)

    if event_obj:
        description = clean_text(event_obj.get("description") or "")
        if description:
            summary_candidates.append(f"Event context: {split_sentences(description)[0]}")
        if event_obj.get("startDate"):
            summary_candidates.append(f"Event date: {parse_structured_date(event_obj['startDate'])}")
        location = event_obj.get("location")
        if isinstance(location, dict):
            place_name = clean_text(location.get("name") or "")
            if place_name:
                detail_candidates.append(f"Location: {place_name}")

    note_type = note.get("type")
    for block in blocks[:4]:
        summary_candidates.append(block)

    if note_type == "resource":
        repo_blocks = pick_keyword_blocks(blocks, ["install", "usage", "readme", "repository", "benchmark", "guide"], limit=2)
        detail_candidates.extend(repo_blocks)
    elif note_type == "article":
        detail_candidates.extend(blocks[:3])
    elif note_type == "event":
        detail_candidates.extend(pick_keyword_blocks(blocks, ["register", "agenda", "speaker", "meetup", "workshop"], limit=3))
    elif note_type == "opportunity":
        detail_candidates.extend(pick_keyword_blocks(blocks, ["apply", "program", "cohort", "deadline", "eligibility"], limit=3))
    else:
        detail_candidates.extend(blocks[:2])

    for heading in headings[:6]:
        detail_candidates.append(f"Page section: {heading}")

    summary = dedupe_lines(summary_candidates, max_items=4, min_length=30, limit=220)
    details = dedupe_lines(detail_candidates, max_items=6, min_length=20, limit=220)
    return summary, details


def apply_host_specific_context(meta: dict[str, Any], url: str) -> None:
    host = host_from_url(url)
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]

    if "github.com" in host and len(parts) >= 2:
        owner, repo = parts[0], parts[1]
        subpath = "/".join(parts[2:])
        summary = [f"GitHub reference to repository {owner}/{repo}."]
        highlights = []
        if subpath:
            summary.append(f"Saved path within the repo: {subpath}.")
            highlights.append(f"GitHub path: {subpath}")
        meta["context_summary"] = dedupe_lines(summary, max_items=3, min_length=20, limit=220)
        meta["context_highlights"] = dedupe_lines(highlights, max_items=4, min_length=12, limit=220)


def extract_from_page(url: str, note: dict[str, Any]) -> dict[str, Any]:
    if is_x_post_url(url):
        post = fetch_x_post(url)
        if post:
            return post.to_meta()

    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=HEADERS, allow_redirects=True)
    final_url = normalize_url(response.url)
    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, Any] = {
        "status_code": response.status_code,
        "final_url": final_url,
        "page_title": "",
        "page_description": "",
        "closed_hint": False,
        "context_summary": [],
        "context_highlights": [],
    }

    title_candidates = [
        soup.find("meta", attrs={"property": "og:title"}),
        soup.find("meta", attrs={"name": "title"}),
        soup.find("title"),
    ]
    for candidate in title_candidates:
        if not candidate:
            continue
        value = candidate.get("content") if hasattr(candidate, "get") else None
        if not value:
            value = candidate.get_text(" ", strip=True)
        value = clean_title(value or "")
        if value and not is_low_signal_title(value):
            meta["page_title"] = value
            break

    desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if desc and desc.get("content"):
        parsed_desc = clean_title(desc["content"])
        if not is_low_signal_title(parsed_desc):
            meta["page_description"] = parsed_desc

    json_ld = parse_json_ld(soup)
    job_obj = select_json_ld_object(json_ld, ["JobPosting"])
    event_obj = select_json_ld_object(json_ld, ["Event"])
    article_obj = select_json_ld_object(json_ld, ["Article", "NewsArticle", "BlogPosting", "TechArticle"])

    if job_obj:
        meta.update(extract_jobposting_metadata(job_obj))
    if event_obj:
        event_meta = extract_event_metadata(event_obj)
        for key, value in event_meta.items():
            meta.setdefault(key, value)
    if article_obj:
        article_meta = extract_article_metadata(article_obj)
        for key, value in article_meta.items():
            meta.setdefault(key, value)

    published_meta = (
        soup.find("meta", attrs={"property": "article:published_time"})
        or soup.find("meta", attrs={"name": "article:published_time"})
        or soup.find("meta", attrs={"name": "pubdate"})
        or soup.find("meta", attrs={"name": "publish-date"})
        or soup.find("meta", attrs={"name": "date"})
    )
    if published_meta and published_meta.get("content"):
        meta.setdefault("published_on", parse_structured_date(published_meta["content"]))

    host = host_from_url(final_url)
    parsed = urlparse(final_url)
    path_parts = [part for part in parsed.path.split("/") if part]

    if "linkedin.com" in host and meta["page_title"]:
        meta["title"] = meta["page_title"]

    if "jobs.ashbyhq.com" in host and meta["page_title"]:
        match = re.match(r"(.+?)\s+@\s+(.+)$", meta["page_title"])
        if match:
            meta["role"] = clean_title(match.group(1))
            meta["company"] = clean_title(match.group(2))

    if "myworkdayjobs.com" in host:
        if path_parts:
            site = path_parts[0] if path_parts[0].lower() != "job" else ""
            if site:
                meta.setdefault("company", prettify_text(site))
        if len(path_parts) >= 3 and path_parts[-2]:
            loc = path_parts[-2].replace("-", ", ")
            meta.setdefault("location", loc)
        if path_parts:
            raw_role = path_parts[-1]
            raw_role = re.sub(r"_R\d+(?:-\d+)?$", "", raw_role)
            raw_role = raw_role.replace("---", " ")
            raw_role = raw_role.replace("--", " ")
            meta.setdefault("role", prettify_text(raw_role))

    if response.status_code in {404, 410} and (
        any(hint in host for hint in OFFICIAL_JOB_HOST_HINTS)
        or "apply.careers.microsoft.com" in host
    ):
        meta["closed_hint"] = True

    if "greenhouse.io" in host and ("error=true" in final_url or response.status_code >= 400):
        meta["closed_hint"] = True

    if meta["page_title"].lower().startswith("jobs at ") and ("greenhouse.io" in host or "job-boards.greenhouse.io" in host):
        meta["closed_hint"] = True

    if response.status_code >= 400 and meta.get("page_title", "").lower() == "eightfold":
        meta["page_title"] = ""
        meta["closed_hint"] = True

    headings, blocks = extract_page_text_blocks(soup)
    job_like = note.get("type") == "job" or "job-search" in set(note.get("topics") or [])
    if "github.com" in host_from_url(final_url) and response.status_code >= 400:
        headings = []
        blocks = []
    if job_like:
        context_summary, context_highlights = build_job_context(meta, job_obj, headings, blocks)
    else:
        context_summary, context_highlights = build_generic_context(note, meta, article_obj, event_obj, headings, blocks)
    meta["context_summary"] = context_summary
    meta["context_highlights"] = context_highlights
    if not meta["context_summary"] and not meta["context_highlights"]:
        apply_host_specific_context(meta, final_url)
    if not meta["context_summary"]:
        fallback_title = meta.get("page_title") or note.get("title") or fallback_title_from_url(note.get("type", ""), final_url)
        if fallback_title and not is_placeholder_title(fallback_title):
            meta["context_summary"] = [f"Reference page saved as: {clean_title(fallback_title)}."]

    return meta


def update_job_fields(data: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    company = meta.get("company")
    role = meta.get("role")
    if company and company != data.get("company"):
        data["company"] = company
        changes.append(f"company={company}")
    if role and role != data.get("role"):
        data["role"] = role
        changes.append(f"role={role}")
    if company and role:
        title = f"{company} - {role}"
        if title != data.get("title"):
            data["title"] = title
            changes.append("title")
    if meta.get("location") and meta["location"] != data.get("location"):
        data["location"] = meta["location"]
        changes.append("location")
    if meta.get("employment_type") and meta["employment_type"] != data.get("employment_type"):
        data["employment_type"] = meta["employment_type"]
        changes.append("employment_type")
    if meta.get("posted_on") and meta["posted_on"] != data.get("posted_on"):
        data["posted_on"] = meta["posted_on"]
        data["date_confidence"] = meta.get("date_confidence", "exact")
        changes.append(f"posted_on={meta['posted_on']}")
    if meta.get("deadline") and meta["deadline"] != data.get("deadline"):
        data["deadline"] = meta["deadline"]
        data["deadline_type"] = meta.get("deadline_type", "explicit")
        changes.append(f"deadline={meta['deadline']}")
    if meta.get("closed_hint") and data.get("status") != "closed":
        data["status"] = "closed"
        data["application_status"] = "closed"
        changes.append("closed")
    return changes


def update_generic_fields(data: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    if meta.get("title") and meta["title"] != data.get("title"):
        data["title"] = meta["title"]
        changes.append("title")
    elif is_placeholder_title(data.get("title", "")):
        fallback = fallback_title_from_url(data.get("type", ""), data.get("url", ""))
        if fallback and fallback != data.get("title"):
            data["title"] = fallback
            changes.append("title")
    if meta.get("published_on") and meta["published_on"] != data.get("published_on"):
        data["published_on"] = meta["published_on"]
        changes.append(f"published_on={meta['published_on']}")
    if meta.get("deadline") and not data.get("deadline"):
        data["deadline"] = meta["deadline"]
        changes.append(f"deadline={meta['deadline']}")
    if meta.get("closed_hint") and data.get("status") == "open":
        data["status"] = "closed"
        changes.append("closed")
    for field in ("source_platform", "author", "author_handle", "tweet_id"):
        if meta.get(field) and meta[field] != data.get(field):
            data[field] = meta[field]
            changes.append(field)
    return changes


def is_job_like(data: dict[str, Any]) -> bool:
    return data.get("type") == "job" or "job-search" in set(data.get("topics") or [])


def context_headings(data: dict[str, Any]) -> tuple[str, str]:
    if is_job_like(data):
        return "Role Context", "Role Signals"
    return "Retrieved Context", "What's In It"


def render_note(
    data: dict[str, Any],
    enrichment_lines: list[str],
    context_summary: list[str] | None = None,
    context_highlights: list[str] | None = None,
) -> str:
    frontmatter = dump_frontmatter(data)
    item_type = data.get("type", "")
    title = data.get("title", "Untitled")
    context_heading, highlights_heading = context_headings(data)
    lines = [
        "---",
        frontmatter,
        "---",
        "",
        f"# {title}",
        "",
        "## Summary",
        "",
        f"- {data.get('why_saved', 'Saved for later review.')}",
    ]
    if data.get("url"):
        lines.append(f"- Canonical URL: {data['url']}")
    lines.append(f"- First saved on: {data.get('discovered_on', '')}")
    if data.get("last_relevant_on") and data.get("last_relevant_on") != data.get("discovered_on"):
        lines.append(f"- Last seen in export on: {data['last_relevant_on']}")
    lines.append("")

    if item_type == "job":
        lines.extend(
            [
                "## Application Snapshot",
                "",
                f"- Company: {data.get('company') or 'Unknown'}",
                f"- Role: {data.get('role') or 'Role to review'}",
                f"- Location: {data.get('location') or 'Unknown'}",
                f"- Employment type: {data.get('employment_type') or 'Unknown'}",
                f"- Application status: {data.get('application_status') or 'to_review'}",
                f"- Posted on: {data.get('posted_on') or 'Unknown from source'}",
                f"- Deadline: {data.get('deadline') or 'Not captured'}",
                "",
            ]
        )

    if context_summary:
        lines.extend([f"## {context_heading}", ""])
        for line in context_summary:
            lines.append(f"- {line}")
        lines.append("")

    if context_highlights:
        lines.extend([f"## {highlights_heading}", ""])
        for line in context_highlights:
            lines.append(f"- {line}")
        lines.append("")

    lines.extend(
        [
            "## Source Context",
            "",
            f"- Source export: [[{data.get('source_export', '')}]]",
            f"- Sender: {data.get('source_sender', 'Unknown')}",
        ]
    )
    if data.get("source_dates"):
        lines.append(f"- Mentions in export: {', '.join(data['source_dates'][:10])}")
    lines.extend(["", "## Notes", "", f"> {data.get('source_excerpt', '').strip()}", ""])

    if enrichment_lines:
        lines.extend(["## Live Enrichment", ""])
        for line in enrichment_lines:
            lines.append(f"- {line}")
        lines.append("")

    if data.get("topics"):
        lines.extend(["## Linked Topics", ""])
        for topic in data["topics"]:
            label = topic.replace("-", " ").title()
            lines.append(f"- [[topics/{topic}|{label}]]")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def collect_notes(vault_root: Path, cutoff: date, mode: str) -> list[Path]:
    selected: list[Path] = []
    if mode == "jobs_recent":
        folders = ["items/jobs", "items/opportunities", "items/misc"]
        for folder in folders:
            for path in iter_note_paths(vault_root / folder):
                try:
                    data, _ = load_note(path)
                except Exception:
                    continue
                if not (data.get("url") and is_recent(data, cutoff)):
                    continue
                if data.get("type") == "job":
                    selected.append(path)
                    continue
                topics = set(data.get("topics") or [])
                if "job-search" in topics:
                    selected.append(path)
    elif mode == "knowledge_all":
        folders = ["items/articles", "items/resources", "items/events", "items/opportunities"]
        for folder in folders:
            for path in iter_note_paths(vault_root / folder):
                try:
                    data, _ = load_note(path)
                except Exception:
                    continue
                if not data.get("url"):
                    continue
                if data.get("type") == "opportunity" and "job-search" in set(data.get("topics") or []):
                    continue
                selected.append(path)
    return selected


def collect_notes_for_targets(vault_root: Path, paths: list[str], urls: list[str]) -> list[Path]:
    selected: list[Path] = []
    seen: set[Path] = set()

    for raw_path in paths:
        candidate = (vault_root / raw_path).resolve()
        try:
            candidate.relative_to(vault_root)
        except ValueError:
            continue
        if candidate.exists() and candidate.suffix == ".md" and candidate not in seen:
            selected.append(candidate)
            seen.add(candidate)

    url_keys = {url_match_key(url) for url in urls if str(url or "").strip()}
    if not url_keys:
        return selected

    folders = ["items/jobs", "items/opportunities", "items/articles", "items/resources", "items/events", "items/tweets", "items/misc"]
    for folder in folders:
        for path in iter_note_paths(vault_root / folder):
            if path in seen:
                continue
            try:
                data, _ = load_note(path)
            except Exception:
                continue
            note_url = str(data.get("url") or "")
            if note_url and url_match_key(note_url) in url_keys:
                selected.append(path)
                seen.add(path)
    return selected


def refresh_dashboards(vault_root: Path) -> None:
    def load_items(folders: list[str]) -> list[tuple[dict[str, Any], Path]]:
        items = []
        for folder in folders:
            for path in iter_note_paths(vault_root / folder):
                try:
                    data, _ = load_note(path)
                except Exception:
                    continue
                items.append((data, path))
        return items

    jobs = load_items(["items/jobs"])
    opportunities = load_items(["items/opportunities"])
    events = load_items(["items/events"])
    reminders = load_items(["items/reminders"])
    reading = load_items(["items/articles", "items/resources", "items/tweets"])

    timely = jobs + opportunities + events + reminders
    timely.sort(key=lambda pair: pair[0].get("discovered_on", ""), reverse=True)
    evergreen = [pair for pair in reading if pair[0].get("priority") in {"high", "critical"}]
    evergreen.sort(key=lambda pair: pair[0].get("discovered_on", ""), reverse=True)

    relevant_lines = [
        "# Relevant Now",
        "",
        "This page is the top-level surfacing view for the vault. It combines urgency with topical relevance, so the first things surfaced are not only deadline-driven items but also saved thoughts, reading, and resources that match current interests.",
        "",
        "## Surfacing Heuristics",
        "",
        "- imminent deadlines and upcoming events",
        "- open jobs with high priority",
        "- recently saved thoughts or resources connected to active topics",
        "- evergreen items with strong `why_saved` context and recurring relevance",
        "",
        "## Dataview: Time-Sensitive Items",
        "",
        "```dataview",
        "TABLE deadline AS Deadline, type AS Type, title AS Title, priority AS Priority, timeliness AS Timeliness",
        'FROM "items"',
        'WHERE deadline AND status != "done" AND status != "closed" AND status != "archived"',
        "SORT deadline ASC, priority DESC",
        "```",
        "",
        "## Dataview: High-Signal Evergreen Items",
        "",
        "```dataview",
        "TABLE discovered_on AS Saved, type AS Type, title AS Title, why_saved AS Why, topics AS Topics",
        'FROM "items"',
        'WHERE timeliness = "evergreen" AND why_saved',
        "SORT discovered_on DESC",
        "LIMIT 25",
        "```",
        "",
        "## Manual Snapshot",
        "",
        "### Time-Sensitive Queue",
        "",
    ]
    for data, path in timely[:20]:
        relevant_lines.append(f"- [[{path.relative_to(vault_root).as_posix()}|{data.get('title','Untitled')}]] ({data.get('type')}, saved {data.get('discovered_on')})")
    relevant_lines.extend(["", "### High-Signal Evergreen Queue", ""])
    for data, path in evergreen[:15]:
        topics = ", ".join(data.get("topics") or []) or "none"
        relevant_lines.append(f"- [[{path.relative_to(vault_root).as_posix()}|{data.get('title','Untitled')}]] ({data.get('type')}, topics: {topics})")
    relevant_lines.append("")
    (vault_root / "dashboards" / "relevant-now.md").write_text("\n".join(relevant_lines))

    jobs_all = jobs + opportunities
    jobs_all.sort(key=lambda pair: pair[0].get("discovered_on", ""), reverse=True)
    jobs_urgent = [
        "# Jobs Urgent",
        "",
        "This page surfaces jobs that need near-term attention. Sort by explicit deadline first, then manual priority, then recent discovery.",
        "",
        "## What Belongs Here",
        "",
        "- jobs with imminent deadlines",
        "- high-priority roles still not applied to",
        "- roles that require quick follow-up such as referrals or materials",
        "",
        "## Dataview: Explicit Deadlines",
        "",
        "```dataview",
        "TABLE deadline AS Deadline, company AS Company, role AS Role, priority AS Priority, application_status AS Status",
        'FROM "items/jobs"',
        'WHERE type = "job" AND deadline AND application_status != "applied" AND application_status != "closed" AND application_status != "archived"',
        "SORT deadline ASC, priority DESC, discovered_on DESC",
        "```",
        "",
        "## Dataview: High-Priority Open Jobs",
        "",
        "```dataview",
        "TABLE company AS Company, role AS Role, posted_on AS Posted, discovered_on AS Discovered, application_status AS Status",
        'FROM "items/jobs"',
        'WHERE type = "job" AND priority = "critical"',
        "SORT deadline ASC, posted_on DESC, discovered_on DESC",
        "```",
        "",
        "## Manual Snapshot",
        "",
    ]
    for data, path in jobs_all[:30]:
        jobs_urgent.append(f"- [[{path.relative_to(vault_root).as_posix()}|{data.get('title','Untitled')}]] ({data.get('type')}, saved {data.get('discovered_on')})")
    jobs_urgent.append("")
    (vault_root / "dashboards" / "jobs-urgent.md").write_text("\n".join(jobs_urgent))

    jobs_only = load_items(["items/jobs"])
    jobs_only.sort(key=lambda pair: pair[0].get("discovered_on", ""), reverse=True)
    ledger = [
        "# Jobs Ledger",
        "",
        "This page is the canonical ledger for job opportunities. Jobs should be organized primarily by website-posted date. If a posted date is unavailable, the job belongs in the unknown-date bucket.",
        "",
        "## Operating Rules",
        "",
        "- Sort by `posted_on` descending.",
        "- Never infer `posted_on` from discovery date.",
        "- Keep unknown posted dates visible instead of hiding them.",
        "",
        "## Dataview: Jobs With Known Posted Date",
        "",
        "```dataview",
        "TABLE posted_on AS Posted, company AS Company, role AS Role, deadline AS Deadline, application_status AS Status",
        'FROM "items/jobs"',
        'WHERE type = "job" AND posted_on',
        "SORT posted_on DESC, deadline ASC",
        "```",
        "",
        "## Dataview: Unknown Posted Date",
        "",
        "```dataview",
        "TABLE company AS Company, role AS Role, discovered_on AS Discovered, deadline AS Deadline, application_status AS Status",
        'FROM "items/jobs"',
        'WHERE type = "job" AND !posted_on',
        "SORT discovered_on DESC",
        "```",
        "",
        "## Manual Snapshot",
        "",
        "### Unknown Posted Date Bucket",
        "",
        "- These jobs were imported from the raw export without reliable website-posted dates. They are sorted here by discovery date only as a review aid, not as a replacement for `posted_on`.",
        "",
    ]
    for data, path in jobs_only[:40]:
        suffix = f"posted {data.get('posted_on')}" if data.get("posted_on") else f"saved {data.get('discovered_on')}"
        ledger.append(f"- [[{path.relative_to(vault_root).as_posix()}|{data.get('title','Untitled')}]] ({suffix})")
    ledger.append("")
    (vault_root / "dashboards" / "jobs-ledger.md").write_text("\n".join(ledger))


def write_output_summary(vault_root: Path, records: list[dict[str, Any]], mode: str) -> Path:
    suffix = "jobs" if mode == "jobs_recent" else "knowledge"
    out = vault_root / "outputs" / f"{CURRENT_DATE.isoformat()} live-enrichment-summary-{suffix}.md"
    host_counts = Counter(record["host"] for record in records)
    changed = [record for record in records if record["changes"]]
    closed = [record for record in records if "closed" in record["changes"]]
    posted = [record for record in records if any(change.startswith("posted_on=") for change in record["changes"])]
    context_updates = [record for record in records if any(change.startswith("context_") for change in record["changes"])]
    lines = [
        "---",
        f'title: "Live Enrichment Summary ({suffix})"',
        f'created_on: "{CURRENT_DATE.isoformat()}"',
        "---",
        "",
        "# Live Enrichment Summary",
        "",
        "## Scope",
        "",
        f"- Notes checked: {len(records)}",
        f"- Notes changed: {len(changed)}",
        f"- Jobs marked closed/unavailable: {len(closed)}",
        f"- Jobs with newly recovered posted dates: {len(posted)}",
        f"- Notes with richer retrieved context: {len(context_updates)}",
        "",
        "## Host Coverage",
        "",
    ]
    for host, count in host_counts.most_common():
        lines.append(f"- {host}: {count}")
    lines.extend(["", "## Changed Notes", ""])
    for record in changed[:50]:
        lines.append(f"- [[{record['path']}|{record['title']}]]: {', '.join(record['changes'])}")
    lines.append("")
    out.write_text("\n".join(lines))
    return out


def fetch_note_page(path: Path, vault_root: Path) -> dict[str, Any]:
    data, _ = load_note(path)
    url = data.get("url") or ""
    host = host_from_url(url)
    record = {
        "path": path.relative_to(vault_root).as_posix(),
        "title": data.get("title", "Untitled"),
        "host": host,
        "changes": [],
        "error": None,
        "meta": None,
    }
    try:
        record["meta"] = extract_from_page(url, data)
    except Exception as exc:
        record["error"] = f"fetch_failed:{type(exc).__name__}"
    return record


def enrich(vault_root: Path, cutoff: date, mode: str, target_paths: list[str] | None = None, target_urls: list[str] | None = None) -> None:
    if target_paths or target_urls:
        notes = collect_notes_for_targets(vault_root, target_paths or [], target_urls or [])
    else:
        notes = collect_notes(vault_root, cutoff, mode)
    records: list[dict[str, Any]] = []

    fetched_records: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        future_map = {executor.submit(fetch_note_page, path, vault_root): path for path in notes}
        for future in as_completed(future_map):
            result = future.result()
            fetched_records[result["path"]] = result

    for path in notes:
        original_text = path.read_text(encoding="utf-8", errors="ignore")
        data, body = load_note(path)
        url = data.get("url") or ""
        record = fetched_records[path.relative_to(vault_root).as_posix()]
        if record.get("error"):
            record["changes"] = [record["error"]]
            records.append(record)
            continue
        meta = record["meta"] or {}

        data["url"] = meta.get("final_url", data.get("url"))

        if data.get("type") == "job":
            changes = update_job_fields(data, meta)
        else:
            if meta.get("page_title"):
                meta.setdefault("title", meta["page_title"])
            changes = update_generic_fields(data, meta)

        summary_heading, highlights_heading = context_headings(data)
        existing_context_summary = extract_existing_section_lines(body, summary_heading)
        existing_context_highlights = extract_existing_section_lines(body, highlights_heading)
        context_summary = meta.get("context_summary") or existing_context_summary
        context_highlights = meta.get("context_highlights") or existing_context_highlights

        if meta.get("context_summary") and meta["context_summary"] != existing_context_summary:
            changes.append("context_summary")
        if meta.get("context_highlights") and meta["context_highlights"] != existing_context_highlights:
            changes.append("context_highlights")

        enrichment_lines = [
            f"Checked live page on {CURRENT_DATE.isoformat()} ({meta.get('status_code')}).",
            f"Final URL: {meta.get('final_url', url)}",
        ]
        if meta.get("page_title"):
            enrichment_lines.append(f"Page title: {meta['page_title']}")
        if meta.get("page_description"):
            enrichment_lines.append(f"Page description: {meta['page_description'][:220]}")
        if meta.get("x_extraction_source"):
            enrichment_lines.append(f"X extraction source: {meta['x_extraction_source']}")
        if "closed" in changes:
            enrichment_lines.append("The live page appeared closed or unavailable during enrichment.")
        if context_summary:
            enrichment_lines.append(f"Retrieved {len(context_summary)} context bullets for vault retrieval.")
        if changes:
            new_text = render_note(data, enrichment_lines, context_summary=context_summary, context_highlights=context_highlights)
            if new_text != original_text:
                path.write_text(new_text)
            record["title"] = data.get("title", record["title"])
        record["changes"] = changes
        records.append(record)

    refresh_dashboards(vault_root)
    out = write_output_summary(vault_root, records, mode)

    log_path = vault_root / "log.md"
    existing = log_path.read_text(encoding="utf-8", errors="ignore")
    entry = (
        f"\n## [{CURRENT_DATE.isoformat()}] enrichment | Live metadata refresh ({mode})\n\n"
        f"- Checked {len(records)} notes against live pages.\n"
        f"- Updated metadata where reliable fields were exposed by official pages or page metadata.\n"
        f"- Refreshed key dashboards and wrote [[{out.relative_to(vault_root).as_posix()}]].\n"
    )
    if entry not in existing:
        log_path.write_text(existing.rstrip() + "\n" + entry)

    changed = sum(1 for record in records if record["changes"] and not record["changes"][0].startswith("fetch_failed"))
    print(
        json.dumps(
            {
                "checked": len(records),
                "changed": changed,
                "mode": mode,
                "output_summary": str(out),
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich recent job-related notes from live web pages.")
    parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    parser.add_argument("--cutoff", default="2026-01-01", help="Only check notes discovered on or after this date (YYYY-MM-DD).")
    parser.add_argument("--mode", choices=["jobs_recent", "knowledge_all", "targeted"], default="jobs_recent")
    parser.add_argument("--path", action="append", default=[], help="Specific vault-relative note path to enrich.")
    parser.add_argument("--url", action="append", default=[], help="Specific canonical/source URL whose note should be enriched.")
    args = parser.parse_args()
    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d").date()
    enrich(args.vault_root.resolve(), cutoff, args.mode, target_paths=args.path, target_urls=args.url)


if __name__ == "__main__":
    main()
