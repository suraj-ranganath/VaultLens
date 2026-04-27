#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


MEMORY_ROOT = Path("memory")


def main() -> None:
    parser = argparse.ArgumentParser(description="Append compact daily session memory entries.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    append_parser = subparsers.add_parser("append")
    append_parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    append_parser.add_argument("--surface", required=True)
    append_parser.add_argument("--summary", required=True)
    append_parser.add_argument("--raw-text", default="")
    append_parser.add_argument("--metadata-json", default="{}")

    args = parser.parse_args()
    if args.command == "append":
        try:
            metadata = json.loads(args.metadata_json)
        except json.JSONDecodeError:
            metadata = {"metadata_parse_error": args.metadata_json}
        path = append_memory(
            args.vault_root.resolve(),
            surface=args.surface,
            summary=args.summary,
            raw_text=args.raw_text,
            metadata=metadata,
        )
        print(json.dumps({"path": str(path), "relative_path": path.relative_to(args.vault_root.resolve()).as_posix()}))


def append_memory(
    vault_root: Path,
    *,
    surface: str,
    summary: str,
    raw_text: str = "",
    metadata: dict[str, Any] | None = None,
    when: datetime | None = None,
) -> Path:
    when = when or datetime.now().astimezone()
    target = vault_root / MEMORY_ROOT / f"{when.date().isoformat()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(
            "\n".join(
                [
                    "---",
                    "type: session_memory",
                    f"date: {when.date().isoformat()}",
                    "status: active",
                    "tags: [session-memory]",
                    "---",
                    "",
                    f"# Session Memory - {when.date().isoformat()}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    metadata = metadata or {}
    lines = [
        f"## {when.isoformat(timespec='seconds')} - {surface}",
        "",
        f"- Summary: {truncate(summary, 500)}",
    ]
    if raw_text.strip():
        lines.append(f"- Raw: {truncate(raw_text, 700)}")
    useful_metadata = {key: value for key, value in metadata.items() if value not in (None, "", [], {})}
    if useful_metadata:
        lines.append("- Metadata:")
        for key, value in useful_metadata.items():
            lines.append(f"  - {key}: {truncate(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value, 240)}")
    lines.append("")
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return target


def recent_memory_context(vault_root: Path, *, max_days: int = 7, max_chars: int = 20_000) -> str:
    root = vault_root / MEMORY_ROOT
    if not root.exists():
        return ""
    files = sorted(root.glob("*.md"), reverse=True)[:max_days]
    sections: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        sections.append(f"### {path.relative_to(vault_root).as_posix()}\n{tail(text, max_chars // max(1, len(files)))}")
    return "\n\n".join(sections)[:max_chars]


def tail(text: str, max_chars: int) -> str:
    source = str(text or "").strip()
    if len(source) <= max_chars:
        return source
    return "[truncated]\n" + source[-max_chars:]


def truncate(value: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 3].rstrip() + "..."


if __name__ == "__main__":
    main()
