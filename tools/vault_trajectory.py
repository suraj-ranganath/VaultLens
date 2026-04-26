#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRAJECTORY_DIR = Path(".vault") / "trajectories"
EXPORT_DIR = Path("outputs") / "trajectory-exports"
MAX_EVENT_LINE_BYTES = 256 * 1024


def sanitize_run_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return clean[:120] or datetime.now(timezone.utc).strftime("run-%Y%m%d%H%M%S")


def append_trajectory_event(vault_root: Path, run_id: str, event: dict[str, Any]) -> Path:
    safe_id = sanitize_run_id(run_id)
    target = vault_root / TRAJECTORY_DIR / f"{safe_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "trace_schema": "my-vault-trajectory-v1",
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "run_id": safe_id,
        **event,
    }
    line = json.dumps(redact(payload), sort_keys=True, ensure_ascii=False)
    encoded = line.encode("utf-8")
    if len(encoded) > MAX_EVENT_LINE_BYTES:
        payload["truncated"] = True
        payload["payload_preview"] = line[:MAX_EVENT_LINE_BYTES].decode("utf-8", errors="ignore") if isinstance(line, bytes) else line[:MAX_EVENT_LINE_BYTES]
        line = json.dumps(redact(payload), sort_keys=True, ensure_ascii=False)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return target


def export_trajectory(vault_root: Path, run_id: str, output_name: str | None = None) -> Path:
    safe_id = sanitize_run_id(run_id)
    source = vault_root / TRAJECTORY_DIR / f"{safe_id}.jsonl"
    if not source.exists():
        raise FileNotFoundError(f"No trajectory found for run id {safe_id}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_output = sanitize_run_id(output_name or f"{safe_id}-{timestamp}")
    target_dir = vault_root / EXPORT_DIR / safe_output
    target_dir.mkdir(parents=True, exist_ok=False)
    events_path = target_dir / "events.jsonl"
    shutil.copyfile(source, events_path)
    events = read_jsonl(events_path)
    manifest = {
        "schema": "my-vault-trajectory-export-v1",
        "run_id": safe_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(events),
        "streams": sorted({str(event.get("surface") or event.get("event") or "unknown") for event in events}),
        "files": ["events.jsonl", "manifest.json"],
    }
    (target_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target_dir


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if re.search(r"(api[_-]?key|token|secret|credential|authorization|password)", key_str, re.I):
                output[key_str] = "[redacted]"
            else:
                output[key_str] = redact(item)
        return output
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        home = os.path.expanduser("~")
        if home and home != "/" and home in value:
            return value.replace(home, "$HOME")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Record or export vault agent trajectory events.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--vault-root", type=Path, default=Path.cwd())
    record.add_argument("--run-id", required=True)
    record.add_argument("--event-json", required=True)

    export = subparsers.add_parser("export")
    export.add_argument("--vault-root", type=Path, default=Path.cwd())
    export.add_argument("--run-id", required=True)
    export.add_argument("--output-name")

    args = parser.parse_args()
    if args.command == "record":
        path = append_trajectory_event(args.vault_root.resolve(), args.run_id, json.loads(args.event_json))
        print(json.dumps({"ok": True, "path": str(path)}))
        return
    if args.command == "export":
        path = export_trajectory(args.vault_root.resolve(), args.run_id, args.output_name)
        print(json.dumps({"ok": True, "path": str(path)}))


if __name__ == "__main__":
    main()
