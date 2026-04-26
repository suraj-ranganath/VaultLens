#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVENTS_PATH = Path(".vault") / "events" / "agent-events.jsonl"


def append_event(vault_root: Path, event: dict[str, Any]) -> Path:
    target = vault_root / EVENTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Append a structured vault agent event.")
    parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    parser.add_argument("--event-json", required=True)
    args = parser.parse_args()
    append_event(args.vault_root.resolve(), json.loads(args.event_json))


if __name__ == "__main__":
    main()
