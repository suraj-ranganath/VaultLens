#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

from x_content import is_x_post_url, normalize_x_url


CURRENT_DATE = date.today()
MESSAGE_RE = re.compile(
    r"^[\u200e]?\[(?P<date>\d{1,2}/\d{1,2}/\d{2}), (?P<time>\d{1,2}:\d{2}:\d{2}\s*[AP]M)\] (?P<sender>[^:]+): (?P<text>.*)$"
)
URL_RE = re.compile(r"https?://\S+")
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "igsh",
    "igshid",
    "rcm",
    "si",
    "s",
    "t",
    "feature",
    "ref",
    "ref_src",
    "source",
    "ssr",
    "shared_id",
    "previouslocale",
    "lang",
    "width",
    "height",
    "mobile",
    "needredirect",
    "jan1offset",
    "jun1offset",
    "bga",
    "pli",
}
JOB_HOST_PATTERNS = (
    "greenhouse.io",
    "ashbyhq.com",
    "lever.co",
    "myworkdayjobs.com",
    "oraclecloud.com",
    "amazon.jobs",
    "icims.com",
    "eightfold.ai",
    "workstory.io",
)
ARTICLE_HOST_PATTERNS = (
    "arxiv.org",
    "medium.com",
    "towardsdatascience.com",
    "medicalxpress.com",
    "openai.com",
)
RESOURCE_HOST_PATTERNS = (
    "github.com",
    "docs.google.com",
    "youtube.com",
    "youtu.be",
    "qiskit.org",
    "stackoverflow.com",
    "drive.google.com",
    "unsloth.ai",
    "discord.gg",
)
EVENT_HOST_PATTERNS = (
    "meetup.com",
    "zoom.us",
    "meet.google.com",
    "rsaconference.com",
    "whatson.melbourne.vic.gov.au",
    "treehacks.com",
)
TOPIC_PATTERNS = {
    "job-search": [
        "intern",
        "hiring",
        "career",
        "job",
        "resume",
        "apply",
        "recruit",
    ],
    "ai-research": [
        "research",
        "paper",
        "arxiv",
        "multimodal",
        "detector",
        "benchmark",
        "reasoning",
    ],
    "llm-agents": [
        "agent",
        "claude",
        "codex",
        "anthropic",
        "openai",
        "llm",
        "agentic",
    ],
    "identity-security": [
        "identity",
        "security",
        "1password",
        "rsa",
        "saviynt",
        "skyflow",
        "anvilogic",
    ],
    "stealthrl": [
        "stealthrl",
        "detector",
        "paraphrase",
        "authormist",
        "fast-detectgpt",
        "binoculars",
        "grpo",
    ],
    "mflow": [
        "mflow",
        "percussion",
        "melody",
        "drum kit",
        "harmonics",
        "pattern",
    ],
    "academic-opportunities": [
        "workshop",
        "call for",
        "fellowship",
        "scholars",
        "expo",
        "summer school",
        "challenge",
        "conference",
    ],
    "ml-systems": [
        "optimizer",
        "scaling",
        "gpu",
        "nvidia",
        "speedrun",
        "model",
    ],
    "design-inspiration": [
        "landing page",
        "copy",
        "aesthetics",
        "design",
        "merch",
        "inspo",
    ],
    "personal-admin": [
        "tax filing",
        "file taxes",
        "passport",
        "wallet",
        "flight",
        "real id",
        "fuel",
        "wholefoods",
        "doordash",
    ],
    "ios-swift": [
        "swift",
        "airtable",
        "apple",
        "universal links",
    ],
    "crypto": [
        "binance",
        "coinmarketcap",
        "crypto",
    ],
}
TOPIC_TITLES = {
    "job-search": "Job Search",
    "ai-research": "AI Research",
    "llm-agents": "LLM Agents",
    "identity-security": "Identity Security",
    "stealthrl": "StealthRL",
    "mflow": "Mflow",
    "academic-opportunities": "Academic Opportunities",
    "ml-systems": "ML Systems",
    "design-inspiration": "Design Inspiration",
    "personal-admin": "Personal Admin",
    "ios-swift": "iOS and Swift",
    "crypto": "Crypto",
}
PROJECT_TOPICS = {
    "job-search": "job-search",
    "stealthrl": "stealthrl",
    "mflow": "mflow",
    "identity-security": "identity-security",
}


@dataclass
class Message:
    dt: datetime
    sender: str
    text: str


@dataclass
class Item:
    key: str
    type: str
    title: str
    discovered_on: str
    last_relevant_on: str
    url: str = ""
    source_excerpt: str = ""
    source_sender: str = ""
    tags: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    why_saved: str = ""
    timeliness: str = "evergreen"
    priority: str = "medium"
    status: str = "open"
    date_confidence: str = "unknown"
    source_dates: list[str] = field(default_factory=list)
    interest_signals: list[str] = field(default_factory=list)
    body_notes: list[str] = field(default_factory=list)
    company: str = ""
    role: str = ""
    location: str = ""
    employment_type: str = ""
    posted_on: str = ""
    application_status: str = "to_review"
    deadline: str = ""
    deadline_type: str = "unknown"
    requires_referral: bool = False


def clean_text(text: str) -> str:
    return text.replace("\u200e", "").replace("\ufeff", "").strip()


def slugify(text: str, max_len: int = 80) -> str:
    text = unquote(text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len].strip("-") or "item"


def prettify_slug(text: str) -> str:
    text = clean_text(text)
    text = unquote(text)
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Untitled"
    words = []
    for token in text.split():
        if token.upper() in {"AI", "ML", "LLM", "NLP", "RL", "GPU", "IOS"}:
            words.append(token.upper())
        elif token.isupper() and len(token) <= 5:
            words.append(token)
        else:
            words.append(token.capitalize())
    return " ".join(words)


def yaml_str(value: str) -> str:
    return json.dumps(value or "")


def yaml_list(values: Iterable[str]) -> str:
    return json.dumps(list(values))


def vault_rel_path(path: Path) -> str:
    markers = {"items", "topics", "projects", "outputs", "raw", "dashboards", "templates", "imports"}
    parts = list(path.parts)
    for idx, part in enumerate(parts):
        if part in markers:
            return "/".join(parts[idx:])
    return path.as_posix()


def parse_messages(path: Path) -> list[Message]:
    lines = path.read_text(errors="replace").splitlines()
    messages: list[Message] = []
    current: Message | None = None

    for line in lines:
        match = MESSAGE_RE.match(line)
        if match:
            if current:
                current.text = clean_text(current.text)
                messages.append(current)
            message_time = match.group("time").replace("\u202f", " ")
            dt = datetime.strptime(
                f"{match.group('date')} {message_time}",
                "%m/%d/%y %I:%M:%S %p",
            )
            current = Message(
                dt=dt,
                sender=clean_text(match.group("sender")),
                text=clean_text(match.group("text")),
            )
        elif current:
            current.text += "\n" + line

    if current:
        current.text = clean_text(current.text)
        messages.append(current)

    return messages


def extract_urls(text: str) -> list[str]:
    found = []
    for match in URL_RE.findall(text):
        found.append(match.rstrip("),.]}>"))
    return found


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if "instagram.com" in parsed.netloc and parsed.path == "/" and "u" in parse_qs(parsed.query):
        target = parse_qs(parsed.query)["u"][0]
        return normalize_url(unquote(target))
    if is_x_post_url(url):
        return normalize_x_url(url)

    netloc = parsed.netloc.lower().replace("www.", "")
    query = parse_qs(parsed.query, keep_blank_values=False)
    clean_query = []
    for key, values in query.items():
        lower_key = key.lower()
        if lower_key in TRACKING_PARAMS or lower_key.startswith("utm_"):
            continue
        clean_query.append((key, values[-1]))
    clean_query.sort()
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    normalized = parsed._replace(
        scheme="https",
        netloc=netloc,
        path=path,
        params="",
        query=urlencode(clean_query),
        fragment="",
    )
    return urlunparse(normalized)


def infer_topics(blob: str) -> list[str]:
    lowered = blob.lower()
    topics = []
    for topic, patterns in TOPIC_PATTERNS.items():
        matched = False
        for pattern in patterns:
            if " " in pattern or "-" in pattern:
                if pattern in lowered:
                    matched = True
                    break
            else:
                if re.search(rf"\b{re.escape(pattern)}\b", lowered):
                    matched = True
                    break
        if matched:
            topics.append(topic)
    return sorted(set(topics))


def company_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    path_parts = [part for part in parsed.path.split("/") if part]

    if "greenhouse.io" in host and len(path_parts) >= 2:
        return prettify_slug(path_parts[0])
    if "ashbyhq.com" in host and path_parts:
        return prettify_slug(path_parts[0])
    if "lever.co" in host and path_parts:
        return prettify_slug(path_parts[0])
    if "myworkdayjobs.com" in host:
        first = host.split(".")[0]
        return prettify_slug(first.replace("wd1", "").replace("wd5", ""))
    if host.endswith("amazon.jobs"):
        return "Amazon"
    if "snowflake" in host:
        return "Snowflake"
    if "mongodb" in host:
        return "MongoDB"
    if "zoom" in host:
        return "Zoom"
    if "nvidia" in host:
        return "NVIDIA"
    if "waymo" in host:
        return "Waymo"
    if "notion" in host:
        return "Notion"
    if "visa" in host:
        return "Visa"
    if "uber" in host:
        return "Uber"
    if "cisco" in host:
        return "Cisco"
    if "tesla" in host:
        return "Tesla"
    if "microsoft" in host:
        return "Microsoft"
    if "docusign" in host:
        return "DocuSign"
    if "zoox" in host:
        return "Zoox"
    if path_parts:
        return prettify_slug(path_parts[0])
    return prettify_slug(host.split(".")[0])


def role_from_url(url: str) -> str:
    parsed = urlparse(url)
    candidates = []
    for part in parsed.path.split("/"):
        part = unquote(part)
        if not part:
            continue
        if re.fullmatch(r"[a-z0-9]{8,}", part.lower()):
            continue
        if re.fullmatch(r"\d+", part):
            continue
        if any(word in part.lower() for word in ["intern", "engineer", "scientist", "developer", "research", "analyst", "product", "machine-learning", "software", "data"]):
            candidates.append(part)
    if candidates:
        best = max(candidates, key=len)
        return prettify_slug(best)
    return "Role To Review"


def classify_url(url: str, text: str) -> str:
    blob = f"{url} {text}".lower()
    host = urlparse(url).netloc.lower()

    if "x.com" in host or "twitter.com" in host:
        return "tweet"
    if "linkedin.com/jobs/" in url.lower():
        return "job"
    if any(pattern in host for pattern in JOB_HOST_PATTERNS):
        return "job"
    if any(marker in blob for marker in ["internship", "hiring", "job opening", "summer 2026", "apply now"]) and "linkedin.com" in host:
        return "opportunity"
    if any(marker in blob for marker in ["call for", "fellowship", "scholar", "challenge", "summer school", "research expo"]):
        return "opportunity"
    if any(pattern in host for pattern in EVENT_HOST_PATTERNS):
        return "event"
    if any(marker in blob for marker in ["meetup", "webinar", "agenda", "event", "conference", "session"]) and "linkedin.com" not in host:
        return "event"
    if any(pattern in host for pattern in ARTICLE_HOST_PATTERNS):
        return "article"
    if any(pattern in host for pattern in RESOURCE_HOST_PATTERNS):
        return "resource"
    if "linkedin.com/in/" in url.lower():
        return "resource"
    if "linkedin.com" in host or "instagram.com" in host or "reddit.com" in host:
        return "resource"
    return "misc"


def build_url_title(item_type: str, url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    path_parts = [part for part in parsed.path.split("/") if part]

    if item_type == "tweet":
        handle = path_parts[0] if path_parts else "unknown"
        return f"Tweet from @{handle}"
    if item_type == "job":
        company = company_from_url(url)
        role = role_from_url(url)
        return f"{company} - {role}"
    if item_type == "opportunity":
        if "linkedin.com" in host:
            handle = path_parts[1] if len(path_parts) > 1 else "unknown"
            return f"LinkedIn opportunity post from {prettify_slug(handle)}"
        if path_parts:
            if path_parts[-1].isdigit():
                return f"Opportunity from {prettify_slug(host.split('.')[0])}"
            return prettify_slug(path_parts[-1])
        return f"Opportunity from {prettify_slug(host.split('.')[0])}"
    if item_type == "event":
        if "linkedin.com" in host:
            handle = path_parts[1] if len(path_parts) > 1 else "unknown"
            return f"LinkedIn event post from {prettify_slug(handle)}"
        if path_parts:
            if path_parts[-1].isdigit():
                return f"Event from {prettify_slug(host.split('.')[0])}"
            return prettify_slug(path_parts[-1])
        return f"Event from {prettify_slug(host.split('.')[0])}"
    if item_type == "article":
        if path_parts:
            return prettify_slug(path_parts[-1])
        return f"Article from {prettify_slug(host.split('.')[0])}"
    if item_type == "resource":
        if "linkedin.com" in host and path_parts and path_parts[0] == "posts":
            handle = path_parts[1] if len(path_parts) > 1 else "unknown"
            return f"LinkedIn post from {prettify_slug(handle)}"
        if "github.com" in host and len(path_parts) >= 2:
            return f"GitHub repo {path_parts[0]}/{path_parts[1]}"
        if "linkedin.com/in/" in url.lower():
            return f"LinkedIn profile {prettify_slug(path_parts[-1])}"
        if path_parts:
            return prettify_slug(path_parts[-1])
        return f"Resource from {prettify_slug(host.split('.')[0])}"
    if path_parts:
        return prettify_slug(path_parts[-1])
    return f"Saved link from {prettify_slug(host.split('.')[0])}"


def choose_priority(item_type: str, dt: datetime, text: str, topics: list[str]) -> str:
    days_old = (CURRENT_DATE - dt.date()).days
    lowered = text.lower()
    if item_type in {"job", "opportunity", "event", "reminder"} and days_old <= 60:
        return "high"
    if any(topic in topics for topic in ["stealthrl", "mflow", "identity-security"]) and days_old <= 90:
        return "high"
    if "openai" in lowered or "anthropic" in lowered:
        return "high"
    return "medium" if days_old <= 365 else "low"


def choose_timeliness(item_type: str, text: str) -> str:
    lowered = text.lower()
    if item_type in {"job", "opportunity", "event", "reminder"}:
        return "timely"
    if any(marker in lowered for marker in ["call for", "deadline", "summer 2026", "spring 2026"]):
        return "timely"
    return "evergreen"


def choose_why_saved(item_type: str, topics: list[str], text: str) -> str:
    if item_type == "job":
        return "Saved as a role to review or potentially apply to."
    if item_type == "opportunity":
        return "Saved as a time-sensitive opportunity, call, fellowship, or application path."
    if item_type == "event":
        return "Saved because it may matter on a specific date or connect to an active interest."
    if item_type == "tweet":
        return "Saved as a short-form signal, idea, or pointer worth revisiting."
    if item_type == "article":
        return "Saved as reading material connected to recurring technical interests."
    if item_type == "resource":
        return "Saved as a reusable reference, profile, repo, doc, or external pointer."
    if "stealthrl" in topics:
        return "Saved because it appears relevant to StealthRL work and detector-evasion research."
    if "mflow" in topics:
        return "Saved because it appears relevant to Mflow design or product iteration."
    return "Saved because it looked worth revisiting later."


def classify_text_message(message: Message) -> str | None:
    text = message.text.strip()
    lowered = text.lower()
    lines = [line.strip("•-* \t") for line in text.splitlines() if line.strip()]
    if not text:
        return None
    if "omitted" in lowered or "you deleted this message" in lowered:
        return None
    if len(text) < 18 and len(lines) == 1:
        return None
    recent = (CURRENT_DATE - message.dt.date()).days <= 365
    if len(lines) >= 2:
        if recent and any(
            marker in lowered
            for marker in [
                "mail",
                "follow up",
                "apply",
                "tax",
                "passport",
                "wallet",
                "resume",
                "request",
                "drop course",
                "message ",
            ]
        ):
            return "reminder"
        if any(marker in lowered for marker in ["replace", "default", "randomise", "grid", "let tables be csv", "semantic similarity", "thresholds", "eval", "detector", "percussion", "melody"]):
            return "thought"
        if len(text) > 180:
            return "thought"
    if len(text) > 220:
        return "thought"
    if recent and any(marker in lowered for marker in ["remember", "dating", "relationship", "girlfriend", "boyfriend", "partner"]):
        return "thought"
    if recent and any(marker in lowered for marker in ["openai", "experian", "anthropic", "mats", "tax filing", "paper submission", "flight"]):
        return "reminder"
    return None


def build_text_title(item_type: str, text: str, dt: datetime) -> str:
    lines = [line.strip("•-* \t") for line in text.splitlines() if line.strip()]
    head = lines[0] if lines else text.strip()
    head = re.sub(r"\s+", " ", head)
    if item_type == "reminder":
        return f"Reminder {dt.date().isoformat()} - {head[:55]}"
    return head[:72]


def note_path_for_item(vault_root: Path, item: Item) -> Path:
    base = vault_root / "items"
    key_suffix = item.key.split("-")[-1][:6]
    if item.type == "job":
        company = slugify(item.company or "unknown-company", 30)
        role = slugify(item.role or "role-to-review", 40)
        if item.posted_on:
            name = f"{item.posted_on} {company} - {role} [{key_suffix}].md"
        else:
            name = f"undated {company} - {role} [{key_suffix}].md"
        return base / "jobs" / name
    folder_map = {
        "article": "articles",
        "thought": "thoughts",
        "event": "events",
        "tweet": "tweets",
        "reminder": "reminders",
        "opportunity": "opportunities",
        "resource": "resources",
        "misc": "misc",
    }
    folder = folder_map.get(item.type, "misc")
    title_slug = slugify(item.title, 60)
    name = f"{item.discovered_on} {title_slug} [{key_suffix}].md"
    return base / folder / name


def render_note(item: Item, source_rel: str) -> str:
    lines = [
        "---",
        f"type: {yaml_str(item.type)}",
        f"title: {yaml_str(item.title)}",
        f"url: {yaml_str(item.url)}",
        f"source_export: {yaml_str(source_rel)}",
        f"source_excerpt: {yaml_str(item.source_excerpt)}",
        f"source_sender: {yaml_str(item.source_sender)}",
        f"discovered_on: {yaml_str(item.discovered_on)}",
        f"published_on: {yaml_str('')}",
        f"deadline: {yaml_str(item.deadline)}",
        f"status: {yaml_str(item.status)}",
        f"priority: {yaml_str(item.priority)}",
        f"tags: {yaml_list(item.tags)}",
        f"topics: {yaml_list(item.topics)}",
        f"why_saved: {yaml_str(item.why_saved)}",
        f"revisit_after: {yaml_str('')}",
        f"last_relevant_on: {yaml_str(item.last_relevant_on)}",
        f"timeliness: {yaml_str(item.timeliness)}",
        f"interest_signals: {yaml_list(item.interest_signals)}",
        f"date_confidence: {yaml_str(item.date_confidence)}",
    ]
    if item.type == "job":
        lines.extend(
            [
                f"company: {yaml_str(item.company)}",
                f"role: {yaml_str(item.role)}",
                f"location: {yaml_str(item.location)}",
                f"employment_type: {yaml_str(item.employment_type)}",
                f"posted_on: {yaml_str(item.posted_on)}",
                f"application_status: {yaml_str(item.application_status)}",
                f"deadline_type: {yaml_str(item.deadline_type)}",
                f"requires_referral: {'true' if item.requires_referral else 'false'}",
            ]
        )
    lines.append("---")
    lines.append("")
    lines.append(f"# {item.title}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- {item.why_saved}")
    if item.url:
        lines.append(f"- Canonical URL: {item.url}")
    lines.append(f"- First saved on: {item.discovered_on}")
    if item.last_relevant_on != item.discovered_on:
        lines.append(f"- Last seen in export on: {item.last_relevant_on}")
    lines.append("")
    if item.type == "job":
        lines.append("## Application Snapshot")
        lines.append("")
        lines.append(f"- Company: {item.company or 'Unknown'}")
        lines.append(f"- Role: {item.role or 'Role to review'}")
        lines.append(f"- Application status: {item.application_status}")
        lines.append(f"- Posted on: {item.posted_on or 'Unknown from source'}")
        lines.append(f"- Deadline: {item.deadline or 'Not captured'}")
        lines.append("")
    lines.append("## Source Context")
    lines.append("")
    lines.append(f"- Source export: [[{source_rel}]]")
    lines.append(f"- Sender: {item.source_sender or 'Unknown'}")
    if item.source_dates:
        lines.append(f"- Mentions in export: {', '.join(item.source_dates[:10])}")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(f"> {item.source_excerpt}")
    lines.append("")
    if item.body_notes:
        lines.append("## Agent Notes")
        lines.append("")
        for note in item.body_notes:
            lines.append(f"- {note}")
        lines.append("")
    if item.topics:
        lines.append("## Linked Topics")
        lines.append("")
        for topic in item.topics:
            lines.append(f"- [[topics/{topic}|{TOPIC_TITLES.get(topic, prettify_slug(topic))}]]")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_topic_page(topic: str, items: list[tuple[Item, Path]]) -> str:
    title = TOPIC_TITLES.get(topic, prettify_slug(topic))
    type_counts = Counter(item.type for item, _ in items)
    recent = sorted(items, key=lambda pair: pair[0].last_relevant_on, reverse=True)[:20]
    lines = [
        "---",
        f"title: {yaml_str(title)}",
        f"tags: {yaml_list([topic])}",
        f"related_items: {yaml_list([vault_rel_path(path) for _, path in recent])}",
        f"updated_on: {yaml_str(CURRENT_DATE.isoformat())}",
        f"active_interest: {'true' if any(item.priority in {'high', 'critical'} for item, _ in items) else 'false'}",
        "---",
        "",
        f"# {title}",
        "",
        "## Summary",
        "",
        f"- This topic currently links to {len(items)} canonical items in the vault.",
        f"- Type mix: {', '.join(f'{kind}={count}' for kind, count in type_counts.most_common())}.",
        "",
        "## Why It Is Relevant",
        "",
        "- This page exists so an agent can crawl a compact topical cluster instead of rediscovering related items from scratch.",
        "",
        "## Recent Items",
        "",
    ]
    for item, path in recent:
        rel = vault_rel_path(path)
        lines.append(f"- [[{rel}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.append("")
    return "\n".join(lines)


def render_project_page(project_slug: str, items: list[tuple[Item, Path]]) -> str:
    title = TOPIC_TITLES.get(PROJECT_TOPICS.get(project_slug, project_slug), prettify_slug(project_slug))
    recent = sorted(items, key=lambda pair: pair[0].last_relevant_on, reverse=True)[:20]
    lines = [
        "---",
        f"title: {yaml_str(title)}",
        'status: "active"',
        'priority: "high"',
        f"started_on: {yaml_str(recent[-1][0].discovered_on if recent else '')}",
        f"deadline: {yaml_str('')}",
        f"tags: {yaml_list([project_slug])}",
        f"topics: {yaml_list([PROJECT_TOPICS.get(project_slug, project_slug)])}",
        f"linked_decisions: {yaml_list([])}",
        f"linked_outputs: {yaml_list([])}",
        f"linked_items: {yaml_list([vault_rel_path(path) for _, path in recent])}",
        "---",
        "",
        f"# {title}",
        "",
        "## Goal",
        "",
        f"- Aggregate recurring context for {title.lower()} so future work can reuse prior links, reminders, and notes.",
        "",
        "## Current Context",
        "",
        f"- This project page was bootstrapped from the chat export ingest and currently links {len(items)} relevant items.",
        "",
        "## Relevant Knowledge",
        "",
    ]
    for item, path in recent:
        rel = vault_rel_path(path)
        lines.append(f"- [[{rel}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.extend(
        [
            "",
            "## Decisions",
            "",
            "- Populate this section as explicit verdict notes are created.",
            "",
            "## Open Questions",
            "",
            "- Which of these items should be elevated into explicit decision or system notes?",
            "",
            "## Outputs",
            "",
            "- Link reusable deliverables here as they are generated.",
            "",
        ]
    )
    return "\n".join(lines)


def render_output_summary(
    source_rel: str,
    all_items: list[tuple[Item, Path]],
    topic_counts: Counter,
    recent_jobs: list[tuple[Item, Path]],
    recent_thoughts: list[tuple[Item, Path]],
) -> str:
    type_counts = Counter(item.type for item, _ in all_items)
    lines = [
        "---",
        f"title: {yaml_str('Chat Export Ingest Summary')}",
        f"created_on: {yaml_str(CURRENT_DATE.isoformat())}",
        f"source_export: {yaml_str(source_rel)}",
        "---",
        "",
        "# Chat Export Ingest Summary",
        "",
        "## Scope",
        "",
        f"- Source export: [[{source_rel}]]",
        f"- Canonical notes generated or refreshed: {len(all_items)}",
        f"- Type mix: {', '.join(f'{kind}={count}' for kind, count in type_counts.most_common())}",
        "",
        "## Strongest Topic Clusters",
        "",
    ]
    for topic, count in topic_counts.most_common(10):
        lines.append(f"- [[topics/{topic}|{TOPIC_TITLES.get(topic, prettify_slug(topic))}]]: {count} linked items")
    lines.extend(
        [
            "",
            "## Recent Job and Opportunity Signals",
            "",
        ]
    )
    for item, path in recent_jobs[:15]:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.extend(
        [
            "",
            "## Recent Thought and System Signals",
            "",
        ]
    )
    for item, path in recent_thoughts[:10]:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.extend(
        [
            "",
            "## Initial Read",
            "",
            "- The export is heavily skewed toward saved links, especially social and hiring signals, but it also contains useful project notes and reminder lists.",
            "- The most active recent clusters are job search, AI research, LLM agents, identity security, and project-specific notes around StealthRL and Mflow.",
            "- Many job links do not expose website-posted dates from the raw export alone, so they currently live in the unknown posted-date bucket until enriched later.",
            "",
        ]
    )
    return "\n".join(lines)


def render_relevant_now(items: list[tuple[Item, Path]]) -> str:
    timely = [pair for pair in items if pair[0].timeliness == "timely"]
    evergreen = [pair for pair in items if pair[0].timeliness == "evergreen" and pair[0].priority in {"high", "critical"}]
    timely = sorted(timely, key=lambda pair: (pair[0].discovered_on, pair[0].priority), reverse=True)[:20]
    evergreen = sorted(evergreen, key=lambda pair: (pair[0].discovered_on, pair[0].priority), reverse=True)[:15]
    lines = [
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
        "SORT discovered_on DESC, deadline ASC, priority DESC",
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
    for item, path in timely:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.extend(["", "### High-Signal Evergreen Queue", ""])
    for item, path in evergreen:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, topics: {', '.join(item.topics[:3]) or 'none'})")
    lines.append("")
    return "\n".join(lines)


def render_jobs_urgent(items: list[tuple[Item, Path]]) -> str:
    jobs = [pair for pair in items if pair[0].type in {"job", "opportunity"}]
    jobs = sorted(jobs, key=lambda pair: pair[0].discovered_on, reverse=True)[:30]
    lines = [
        "# Jobs Urgent",
        "",
        "This page surfaces jobs that need near-term attention, with newest-shared items first while preserving explicit deadline fields for scanning.",
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
        "SORT discovered_on DESC, deadline ASC, priority DESC",
        "```",
        "",
        "## Dataview: High-Priority Open Jobs",
        "",
        "```dataview",
        "TABLE company AS Company, role AS Role, posted_on AS Posted, discovered_on AS Discovered, application_status AS Status",
        'FROM "items/jobs"',
        'WHERE type = "job" AND priority = "critical"',
        "SORT discovered_on DESC, deadline ASC, posted_on DESC",
        "```",
        "",
        "## Manual Snapshot",
        "",
    ]
    for item, path in jobs:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.append("")
    return "\n".join(lines)


def render_jobs_ledger(items: list[tuple[Item, Path]]) -> str:
    jobs = [pair for pair in items if pair[0].type == "job"]
    jobs = sorted(jobs, key=lambda pair: pair[0].discovered_on, reverse=True)
    lines = [
        "# Jobs Ledger",
        "",
        "This page is the canonical ledger for job opportunities. It surfaces the newest-shared jobs first, while preserving website-posted date as metadata for decision-making.",
        "",
        "## Operating Rules",
        "",
        "- Surface newest shared jobs first.",
        "- Keep `posted_on` explicit when known, but never infer it from discovery date.",
        "- Keep unknown posted dates visible instead of hiding them.",
        "",
        "## Dataview: Jobs With Known Posted Date",
        "",
        "```dataview",
        "TABLE discovered_on AS Discovered, posted_on AS Posted, company AS Company, role AS Role, deadline AS Deadline, application_status AS Status",
        'FROM "items/jobs"',
        'WHERE type = "job" AND posted_on',
        "SORT discovered_on DESC, posted_on DESC, deadline ASC",
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
    for item, path in jobs[:40]:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] (saved {item.discovered_on})")
    lines.append("")
    return "\n".join(lines)


def render_deadlines(items: list[tuple[Item, Path]]) -> str:
    timely = [pair for pair in items if pair[0].type in {"event", "opportunity", "job", "reminder"}]
    timely = sorted(timely, key=lambda pair: pair[0].discovered_on, reverse=True)[:30]
    lines = [
        "# Deadlines",
        "",
        "This page aggregates all known deadlines across the vault.",
        "",
        "## Dataview: All Open Deadlines",
        "",
        "```dataview",
        "TABLE deadline AS Deadline, type AS Type, title AS Title, priority AS Priority, status AS Status",
        'FROM "items"',
        'WHERE deadline AND status != "done" AND status != "closed" AND status != "archived"',
        "SORT discovered_on DESC, deadline ASC, priority DESC",
        "```",
        "",
        "## Manual Notes",
        "",
        "- The current ingest captured many timely items but very few explicit deadlines from raw text alone.",
        "- Use later enrichment passes to backfill deadlines from live pages where available.",
        "",
        "## Recent Timely Items",
        "",
    ]
    for item, path in timely:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.append("")
    return "\n".join(lines)


def source_platform(item: Item) -> str:
    host = urlparse(item.url or "").netloc.lower().replace("www.", "")
    if "medium.com" in host:
        return "Medium"
    if "towardsdatascience.com" in host:
        return "Towards Data Science"
    if "x.com" in host or "twitter.com" in host:
        return "X"
    if "linkedin.com" in host:
        return "LinkedIn"
    if "openai.com" in host:
        return "OpenAI"
    if "github.com" in host:
        return "GitHub"
    if "youtube.com" in host or "youtu.be" in host:
        return "YouTube"
    if "arxiv.org" in host:
        return "arXiv"
    if host:
        return prettify_slug(host.split(".")[0])
    return "Unknown"


def author_and_credential(item: Item) -> tuple[str, str]:
    title = clean_text(item.title)
    if item.type == "tweet":
        match = re.search(r"@([A-Za-z0-9_]+)", title)
        if match:
            return f"@{match.group(1)}", ""
        host = urlparse(item.url or "").netloc.lower()
        if "x.com" in host or "twitter.com" in host:
            path_parts = [part for part in urlparse(item.url).path.split("/") if part]
            if path_parts:
                return f"@{path_parts[0]}", ""
        return "", ""

    if "|" in title:
        parts = [part.strip() for part in title.split("|") if part.strip()]
        if len(parts) >= 2:
            if item.type == "resource" and source_platform(item) == "LinkedIn":
                if len(parts) == 2:
                    return parts[-1], ""
                author = parts[-2]
                credential = parts[-1] if parts[-1] != author else ""
                if credential.lower().endswith("comments"):
                    credential = ""
                return author, credential
            author = parts[-2]
            credential = parts[-1] if parts[-1] != author else ""
            if item.type == "resource":
                return author, credential
    return "", ""


def topic_badges(item: Item) -> str:
    if not item.topics:
        return "none"
    return ", ".join(f"#{topic}" for topic in item.topics[:5])


def render_reading_queue(items: list[tuple[Item, Path]]) -> str:
    reading = [pair for pair in items if pair[0].type in {"article", "resource", "tweet"}]
    reading = sorted(reading, key=lambda pair: (pair[0].discovered_on, pair[0].last_relevant_on), reverse=True)[:50]
    lines = [
        "# Reading Queue",
        "",
        "This page tracks technical articles, general reading, and reusable resources worth revisiting.",
        "",
        "## Dataview: Articles",
        "",
        "```dataview",
        "TABLE discovered_on AS Saved, published_on AS Published, priority AS Priority, topics AS Topics",
        'FROM "items/articles"',
        'WHERE status != "done" AND status != "archived"',
        "SORT discovered_on DESC, published_on DESC, priority DESC",
        "```",
        "",
        "## Dataview: Resources",
        "",
        "```dataview",
        "TABLE discovered_on AS Saved, title AS Title, priority AS Priority, topics AS Topics",
        'FROM "items/resources" OR "items/tweets"',
        'WHERE status != "done" AND status != "archived"',
        "SORT discovered_on DESC, priority DESC",
        "```",
        "",
        "## Manual Snapshot",
        "",
    ]
    for item, path in reading:
        platform = source_platform(item)
        author, credential = author_and_credential(item)
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]]")
        lines.append(f"  Saved: {item.discovered_on} | Type: {item.type} | Source: {platform} | Topics: {topic_badges(item)}")
        if author:
            author_line = f"Author: {author}"
            if credential:
                author_line += f" | Credential: {credential}"
            lines.append(f"  {author_line}")
    lines.append("")
    return "\n".join(lines)


def render_thoughts_radar(items: list[tuple[Item, Path]]) -> str:
    thoughts = [pair for pair in items if pair[0].type == "thought"]
    thoughts = sorted(thoughts, key=lambda pair: pair[0].discovered_on, reverse=True)[:30]
    lines = [
        "# Thoughts Radar",
        "",
        "This page keeps personal thoughts, observations, and ideas easy to retrieve later.",
        "",
        "## Dataview: Recent Thoughts",
        "",
        "```dataview",
        "TABLE discovered_on AS Saved, priority AS Priority, topics AS Topics, why_saved AS Why",
        'FROM "items/thoughts"',
        'WHERE status != "archived"',
        "SORT discovered_on DESC",
        "LIMIT 50",
        "```",
        "",
        "## Dataview: Revisit Candidates",
        "",
        "```dataview",
        "TABLE revisit_after AS Revisit, title AS Title, topics AS Topics, priority AS Priority",
        'FROM "items/thoughts"',
        'WHERE revisit_after AND status != "archived"',
        "SORT revisit_after ASC, priority DESC",
        "```",
        "",
        "## Manual Snapshot",
        "",
    ]
    for item, path in thoughts:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] (saved {item.discovered_on})")
    lines.append("")
    return "\n".join(lines)


def render_recent_items(items: list[tuple[Item, Path]]) -> str:
    recent = sorted(items, key=lambda pair: pair[0].discovered_on, reverse=True)[:50]
    lines = [
        "# Recent Items",
        "",
        "This page shows the most recently discovered items across all categories.",
        "",
        "## Dataview: Recently Discovered",
        "",
        "```dataview",
        "TABLE discovered_on AS Discovered, type AS Type, title AS Title, priority AS Priority, status AS Status",
        'FROM "items"',
        "WHERE discovered_on",
        "SORT discovered_on DESC",
        "LIMIT 50",
        "```",
        "",
        "## Manual Notes",
        "",
    ]
    for item, path in recent:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.append("")
    return "\n".join(lines)


def render_followups(items: list[tuple[Item, Path]]) -> str:
    followups = [pair for pair in items if pair[0].type in {"reminder", "opportunity"}]
    followups = sorted(followups, key=lambda pair: pair[0].discovered_on, reverse=True)[:40]
    lines = [
        "# Followups",
        "",
        "This page highlights open reminders and unresolved opportunities that need attention.",
        "",
        "## Dataview: Open Reminders",
        "",
        "```dataview",
        "TABLE discovered_on AS Discovered, deadline AS Deadline, priority AS Priority, status AS Status",
        'FROM "items/reminders"',
        'WHERE status != "done" AND status != "archived"',
        "SORT discovered_on DESC, deadline ASC, priority DESC",
        "```",
        "",
        "## Dataview: Open Opportunities",
        "",
        "```dataview",
        "TABLE discovered_on AS Discovered, deadline AS Deadline, priority AS Priority, status AS Status",
        'FROM "items/opportunities"',
        'WHERE status != "done" AND status != "closed" AND status != "archived"',
        "SORT discovered_on DESC, deadline ASC, priority DESC",
        "```",
        "",
        "## Manual Notes",
        "",
    ]
    for item, path in followups:
        lines.append(f"- [[{vault_rel_path(path)}|{item.title}]] ({item.type}, saved {item.discovered_on})")
    lines.append("")
    return "\n".join(lines)


def render_decisions_ledger(project_pages: list[Path]) -> str:
    lines = [
        "# Decisions Ledger",
        "",
        "This page tracks explicit judgments, verdicts, and go-test-skip calls so future work can build on prior reasoning instead of starting over.",
        "",
        "## Dataview: Recent Decisions",
        "",
        "```dataview",
        "TABLE discovered_on AS Saved, verdict AS Verdict, decision_domain AS Domain, related_projects AS Projects",
        'FROM "items/decisions"',
        'WHERE type = "decision"',
        "SORT discovered_on DESC",
        "```",
        "",
        "## Dataview: Decisions To Revisit",
        "",
        "```dataview",
        "TABLE review_after AS Review, verdict AS Verdict, title AS Title, related_projects AS Projects",
        'FROM "items/decisions"',
        "WHERE review_after",
        "SORT review_after ASC",
        "```",
        "",
        "## Manual Snapshot",
        "",
        "- No explicit decision notes were synthesized automatically in this first pass.",
        "- Start from these project pages when promoting implicit judgments into explicit decisions:",
        "",
    ]
    for page in project_pages:
        lines.append(f"- [[{page.relative_to(page.parent.parent).as_posix()}|{prettify_slug(page.stem)}]]")
    lines.append("")
    return "\n".join(lines)


def render_dashboard_base() -> str:
    lines = [
        "filters:",
        "  and:",
        '    - file.inFolder("items")',
        '    - \'file.ext == "md"\'',
        "",
        "formulas:",
        "  age_days: '(now() - file.mtime).days'",
        "  has_deadline: 'if(deadline, \"yes\", \"\")'",
        "",
        "properties:",
        "  type:",
        '    displayName: "Type"',
        "  priority:",
        '    displayName: "Priority"',
        "  deadline:",
        '    displayName: "Deadline"',
        "  discovered_on:",
        '    displayName: "Saved"',
        "  formula.age_days:",
        '    displayName: "Days Since Edit"',
        "",
        "views:",
        "  - type: table",
        '    name: "Recent Items"',
        "    limit: 25",
        "    order:",
        "      - discovered_on",
        "      - type",
        "      - priority",
        "      - formula.age_days",
        "  - type: table",
        '    name: "Jobs To Apply"',
        "    filters:",
        "      and:",
        '        - file.inFolder("items/jobs")',
        '        - \'application_status == "to_apply" or application_status == "to_review"\'',
        "    order:",
        "      - discovered_on",
        "      - posted_on",
        "      - priority",
        "      - deadline",
        "  - type: table",
        '    name: "Reading Queue"',
        "    filters:",
        "      and:",
        '        - \'type == "article" or type == "resource" or type == "tweet"\'',
        "    order:",
        "      - discovered_on",
        "      - priority",
        "      - formula.age_days",
        "  - type: table",
        '    name: "Upcoming Deadlines"',
        "    filters:",
        "      and:",
        '        - \'deadline\'',
        '        - \'status != "done" and status != "closed" and status != "archived"\'',
        "    order:",
        "      - discovered_on",
        "      - deadline",
        "      - priority",
        "  - type: table",
        '    name: "Decisions And Systems"',
        "    filters:",
        "      or:",
        '        - file.inFolder("items/decisions")',
        '        - file.inFolder("items/systems")',
        "    order:",
        "      - discovered_on",
        "      - verdict",
        "      - priority",
        "  - type: list",
        '    name: "Thoughts Radar"',
        "    filters:",
        "      and:",
        '        - file.inFolder("items/thoughts")',
        '        - \'status != "archived"\'',
        "    order:",
        "      - discovered_on",
        "      - priority",
    ]
    return "\n".join(lines) + "\n"


def render_dashboard_hub() -> str:
    lines = [
        "# Vault Dashboard",
        "",
        "This is the native Obsidian dashboard surface for the vault. It complements the markdown dashboards with a single Bases view and links to the deeper operational pages.",
        "",
        "## Native Dashboard",
        "",
        "![[dashboard.base]]",
        "",
        "## Operational Pages",
        "",
        "- [[relevant-now|Relevant Now]]",
        "- [[jobs-urgent|Jobs Urgent]]",
        "- [[jobs-ledger|Jobs Ledger]]",
        "- [[reading-queue|Reading Queue]]",
        "- [[thoughts-radar|Thoughts Radar]]",
        "- [[recent-items|Recent Items]]",
        "- [[followups|Followups]]",
        "- [[deadlines|Deadlines]]",
        "- [[decisions-ledger|Decisions Ledger]]",
        "- [[vault-health|Vault Health]]",
        "",
        "## Operating Notes",
        "",
        "- Latest shared items should surface first by default.",
        "- Website-posted dates remain visible on job notes even when surfacing is recency-first.",
        "- `hot.md` is the cross-session cache for recent context.",
        "",
    ]
    return "\n".join(lines)


def render_hot_cache(items: list[tuple[Item, Path]], project_pages: list[Path]) -> str:
    recent = sorted(items, key=lambda pair: (pair[0].discovered_on, pair[0].last_relevant_on), reverse=True)[:20]
    recent_titles = [f"[[{vault_rel_path(path)}|{item.title}]]" for item, path in recent[:6]]
    topic_counts = Counter(
        topic
        for item, _ in recent
        for topic in item.topics
    )
    active_topics = [topic for topic, _ in topic_counts.most_common(5)]
    open_jobs = [
        pair for pair in recent
        if pair[0].type == "job" and pair[0].application_status in {"to_review", "to_apply", "interviewing"}
    ][:5]
    timely = [
        pair for pair in recent
        if pair[0].deadline or pair[0].type in {"event", "reminder", "opportunity"}
    ][:5]
    lines = [
        "---",
        'type: "meta"',
        'title: "Hot Cache"',
        f'updated: "{datetime.now().isoformat(timespec="seconds")}"',
        'tags: ["meta", "hot-cache"]',
        'status: "evergreen"',
        "---",
        "",
        "# Recent Context",
        "",
        "## Last Updated",
        f"{CURRENT_DATE.isoformat()} — rebuilt vault surfaces and refreshed recent retrieval context.",
        "",
        "## Key Recent Facts",
    ]
    if recent_titles:
        lines.extend([f"- Most recent items in the vault: {', '.join(recent_titles[:3])}."])
    if active_topics:
        lines.append(f"- Active recent topics: {', '.join(f'`{topic}`' for topic in active_topics)}.")
    if open_jobs:
        lines.append(f"- Open recent job flow items: {', '.join(item.title for item, _ in open_jobs[:3])}.")
    if timely:
        lines.append(f"- Time-sensitive recent items remain concentrated in: {', '.join(item.title for item, _ in timely[:3])}.")
    lines.extend([
        "",
        "## Recent Changes",
        f"- Rebuilt markdown dashboards and the native Bases dashboard from {len(items)} canonical notes.",
        f"- Active project pages currently present: {len(project_pages)}.",
        "- Browser enrichment is prioritized for recent weak links, especially X posts from the last 30 days.",
        "",
        "## Active Threads",
    ])
    if active_topics:
        for topic in active_topics[:4]:
            lines.append(f"- Current interest cluster: `#{topic}`.")
    else:
        lines.append("- No dominant topic cluster was detected in the recent item window.")
    lines.append("- Keep the next ingest focused on high-context captures: jobs, technical reading, thoughts, and explicit decisions.")
    lines.append("")
    return "\n".join(lines)


def render_root_index(items: list[tuple[Item, Path]], project_pages: list[Path]) -> str:
    counts = Counter(item.type for item, _ in items)
    total = len(items)
    lines = [
        "# Index",
        "",
        "This page catalogs the main operating surfaces in the vault.",
        "",
        f"Last rebuilt: {CURRENT_DATE.isoformat()} | Canonical notes: {total} | Projects: {len(project_pages)}",
        "",
        "## Core Navigation",
        "",
        "- [[hot|Hot Cache]]: recent cross-session context for fast startup and cross-project retrieval",
        "- [[log|Log]]: append-only history of major ingest and maintenance operations",
        "- [[dashboards/dashboard|Vault Dashboard]]: native dashboard hub with the Bases view and links to deeper pages",
        "- [[dashboards/vault-health|Vault Health]]: lint-style scan for weak notes, missing metadata, and maintenance gaps",
        "",
        "## Dashboards",
        "",
        "- [[dashboards/relevant-now|Relevant Now]]: time-sensitive and topic-relevant items that should be surfaced first",
        "- [[dashboards/jobs-urgent|Jobs Urgent]]: highest-priority job applications and time-sensitive postings",
        "- [[dashboards/jobs-ledger|Jobs Ledger]]: ledger of job opportunities organized by posted date metadata",
        "- [[dashboards/artifact-capture-queue|Artifact Capture Queue]]: notes that need first-party clips, PDFs, screenshots, or pasted descriptions",
        "- [[dashboards/decisions-ledger|Decisions Ledger]]: explicit verdicts, go-test-skip calls, and their rationale",
        "- [[dashboards/deadlines|Deadlines]]: all known deadlines across jobs, events, reminders, and opportunities",
        "- [[dashboards/reading-queue|Reading Queue]]: technical and general reading worth revisiting",
        "- [[dashboards/thoughts-radar|Thoughts Radar]]: recent and high-signal personal thoughts, ideas, and observations",
        "- [[dashboards/recent-items|Recent Items]]: recently ingested items across all categories",
        "- [[dashboards/followups|Followups]]: open reminders and unresolved opportunities",
        "",
        "## Canonical Note Space",
        "",
        f"- `jobs`: {counts.get('job', 0)}",
        f"- `articles`: {counts.get('article', 0)}",
        f"- `thoughts`: {counts.get('thought', 0)}",
        f"- `tweets`: {counts.get('tweet', 0)}",
        f"- `resources`: {counts.get('resource', 0)}",
        f"- `opportunities`: {counts.get('opportunity', 0)}",
        f"- `reminders`: {counts.get('reminder', 0)}",
        f"- `events`: {counts.get('event', 0)}",
        f"- `decisions`: {counts.get('decision', 0)}",
        f"- `systems`: {counts.get('system', 0)}",
        "",
        "## Inputs",
        "",
        "- [[imports/chat-exports/README|Chat Exports]]: immutable source files to be ingested",
        "- [[imports/whatsapp-inbox/README|WhatsApp Inbox]]: self-group exports for low-friction day-to-day capture",
        "- [[imports/telegram-inbox/README|Telegram Inbox]]: always-on live bot inbox for future capture",
        "- [[raw/README|Raw Corpus]]: canonical source corpus for clips, exports, images, and documents",
        "",
        "## Templates",
        "",
        "- [[templates/item-template|Item Template]]",
        "- [[templates/job-template|Job Template]]",
        "- [[templates/thought-template|Thought Template]]",
        "- [[templates/decision-template|Decision Template]]",
        "- [[templates/system-template|System Template]]",
        "- [[templates/project-template|Project Template]]",
        "- [[templates/topic-template|Topic Template]]",
        "",
        "## System",
        "",
        "- [[AGENTS]]: operating rules for ingest, query, and maintenance",
        "- [[WIKI]]: reference schema and architectural contract for the vault",
        "- `raw/.manifest.json`: local ingest manifest for processed source files",
        "",
        "## Topic Space",
        "",
        "- [[topics/README|Topics]]: synthesis pages are created here as the vault grows",
        "",
        "## Project Space",
        "",
        "- [[projects/README|Projects]]: active projects gather related topics, decisions, and outputs",
        "",
        "## Output Space",
        "",
        "- [[outputs/README|Outputs]]: reusable briefs, notes, and deliverables generated from the wiki",
        "",
    ]
    return "\n".join(lines)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def upsert_log(log_path: Path, source_rel: str, item_count: int, url_count: int, text_count: int) -> None:
    original = log_path.read_text() if log_path.exists() else "# Log\n"
    source_name = Path(source_rel).name
    entry = (
        f"\n## [{CURRENT_DATE.isoformat()}] ingest | Imported {source_name}\n\n"
        f"- Source export copied into `raw/chat-exports/` and linked as `{source_rel}`.\n"
        f"- Generated or refreshed {item_count} canonical notes from {url_count} unique URLs and {text_count} text notes.\n"
        f"- Refreshed dashboards, topic pages, project pages, and the ingest summary output.\n"
    )
    section_pattern = re.compile(
        rf"\n## \[{re.escape(CURRENT_DATE.isoformat())}\] ingest \| Imported {re.escape(source_name)}\n\n.*?(?=\n## |\Z)",
        re.DOTALL,
    )
    if section_pattern.search(original):
        updated = section_pattern.sub(entry, original)
        log_path.write_text(updated.rstrip() + "\n")
        return
    if entry not in original:
        log_path.write_text(original.rstrip() + "\n" + entry)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upsert_source_manifest(
    manifest_path: Path,
    source_rel: str,
    source_path: Path,
    item_count: int,
    url_count: int,
    text_count: int,
    output_rel: str,
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    except Exception:
        manifest = {}
    stat = source_path.stat()
    manifest[source_rel] = {
        "sha256": file_sha256(source_path),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "ingested_on": CURRENT_DATE.isoformat(),
        "items_written": item_count,
        "url_items": url_count,
        "text_items": text_count,
        "summary_output": output_rel,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def ingest(vault_root: Path, source_path: Path) -> None:
    raw_dir = vault_root / "raw" / "chat-exports"
    raw_dir.mkdir(parents=True, exist_ok=True)
    target_name = f"{CURRENT_DATE.isoformat()} {source_path.name}"
    raw_copy = raw_dir / target_name
    shutil.copy2(source_path, raw_copy)
    source_rel = raw_copy.relative_to(vault_root).as_posix()

    messages = parse_messages(source_path)

    url_items: dict[str, Item] = {}
    text_items: dict[str, Item] = {}

    for message in messages:
        urls = extract_urls(message.text)
        if urls:
            for raw_url in urls:
                normalized = normalize_url(raw_url)
                item_type = classify_url(normalized, message.text)
                text_without_urls = URL_RE.sub(" ", message.text)
                topics = infer_topics(f"{normalized}\n{text_without_urls}\n{item_type}")
                priority = choose_priority(item_type, message.dt, message.text, topics)
                why_saved = choose_why_saved(item_type, topics, message.text)
                existing = url_items.get(normalized)
                source_date = message.dt.date().isoformat()
                if not existing:
                    title = build_url_title(item_type, normalized)
                    item = Item(
                        key=f"url-{slugify(normalized, 40)}",
                        type=item_type,
                        title=title,
                        url=normalized,
                        discovered_on=source_date,
                        last_relevant_on=source_date,
                        source_excerpt=message.text[:500],
                        source_sender=message.sender,
                        tags=[item_type],
                        topics=topics,
                        why_saved=why_saved,
                        timeliness=choose_timeliness(item_type, message.text),
                        priority=priority,
                        status="open",
                        date_confidence="unknown",
                        source_dates=[source_date],
                        interest_signals=[],
                    )
                    if item_type == "job":
                        item.company = company_from_url(normalized)
                        item.role = role_from_url(normalized)
                        item.deadline_type = "unknown"
                    url_items[normalized] = item
                else:
                    item = existing
                    if source_date < item.discovered_on:
                        item.discovered_on = source_date
                    if source_date > item.last_relevant_on:
                        item.last_relevant_on = source_date
                    item.topics = sorted(set(item.topics) | set(topics))
                    item.tags = sorted(set(item.tags) | {item.type})
                    item.source_dates.append(source_date)

        else:
            item_type = classify_text_message(message)
            if not item_type:
                continue
            key = clean_text(message.text)
            topics = infer_topics(key)
            title = build_text_title(item_type, key, message.dt)
            source_date = message.dt.date().isoformat()
            identifier = f"text-{slugify(title, 40)}-{source_date}"
            if identifier not in text_items:
                text_items[identifier] = Item(
                    key=identifier,
                    type=item_type,
                    title=title,
                    discovered_on=source_date,
                    last_relevant_on=source_date,
                    source_excerpt=message.text[:700],
                    source_sender=message.sender,
                    tags=[item_type],
                    topics=topics,
                    why_saved=choose_why_saved(item_type, topics, message.text),
                    timeliness=choose_timeliness(item_type, message.text),
                    priority=choose_priority(item_type, message.dt, message.text, topics),
                    status="open",
                    date_confidence="exact",
                    source_dates=[source_date],
                    interest_signals=[],
                )

    all_items = list(url_items.values()) + list(text_items.values())
    all_items.sort(key=lambda item: (item.discovered_on, item.title))

    topic_to_items: dict[str, list[tuple[Item, Path]]] = defaultdict(list)
    project_to_items: dict[str, list[tuple[Item, Path]]] = defaultdict(list)
    written_items: list[tuple[Item, Path]] = []

    for item in all_items:
        item.source_dates = sorted(set(item.source_dates))
        if len(item.source_dates) > 1:
            item.interest_signals.append(f"saved_{len(item.source_dates)}_times")
        if item.topics:
            item.interest_signals.extend([f"topic_{topic}" for topic in item.topics[:5]])
        item.interest_signals = sorted(set(item.interest_signals))

        note_path = note_path_for_item(vault_root, item)
        write(note_path, render_note(item, source_rel))
        written_items.append((item, note_path))
        for topic in item.topics:
            topic_to_items[topic].append((item, note_path))
        for project_slug, project_topic in PROJECT_TOPICS.items():
            if project_topic in item.topics:
                project_to_items[project_slug].append((item, note_path))

    # Topic pages
    for topic, items in topic_to_items.items():
        if len(items) < 2:
            continue
        topic_path = vault_root / "topics" / f"{topic}.md"
        write(topic_path, render_topic_page(topic, items))

    # Project pages
    project_pages = []
    for project_slug, items in project_to_items.items():
        if len(items) < 2:
            continue
        project_path = vault_root / "projects" / f"{project_slug}.md"
        write(project_path, render_project_page(project_slug, items))
        project_pages.append(project_path)

    # Dashboards
    write(vault_root / "dashboards" / "relevant-now.md", render_relevant_now(written_items))
    write(vault_root / "dashboards" / "jobs-urgent.md", render_jobs_urgent(written_items))
    write(vault_root / "dashboards" / "jobs-ledger.md", render_jobs_ledger(written_items))
    write(vault_root / "dashboards" / "deadlines.md", render_deadlines(written_items))
    write(vault_root / "dashboards" / "reading-queue.md", render_reading_queue(written_items))
    write(vault_root / "dashboards" / "thoughts-radar.md", render_thoughts_radar(written_items))
    write(vault_root / "dashboards" / "recent-items.md", render_recent_items(written_items))
    write(vault_root / "dashboards" / "followups.md", render_followups(written_items))
    write(vault_root / "dashboards" / "decisions-ledger.md", render_decisions_ledger(project_pages))

    # Output summary
    topic_counts = Counter(topic for item, _ in written_items for topic in item.topics)
    recent_jobs = sorted(
        [pair for pair in written_items if pair[0].type in {"job", "opportunity"}],
        key=lambda pair: pair[0].last_relevant_on,
        reverse=True,
    )
    recent_thoughts = sorted(
        [pair for pair in written_items if pair[0].type in {"thought", "reminder"}],
        key=lambda pair: pair[0].last_relevant_on,
        reverse=True,
    )
    output_path = vault_root / "outputs" / f"{CURRENT_DATE.isoformat()} chat-export-ingest-summary.md"
    write(
        output_path,
        render_output_summary(source_rel, written_items, topic_counts, recent_jobs, recent_thoughts),
    )
    write(vault_root / "dashboards" / "dashboard.base", render_dashboard_base())
    write(vault_root / "dashboards" / "dashboard.md", render_dashboard_hub())
    write(vault_root / "hot.md", render_hot_cache(written_items, project_pages))
    write(vault_root / "index.md", render_root_index(written_items, project_pages))

    upsert_log(
        vault_root / "log.md",
        source_rel,
        len(written_items),
        len(url_items),
        len(text_items),
    )
    upsert_source_manifest(
        vault_root / "raw" / ".manifest.json",
        source_rel,
        raw_copy,
        len(written_items),
        len(url_items),
        len(text_items),
        output_path.relative_to(vault_root).as_posix(),
    )

    print(
        json.dumps(
            {
                "source": str(source_path),
                "raw_copy": str(raw_copy),
                "items_written": len(written_items),
                "url_items": len(url_items),
                "text_items": len(text_items),
                "topic_pages": sum(1 for items in topic_to_items.values() if len(items) >= 2),
                "project_pages": len(project_pages),
                "output_summary": str(output_path),
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a WhatsApp-style chat export into the vault.")
    parser.add_argument("source", type=Path, help="Path to the chat export text file.")
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path.cwd(),
        help="Vault root directory. Defaults to the current working directory.",
    )
    args = parser.parse_args()
    ingest(args.vault_root.resolve(), args.source.resolve())


if __name__ == "__main__":
    main()
