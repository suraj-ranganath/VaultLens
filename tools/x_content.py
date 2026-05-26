#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import warnings
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning


OEMBED_ENDPOINT = "https://publish.twitter.com/oembed"
REQUEST_TIMEOUT_SECONDS = 12
X_USER_AGENT = "Mozilla/5.0 (compatible; vault-lens-x-content/1.0)"

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)


@dataclass
class XPost:
    url: str
    tweet_id: str
    author_name: str = ""
    author_handle: str = ""
    text: str = ""
    markdown: str = ""
    published_on: str = ""
    source: str = ""
    expanded_urls: list[str] | None = None
    metrics: dict[str, int] | None = None
    raw: dict[str, Any] | None = None

    def to_meta(self) -> dict[str, Any]:
        title = build_title(self)
        summary = []
        if self.text:
            summary.append(f"Post text: {self.text}")
        elif title:
            summary.append(f"X post saved as: {title}.")

        highlights = []
        if self.author_handle:
            highlights.append(f"Author handle: {self.author_handle}")
        if self.author_name:
            highlights.append(f"Author name: {self.author_name}")
        if self.published_on:
            highlights.append(f"Published on X: {self.published_on}")
        if self.source:
            highlights.append(f"X extraction source: {self.source}")
        for expanded_url in (self.expanded_urls or [])[:4]:
            highlights.append(f"Linked URL: {expanded_url}")
        if self.metrics:
            metric_bits = [f"{key}={value}" for key, value in sorted(self.metrics.items()) if value]
            if metric_bits:
                highlights.append(f"Public metrics: {', '.join(metric_bits)}")

        return {
            "status_code": 200,
            "final_url": self.url,
            "page_title": title,
            "page_description": self.text[:280],
            "title": title,
            "published_on": self.published_on,
            "date_confidence": "exact" if self.published_on else "unknown",
            "source_platform": "X",
            "author": self.author_name,
            "author_handle": self.author_handle,
            "tweet_id": self.tweet_id,
            "x_extraction_source": self.source,
            "context_summary": summary[:4],
            "context_highlights": highlights[:8],
            "raw_x_post": asdict(self),
        }


def normalize_x_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    host = parsed.netloc.lower().removeprefix("www.")
    if host in {"twitter.com", "mobile.twitter.com"}:
        host = "x.com"
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 3 and path_parts[1] in {"status", "statuses"}:
        path = f"/{path_parts[0]}/status/{path_parts[2]}"
    elif len(path_parts) >= 3 and path_parts[0] == "i" and path_parts[1] == "web" and path_parts[2] == "status" and len(path_parts) >= 4:
        path = f"/i/web/status/{path_parts[3]}"
    else:
        path = parsed.path.rstrip("/")
    return urlunparse(("https", host or "x.com", path, "", "", ""))


def is_x_post_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"x.com", "twitter.com", "mobile.twitter.com"}:
        return False
    return extract_tweet_id(url) != ""


def extract_tweet_id(url: str) -> str:
    parsed = urlparse(str(url or ""))
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part in {"status", "statuses"} and index + 1 < len(parts):
            match = re.match(r"^(\d+)", parts[index + 1])
            return match.group(1) if match else ""
    return ""


def build_title(post: XPost) -> str:
    author = post.author_handle or (f"@{post.author_name}" if post.author_name else "@unknown")
    text = re.sub(r"\s+", " ", post.text).strip()
    if text:
        return f"X post from {author}: {truncate(text, 90)}"
    return f"X post from {author}"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def parse_date_text(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    if not value:
        return ""
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", value)
    if match:
        return parse_date_text(match.group(1))
    return ""


def clean_text(value: Any) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    text = soup.get_text(" ", strip=True)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def markdown_escape(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", text)


def render_oembed_markdown(blockquote: BeautifulSoup) -> str:
    paragraph = blockquote.find("p")
    if not paragraph:
        return ""
    parts: list[str] = []
    for child in paragraph.children:
        if getattr(child, "name", None) == "a":
            label = clean_text(child.get_text(" ", strip=True))
            href = child.get("href") or label
            parts.append(f"[{markdown_escape(label)}]({href})")
        else:
            parts.append(markdown_escape(clean_text(str(child))))
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()


def parse_oembed_payload(payload: dict[str, Any], fallback_url: str) -> XPost:
    source_url = normalize_x_url(str(payload.get("url") or fallback_url))
    tweet_id = extract_tweet_id(source_url) or extract_tweet_id(fallback_url)
    author_name = clean_text(payload.get("author_name") or "")
    author_url = str(payload.get("author_url") or "")
    author_handle = ""
    if author_url:
        parts = [part for part in urlparse(author_url).path.split("/") if part]
        if parts:
            author_handle = f"@{parts[0]}"

    html_body = str(payload.get("html") or "")
    soup = BeautifulSoup(html_body, "html.parser")
    paragraph = soup.find("p")
    text = clean_text(paragraph.get_text(" ", strip=True) if paragraph else "")
    markdown = render_oembed_markdown(soup)
    expanded_urls = []
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "")
        if href and "/status/" not in href and href not in expanded_urls:
            expanded_urls.append(href)

    published_on = ""
    status_link = soup.find("a", href=re.compile(r"/status(?:es)?/\d+"))
    if status_link:
        published_on = parse_date_text(status_link.get_text(" ", strip=True))

    return XPost(
        url=source_url,
        tweet_id=tweet_id,
        author_name=author_name,
        author_handle=author_handle,
        text=text,
        markdown=markdown,
        published_on=published_on,
        source="twitter_oembed",
        expanded_urls=expanded_urls,
        raw={"oembed": payload},
    )


def fetch_oembed(url: str) -> XPost | None:
    query = urlencode({"url": normalize_x_url(url), "omit_script": "1", "dnt": "1"})
    response = requests.get(
        f"{OEMBED_ENDPOINT}?{query}",
        headers={"User-Agent": X_USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        return None
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    return parse_oembed_payload(payload, url)


def run_xurl(endpoint: str) -> dict[str, Any] | None:
    if not shutil.which("xurl"):
        return None
    try:
        proc = subprocess.run(
            ["xurl", endpoint],
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def fetch_xurl(url: str) -> XPost | None:
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return None
    query = urlencode(
        {
            "ids": tweet_id,
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "tweet.fields": "created_at,conversation_id,entities,public_metrics,referenced_tweets",
            "user.fields": "description,public_metrics,profile_image_url,verified",
        }
    )
    payload = run_xurl(f"/2/tweets?{query}")
    if not payload:
        return None
    data = payload.get("data")
    tweets = data if isinstance(data, list) else []
    tweet = tweets[0] if tweets and isinstance(tweets[0], dict) else None
    if not tweet:
        return None

    users = ((payload.get("includes") or {}).get("users") or []) if isinstance(payload.get("includes"), dict) else []
    users_by_id = {str(user.get("id")): user for user in users if isinstance(user, dict)}
    author = users_by_id.get(str(tweet.get("author_id"))) or {}
    entities = tweet.get("entities") if isinstance(tweet.get("entities"), dict) else {}
    expanded_urls = [
        str(entry.get("expanded_url") or entry.get("url") or "")
        for entry in entities.get("urls", [])
        if isinstance(entry, dict) and (entry.get("expanded_url") or entry.get("url"))
    ]
    text = replace_entity_urls(str(tweet.get("text") or ""), entities)
    metrics = {
        key.replace("_count", ""): int(value)
        for key, value in (tweet.get("public_metrics") or {}).items()
        if isinstance(value, int) and key.endswith("_count")
    } if isinstance(tweet.get("public_metrics"), dict) else {}
    handle = str(author.get("username") or "")
    return XPost(
        url=normalize_x_url(url),
        tweet_id=tweet_id,
        author_name=clean_text(author.get("name") or ""),
        author_handle=f"@{handle}" if handle else "",
        text=clean_text(text),
        markdown=markdown_escape(clean_text(text)),
        published_on=parse_structured_date(tweet.get("created_at")),
        source="xurl",
        expanded_urls=expanded_urls,
        metrics=metrics,
        raw={"xurl": payload},
    )


def replace_entity_urls(text: str, entities: dict[str, Any]) -> str:
    output = text
    urls = entities.get("urls")
    for entry in urls if isinstance(urls, list) else []:
        if not isinstance(entry, dict):
            continue
        short = str(entry.get("url") or "")
        expanded = str(entry.get("expanded_url") or entry.get("display_url") or short)
        if short and expanded:
            output = output.replace(short, expanded)
    return output


def parse_structured_date(value: Any) -> str:
    text = str(value or "")
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else ""


def fetch_x_post(url: str, *, prefer_xurl: bool = True) -> XPost | None:
    if not is_x_post_url(url):
        return None
    if prefer_xurl:
        post = fetch_xurl(url)
        if post:
            return post
    return fetch_oembed(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch normalized X/Twitter post metadata.")
    parser.add_argument("url")
    parser.add_argument("--no-xurl", action="store_true", help="Skip optional xurl transport and use public fallback only.")
    args = parser.parse_args()
    post = fetch_x_post(args.url, prefer_xurl=not args.no_xurl)
    payload = {"ok": bool(post), "post": asdict(post) if post else None}
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
