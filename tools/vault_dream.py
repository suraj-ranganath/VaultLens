#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a reviewable vault dreaming pass.")
    parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    parser.add_argument("--mock-json", default=os.environ.get("VAULT_DREAM_MOCK_JSON", ""))
    args = parser.parse_args()
    result = run_dream(args.vault_root.resolve(), mock_json=args.mock_json)
    print(json.dumps(result, indent=2, sort_keys=True))


def run_dream(vault_root: Path, *, mock_json: str = "") -> dict[str, Any]:
    if mock_json.strip():
        dream = json.loads(mock_json)
        output_path = write_dream_report(vault_root, dream)
        append_dream_index(vault_root, dream, output_path)
        append_event(vault_root, dream, output_path)
        return {"ok": True, "output": output_path.relative_to(vault_root).as_posix(), "dream": dream}

    script = vault_root / "tools" / "vault_dream_agent.mjs"
    payload: dict[str, Any] = {
        "workingDirectory": str(vault_root),
        "model": os.environ.get("VAULT_DREAM_MODEL") or os.environ.get("VAULT_AGENT_MODEL") or "gpt-5.4",
        "reasoningEffort": os.environ.get("VAULT_DREAM_REASONING_EFFORT") or "low",
    }
    proc = subprocess.run(
        ["node", str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=vault_root,
        env=os.environ.copy(),
        check=False,
        timeout=int(os.environ.get("VAULT_DREAM_TIMEOUT_SECONDS", "180")),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "vault dream agent failed")
    dream = json.loads(proc.stdout)
    output_path = write_dream_report(vault_root, dream)
    append_dream_index(vault_root, dream, output_path)
    append_event(vault_root, dream, output_path)
    return {"ok": True, "output": output_path.relative_to(vault_root).as_posix(), "dream": dream}


def write_dream_report(vault_root: Path, dream: dict[str, Any]) -> Path:
    day = date.today().isoformat()
    target_dir = vault_root / "outputs" / "dreams"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{day} vault dream.md"
    lines = [
        "---",
        "type: vault_dream",
        f"created_on: {datetime.now(timezone.utc).isoformat()}",
        "status: review",
        "tags: [vault-dream, memory-promotion]",
        "---",
        "",
        f"# Vault Dream - {day}",
        "",
        "## Summary",
        "",
        f"- {dream.get('summary') or 'No durable promotions found.'}",
    ]
    for heading, key in [
        ("Durable Facts", "durable_facts"),
        ("Preferences", "preferences"),
        ("Decisions Or Systems", "decisions_or_systems"),
        ("Task Implications", "task_implications"),
        ("Topic Updates", "topic_updates"),
        ("Promotion Candidates", "should_promote"),
    ]:
        lines.extend(["", f"## {heading}", ""])
        items = dream.get(key) or []
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- None.")
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def append_dream_index(vault_root: Path, dream: dict[str, Any], output_path: Path) -> None:
    target = vault_root / "memory" / "DREAMS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("# Dreamed Memory Promotions\n\nReviewable outputs from the vault dreaming pass.\n\n", encoding="utf-8")
    relative = output_path.relative_to(vault_root).as_posix()
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"- {datetime.now().astimezone().isoformat(timespec='seconds')}: [[{relative}|vault dream]] - {dream.get('summary') or 'No summary'}\n")


def append_event(vault_root: Path, dream: dict[str, Any], output_path: Path) -> None:
    try:
        from vault_events import append_event as append_vault_event
    except ModuleNotFoundError:
        from tools.vault_events import append_event as append_vault_event

    append_vault_event(
        vault_root,
        {
            "event": "vault.dream.completed",
            "run_id": f"dream-{date.today().isoformat()}",
            "stream": "assistant",
            "output": output_path.relative_to(vault_root).as_posix(),
            "promotion_count": sum(len(dream.get(key) or []) for key in ["durable_facts", "preferences", "decisions_or_systems", "task_implications", "topic_updates"]),
        },
    )


if __name__ == "__main__":
    main()
