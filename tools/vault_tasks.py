#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


TASKS_PATH = Path(".vault") / "tasks" / "tasks.json"
TASK_AUDIT_PATH = Path(".vault") / "tasks" / "task-audit.jsonl"
TASK_DASHBOARD_PATH = Path("dashboards") / "tasks.md"
ACTIONABLE_TYPES = {"job", "opportunity", "event", "reminder"}
OPEN_STATUSES = {"", "open", "watching"}
DONE_WORDS_RE = re.compile(
    r"\b(done|did it|finished|completed|submitted|sent|applied|applied to|read it|read this|handled|took care|"
    r"cancelled|canceled|skipped|not doing|closed|resolved)\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s>)\]]+")


@dataclass
class Note:
    path: Path
    rel_path: str
    frontmatter: dict[str, Any]
    body: str

    @property
    def title(self) -> str:
        return clean_scalar(self.frontmatter.get("title")) or self.path.stem

    @property
    def note_type(self) -> str:
        return clean_scalar(self.frontmatter.get("type")) or infer_type(self.rel_path)

    @property
    def status(self) -> str:
        return clean_scalar(self.frontmatter.get("status")).lower()

    @property
    def url(self) -> str:
        return clean_scalar(self.frontmatter.get("url"))

    @property
    def priority(self) -> str:
        return clean_scalar(self.frontmatter.get("priority")).lower() or "medium"


def main() -> None:
    parser = argparse.ArgumentParser(description="Maintain the vault task ledger.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync-from-vault")
    sync_parser.add_argument("--vault-root", type=Path, default=Path.cwd())

    complete_parser = subparsers.add_parser("complete-from-message")
    complete_parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    complete_parser.add_argument("--message-text", required=True)
    complete_parser.add_argument("--source-id", default="")
    complete_parser.add_argument("--source", default="telegram")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--vault-root", type=Path, default=Path.cwd())
    list_parser.add_argument("--status", default="open")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--limit", type=int, default=50)

    dash_parser = subparsers.add_parser("render-dashboard")
    dash_parser.add_argument("--vault-root", type=Path, default=Path.cwd())

    args = parser.parse_args()
    vault_root = args.vault_root.resolve()

    if args.command == "sync-from-vault":
        result = sync_from_vault(vault_root)
    elif args.command == "complete-from-message":
        result = complete_from_message(
            vault_root,
            message_text=args.message_text,
            source_id=args.source_id,
            source=args.source,
        )
    elif args.command == "list":
        tasks = list_tasks(vault_root, status=args.status, limit=args.limit)
        if args.json:
            print(json.dumps({"tasks": tasks}, indent=2, sort_keys=True))
            return
        print(render_task_rows(tasks))
        return
    elif args.command == "render-dashboard":
        result = {"dashboard": str(render_dashboard(vault_root))}
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported command: {args.command}")

    print(json.dumps(result, indent=2, sort_keys=True))


def sync_from_vault(vault_root: Path) -> dict[str, Any]:
    tasks = load_tasks(vault_root)
    by_key = {task["task_key"]: task for task in tasks if task.get("task_key")}
    created = 0
    updated = 0
    closed = 0

    for note in load_notes(vault_root):
        if note.note_type not in ACTIONABLE_TYPES:
            continue
        task_key = note_task_key(note)
        existing = by_key.get(task_key)
        next_task = task_from_note(note, existing)
        if existing is None:
            tasks.append(next_task)
            by_key[task_key] = next_task
            created += 1
        else:
            if existing != next_task:
                existing.update(next_task)
                updated += 1

    note_paths = {note.rel_path for note in load_notes(vault_root)}
    for task in tasks:
        if task.get("status") != "open":
            continue
        note_path = str(task.get("note_path") or "")
        if note_path and note_path not in note_paths:
            task["status"] = "closed"
            task["completed_at"] = now_iso()
            task["completion_reason"] = "Source note no longer exists."
            closed += 1

    save_tasks(vault_root, tasks)
    render_dashboard(vault_root)
    append_audit(vault_root, {"event": "tasks.synced", "created": created, "updated": updated, "closed": closed})
    return {"created": created, "updated": updated, "closed": closed, "total": len(tasks)}


def complete_from_message(vault_root: Path, *, message_text: str, source_id: str = "", source: str = "telegram") -> dict[str, Any]:
    text = str(message_text or "").strip()
    if not DONE_WORDS_RE.search(text):
        return {"completed": 0, "matched": [], "reason": "no_completion_language"}

    tasks = load_tasks(vault_root)
    open_tasks = [task for task in tasks if task.get("status") == "open"]
    matches = rank_task_matches(open_tasks, text)
    if not matches:
        return {"completed": 0, "matched": [], "reason": "no_matching_open_task"}

    selected = [matches[0]]
    if matches[0]["score"] < 6 and len(open_tasks) != 1:
        return {
            "completed": 0,
            "matched": matches[:5],
            "reason": "ambiguous_completion",
        }

    completion_status = infer_completion_status(text)
    completed_at = now_iso()
    updated_notes: list[str] = []
    for match in selected:
        task = match["task"]
        task["status"] = completion_status
        task["completed_at"] = completed_at
        task["completion_source"] = source
        task["completion_source_id"] = source_id
        task["completion_message_excerpt"] = truncate(text, 400)
        task["completion_reason"] = f"User said this was {completion_status}."
        note_path = clean_scalar(task.get("note_path"))
        if note_path:
            changed = update_note_status(vault_root / note_path, completion_status)
            if changed:
                updated_notes.append(note_path)

    save_tasks(vault_root, tasks)
    render_dashboard(vault_root)
    payload = {
        "event": "tasks.completed_from_message",
        "completed": len(selected),
        "status": completion_status,
        "source": source,
        "source_id": source_id,
        "matches": [{k: v for k, v in match.items() if k != "task"} for match in selected],
        "updated_notes": updated_notes,
    }
    append_audit(vault_root, payload)
    return {
        "completed": len(selected),
        "status": completion_status,
        "matched": [{k: v for k, v in match.items() if k != "task"} for match in selected],
        "updated_notes": updated_notes,
    }


def list_tasks(vault_root: Path, *, status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
    tasks = load_tasks(vault_root)
    status = status.lower().strip()
    if status != "all":
        tasks = [task for task in tasks if clean_scalar(task.get("status")).lower() == status]
    tasks.sort(key=task_sort_key)
    return tasks[: max(1, limit)]


def load_notes(vault_root: Path) -> list[Note]:
    notes: list[Note] = []
    items_root = vault_root / "items"
    if not items_root.exists():
        return notes
    for path in sorted(items_root.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = parse_frontmatter(text)
        notes.append(Note(path=path, rel_path=path.relative_to(vault_root).as_posix(), frontmatter=frontmatter, body=body))
    return notes


def task_from_note(note: Note, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    application_status = clean_scalar(note.frontmatter.get("application_status")).lower()
    note_status = note.status
    status = clean_scalar(existing.get("status")).lower() or "open"
    if note_status in {"done", "closed", "archived"} or application_status in {"applied", "rejected", "closed", "archived"}:
        status = "done" if application_status == "applied" else "closed"
    elif status not in {"done", "skipped", "cancelled", "closed"}:
        status = "open"

    due_on = first_date(note.frontmatter.get("deadline"), note.frontmatter.get("revisit_after"))
    return {
        **existing,
        "id": existing.get("id") or stable_id(note.rel_path),
        "task_key": note_task_key(note),
        "title": task_title(note),
        "task_type": task_type(note),
        "status": status,
        "priority": note.priority,
        "due_on": due_on,
        "note_path": note.rel_path,
        "source_url": note.url or None,
        "created_at": existing.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "source_type": note.note_type,
        "source_title": note.title,
    }


def task_title(note: Note) -> str:
    if note.note_type == "job":
        company = clean_scalar(note.frontmatter.get("company"))
        role = clean_scalar(note.frontmatter.get("role"))
        if company and role:
            return f"Apply/review: {company} - {role}"
        return f"Apply/review: {note.title}"
    if note.note_type == "event":
        return f"Decide/register: {note.title}"
    if note.note_type == "opportunity":
        return f"Follow up: {note.title}"
    return note.title


def task_type(note: Note) -> str:
    if note.note_type == "job":
        return "apply"
    if note.note_type == "event":
        return "event_decision"
    if note.note_type == "reminder":
        return "reminder"
    if note.note_type == "opportunity":
        return "follow_up"
    return "generic"


def rank_task_matches(tasks: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    normalized_text = normalize_match_text(text)
    urls = set(URL_RE.findall(text))
    matches: list[dict[str, Any]] = []
    for task in tasks:
        score = 0
        reasons: list[str] = []
        source_url = clean_scalar(task.get("source_url"))
        if source_url and source_url in urls:
            score += 20
            reasons.append("exact_url")
        title_tokens = important_tokens(clean_scalar(task.get("title")))
        source_tokens = important_tokens(clean_scalar(task.get("source_title")))
        overlap = (title_tokens | source_tokens) & set(normalized_text.split())
        if overlap:
            score += min(12, len(overlap) * 3)
            reasons.append(f"token_overlap:{','.join(sorted(overlap)[:6])}")
        if clean_scalar(task.get("task_type")) == "apply" and re.search(r"\b(applied|submitted|application)\b", text, re.I):
            score += 5
            reasons.append("apply_language")
        if clean_scalar(task.get("task_type")) == "reminder" and re.search(r"\b(done|handled|finished|completed)\b", text, re.I):
            score += 3
            reasons.append("reminder_done_language")
        if score > 0:
            matches.append(
                {
                    "task_id": task.get("id"),
                    "title": task.get("title"),
                    "note_path": task.get("note_path"),
                    "source_url": task.get("source_url"),
                    "score": score,
                    "reasons": reasons,
                    "task": task,
                }
            )
    if len(tasks) == 1 and not matches:
        task = tasks[0]
        matches.append(
            {
                "task_id": task.get("id"),
                "title": task.get("title"),
                "note_path": task.get("note_path"),
                "source_url": task.get("source_url"),
                "score": 6,
                "reasons": ["single_open_task"],
                "task": task,
            }
        )
    matches.sort(key=lambda item: (-int(item["score"]), str(item.get("title") or "")))
    return matches


def render_dashboard(vault_root: Path) -> Path:
    tasks = list_tasks(vault_root, status="all", limit=500)
    open_tasks = [task for task in tasks if task.get("status") == "open"]
    done_tasks = [task for task in tasks if task.get("status") != "open"]
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "# Task Ledger",
        "",
        f"- Updated: {now}",
        f"- Open tasks: {len(open_tasks)}",
        f"- Completed/closed tasks: {len(done_tasks)}",
        "",
        "## Open",
        "",
    ]
    if open_tasks:
        for task in open_tasks[:80]:
            source = clean_scalar(task.get("source_url")) or clean_scalar(task.get("note_path"))
            due = clean_scalar(task.get("due_on")) or "no explicit date"
            lines.append(f"- **{task.get('title')}** ({task.get('task_type')}, {task.get('priority')}, due: {due})")
            lines.append(f"  Source: {source}")
    else:
        lines.append("- No open task-ledger items.")
    lines.extend(["", "## Recently Completed", ""])
    if done_tasks:
        for task in sorted(done_tasks, key=lambda item: str(item.get("completed_at") or item.get("updated_at") or ""), reverse=True)[:40]:
            source = clean_scalar(task.get("source_url")) or clean_scalar(task.get("note_path"))
            lines.append(f"- **{task.get('title')}** - {task.get('status')} on {clean_scalar(task.get('completed_at')) or 'unknown'}")
            lines.append(f"  Source: {source}")
    else:
        lines.append("- No completed task-ledger items yet.")
    target = vault_root / TASK_DASHBOARD_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def update_note_status(path: Path, completion_status: str) -> bool:
    if not path.exists() or path.suffix != ".md":
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = parse_frontmatter(text)
    if not frontmatter:
        return False
    item_type = clean_scalar(frontmatter.get("type"))
    changed = False
    if item_type == "job" and completion_status == "done":
        if clean_scalar(frontmatter.get("application_status")).lower() != "applied":
            frontmatter["application_status"] = "applied"
            changed = True
    next_status = "done" if completion_status == "done" else completion_status
    if clean_scalar(frontmatter.get("status")).lower() != next_status:
        frontmatter["status"] = next_status
        changed = True
    if not changed:
        return False
    path.write_text(render_note(frontmatter, body), encoding="utf-8")
    return True


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw) or {}
            if isinstance(loaded, dict):
                return loaded, body
        except Exception:
            pass
    data: dict[str, Any] = {}
    for line in raw.splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            data[match.group(1)] = match.group(2).strip().strip("\"'")
    return data, body


def render_note(frontmatter: dict[str, Any], body: str) -> str:
    if yaml is not None:
        raw = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    else:
        raw = "\n".join(f"{key}: {value}" for key, value in frontmatter.items())
    return f"---\n{raw}\n---\n{body if body.startswith(chr(10)) else chr(10) + body}"


def load_tasks(vault_root: Path) -> list[dict[str, Any]]:
    path = vault_root / TASKS_PATH
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    return tasks if isinstance(tasks, list) else []


def save_tasks(vault_root: Path, tasks: list[dict[str, Any]]) -> None:
    path = vault_root / TASKS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks.sort(key=task_sort_key)
    path.write_text(json.dumps({"schema": "my-vault-tasks-v1", "updated_at": now_iso(), "tasks": tasks}, indent=2, sort_keys=True) + "\n")


def append_audit(vault_root: Path, event: dict[str, Any]) -> None:
    path = vault_root / TASK_AUDIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"logged_at": now_iso(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def render_task_rows(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No matching tasks."
    return "\n".join(f"- {task.get('title')} [{task.get('status')}] {task.get('note_path') or task.get('source_url') or ''}" for task in tasks)


def note_task_key(note: Note) -> str:
    return f"note:{note.rel_path}"


def stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def task_sort_key(task: dict[str, Any]) -> tuple[int, str, str]:
    status_rank = 0 if task.get("status") == "open" else 1
    due = clean_scalar(task.get("due_on")) or "9999-99-99"
    priority_rank = {"critical": "0", "high": "1", "medium": "2", "low": "3"}.get(clean_scalar(task.get("priority")).lower(), "2")
    return (status_rank, f"{due}:{priority_rank}", clean_scalar(task.get("title")).lower())


def important_tokens(value: str) -> set[str]:
    stop = {"apply", "review", "follow", "up", "the", "this", "that", "with", "from", "role", "job", "event"}
    return {token for token in normalize_match_text(value).split() if len(token) >= 3 and token not in stop}


def normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def infer_completion_status(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\b(cancelled|canceled)\b", lowered):
        return "cancelled"
    if re.search(r"\b(skipped|not doing)\b", lowered):
        return "skipped"
    if re.search(r"\b(closed)\b", lowered):
        return "closed"
    return "done"


def first_date(*values: Any) -> str:
    for value in values:
        cleaned = clean_scalar(value)
        match = re.match(r"^\d{4}-\d{2}-\d{2}", cleaned)
        if match:
            return match.group(0)
    return ""


def infer_type(rel_path: str) -> str:
    parts = rel_path.split("/")
    return parts[1].rstrip("s") if len(parts) > 1 and parts[0] == "items" else "note"


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(clean_scalar(item) for item in value)
    return str(value).strip().strip("\"'")


def truncate(value: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 3].rstrip() + "..."


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
