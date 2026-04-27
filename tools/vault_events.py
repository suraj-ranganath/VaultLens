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
    run_id = str(event.get("run_id") or event.get("thread_id") or event.get("update_id") or "global")
    payload = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "event_schema": "my-vault-agent-event-v2",
        "run_id": run_id,
        "seq": next_seq(target, run_id),
        "stream": event.get("stream") or infer_stream(event),
        **event,
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return target


def next_seq(path: Path, run_id: str) -> int:
    if not path.exists():
        return 1
    try:
        count = 0
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]):
            try:
                event = json.loads(line)
            except Exception:
                continue
            if str(event.get("run_id") or "") == run_id:
                return int(event.get("seq") or 0) + 1
            count += 1
            if count > 500:
                break
    except Exception:
        pass
    return 1


def infer_stream(event: dict[str, Any]) -> str:
    name = str(event.get("event") or "")
    if ".error" in name or name.endswith("failed"):
        return "error"
    if "tool" in name or "action" in name:
        return "tool"
    if "query" in name or "answer" in name:
        return "assistant"
    if "telegram" in name or "web" in name:
        return "lifecycle"
    return "item"


def main() -> None:
    parser = argparse.ArgumentParser(description="Append a structured vault agent event.")
    parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    parser.add_argument("--event-json", required=True)
    args = parser.parse_args()
    append_event(args.vault_root.resolve(), json.loads(args.event_json))


if __name__ == "__main__":
    main()
