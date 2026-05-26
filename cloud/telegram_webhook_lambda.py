from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import requests


TASK_ROOT = Path(os.environ.get("LAMBDA_TASK_ROOT", "/var/task")).resolve()
WORK_ROOT = Path(os.environ.get("VAULT_WORK_ROOT", "/tmp/vault-lens")).resolve()

STATE_DIRS = [
    ".vault",
    "dashboards",
    "imports",
    "items",
    "outputs",
    "projects",
    "raw",
    "topics",
]
STATE_FILES = [
    "hot.md",
    "index.md",
    "log.md",
]
CODE_PATHS = [
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "README.md",
    "WIKI.md",
    "bin",
    "config",
    "hooks",
    ".venv",
    "node_modules",
    "bun.lock",
    "package.json",
    "pyproject.toml",
    "templates",
    "tools",
    "uv.lock",
]
STATE_BUNDLE_NAME = "vault-state.tar.gz"


def python_executable(root: Path = WORK_ROOT) -> str:
    candidate = root / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else "python3"


def prepend_venv_path(env: dict[str, str], root: Path = WORK_ROOT) -> None:
    bin_dir = root / ".venv" / "bin"
    current = env.get("PATH") or os.environ.get("PATH", "")
    if bin_dir.exists():
        env["PATH"] = f"{bin_dir}{os.pathsep}{current}" if current else str(bin_dir)
    else:
        env["PATH"] = current


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    mode = os.environ.get("VAULT_LAMBDA_MODE", "receiver").strip().lower()
    if mode == "heartbeat" or event.get("vaultHeartbeat"):
        return process_heartbeat_event(event)
    if mode == "processor" or event.get("processTelegramUpdate"):
        return process_event(event)
    return receive_webhook(event)


def receive_webhook(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
        return http_response(405, {"ok": False, "error": "method_not_allowed"})

    try:
        validate_secret(event)
    except PermissionError:
        return http_response(403, {"ok": False, "error": "invalid_secret"})

    try:
        update = parse_body(event)
    except Exception as exc:
        return http_response(400, {"ok": False, "error": "invalid_body", "detail": str(exc)})
    update_id = str(update.get("update_id") or f"unknown-{int(time.time() * 1000)}")
    send_typing_for_update(update)
    raw_key = put_raw_update(update, update_id)

    processor_name = required_env("VAULT_PROCESSOR_FUNCTION_NAME")
    boto3.client("lambda").invoke(
        FunctionName=processor_name,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "processTelegramUpdate": True,
                "update": update,
                "rawUpdateKey": raw_key,
            }
        ).encode("utf-8"),
    )

    return http_response(200, {"ok": True, "queued": True, "update_id": update_id})


def process_event(event: dict[str, Any]) -> dict[str, Any]:
    update = event.get("update")
    if not isinstance(update, dict):
        if event.get("rawUpdateKey"):
            update = read_json_from_s3(str(event["rawUpdateKey"]))
        else:
            raise ValueError("Processor event did not include update or rawUpdateKey")

    typing = TelegramTypingHeartbeat(update)
    typing.start()
    try:
        debounce = float(os.environ.get("VAULT_PROCESSOR_DEBOUNCE_SECONDS", "1.5"))
        if debounce > 0:
            time.sleep(min(debounce, 10.0))
        prepare_work_root()
        download_state()
        updates = collect_pending_cloud_updates(update)
        result = run_telegram_webhook(updates)
        upload_state()
    finally:
        typing.stop()

    return {
        "ok": True,
        "mode": "processor",
        "update_id": update.get("update_id"),
        "result": result,
    }


def process_heartbeat_event(event: dict[str, Any]) -> dict[str, Any]:
    prepare_work_root()
    download_state()
    result = run_heartbeat(dry_run=bool(event.get("dryRun")))
    upload_state()
    return {"ok": True, "mode": "heartbeat", "result": result}


def collect_pending_cloud_updates(seed_update: dict[str, Any]) -> list[dict[str, Any]]:
    bucket = required_env("VAULT_STATE_BUCKET")
    prefix = clean_prefix(os.environ.get("VAULT_WEBHOOK_EVENTS_PREFIX", "_webhook-events"))
    processed = load_cloud_processed_update_ids()
    updates_by_id: dict[int, dict[str, Any]] = {}
    seed_id = coerce_update_id(seed_update)
    if seed_id is not None and seed_id not in processed:
        updates_by_id[seed_id] = seed_update

    client = boto3.client("s3")
    now = datetime.now(timezone.utc)
    prefixes = {
        f"{prefix}{now:%Y/%m/%d}/",
        f"{prefix}{(now - timedelta(days=1)):%Y/%m/%d}/",
        # Older receiver builds accidentally wrote a double slash after the prefix.
        # Keep reading that shape so failed queued updates can be replayed.
        f"{prefix}/{now:%Y/%m/%d}/",
        f"{prefix}/{(now - timedelta(days=1)):%Y/%m/%d}/",
    }
    max_pending = int(os.environ.get("VAULT_PROCESSOR_MAX_PENDING_UPDATES", "8"))
    for day_prefix in sorted(prefixes):
        continuation: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": day_prefix, "MaxKeys": 100}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            response = client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                key = item.get("Key")
                if not key:
                    continue
                try:
                    update = read_json_from_s3(str(key))
                except Exception as exc:
                    print(f"Skipping unreadable webhook event {key}: {exc}", flush=True)
                    continue
                update_id = coerce_update_id(update)
                if update_id is None or update_id in processed:
                    continue
                updates_by_id.setdefault(update_id, update)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                break

    ordered = [updates_by_id[key] for key in sorted(updates_by_id)]
    if seed_id is not None and seed_id in updates_by_id:
        seed_index = sorted(updates_by_id).index(seed_id)
        ordered = ordered[seed_index : seed_index + max_pending]
    else:
        ordered = ordered[:max_pending]
    return ordered or [seed_update]


def coerce_update_id(update: dict[str, Any]) -> int | None:
    try:
        return int(update.get("update_id"))
    except Exception:
        return None


def load_cloud_processed_update_ids() -> set[int]:
    session_name = os.environ.get("TELEGRAM_SESSION_NAME", "telegram-live")
    path = WORK_ROOT / "imports" / "telegram-inbox" / f"{session_name} .telegram_processed_updates.jsonl"
    ids: set[int] = set()
    if not path.exists():
        return ids
    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
            ids.add(int(record["update_id"]))
        except Exception:
            continue
    return ids


def validate_secret(event: dict[str, Any]) -> None:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected:
        return

    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    actual = headers.get("x-telegram-bot-api-secret-token", "").strip()
    if actual != expected:
        raise PermissionError("Invalid Telegram webhook secret token")


def parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Telegram webhook body must be a JSON object")
    return payload


def send_typing_for_update(update: dict[str, Any]) -> None:
    chat_id = extract_chat_id(update)
    if chat_id is None:
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendChatAction",
            params={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )
        if not response.ok:
            print(f"sendChatAction failed: status={response.status_code} body={response.text[:200]}", flush=True)
    except Exception as exc:
        print(f"sendChatAction failed: {type(exc).__name__}: {exc}", flush=True)


class TelegramTypingHeartbeat:
    def __init__(self, update: dict[str, Any], interval_seconds: float = 3.5) -> None:
        self.update = update
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        send_typing_for_update(self.update)
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            send_typing_for_update(self.update)


def extract_chat_id(update: dict[str, Any]) -> int | None:
    callback = update.get("callback_query") or {}
    callback_message = callback.get("message") or {}
    callback_chat = callback_message.get("chat") or {}
    callback_chat_id = callback_chat.get("id")
    if callback_chat_id is not None:
        try:
            return int(callback_chat_id)
        except Exception:
            pass

    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or {}
    )
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    try:
        return int(chat_id)
    except Exception:
        return None


def put_raw_update(update: dict[str, Any], update_id: str) -> str:
    bucket = required_env("VAULT_STATE_BUCKET")
    prefix = clean_prefix(os.environ.get("VAULT_WEBHOOK_EVENTS_PREFIX", "_webhook-events"))
    now = datetime.now(timezone.utc)
    key = f"{prefix}{now:%Y/%m/%d}/{update_id}-{int(time.time() * 1000)}.json"
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=(json.dumps(update, sort_keys=True) + "\n").encode("utf-8"),
        ContentType="application/json",
    )
    return key


def read_json_from_s3(key: str) -> dict[str, Any]:
    obj = boto3.client("s3").get_object(Bucket=required_env("VAULT_STATE_BUCKET"), Key=key)
    payload = json.loads(obj["Body"].read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"S3 object {key} did not contain a JSON object")
    return payload


def prepare_work_root() -> None:
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    for relative in CODE_PATHS:
        source = TASK_ROOT / relative
        if not source.exists():
            continue
        target = WORK_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=source.is_dir())


def download_state() -> None:
    bucket = required_env("VAULT_STATE_BUCKET")
    key = state_bundle_key()
    client = boto3.client("s3")
    archive_path = WORK_ROOT / STATE_BUNDLE_NAME
    try:
        client.download_file(bucket, key, str(archive_path))
    except client.exceptions.NoSuchKey:
        return
    except Exception as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return
        raise

    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            target = (WORK_ROOT / member.name).resolve()
            if os.path.commonpath([str(WORK_ROOT), str(target)]) != str(WORK_ROOT):
                raise RuntimeError(f"Unsafe path in state bundle: {member.name}")
        archive.extractall(WORK_ROOT)
    archive_path.unlink(missing_ok=True)


def upload_state() -> None:
    bucket = required_env("VAULT_STATE_BUCKET")
    key = state_bundle_key()
    client = boto3.client("s3")
    archive_path = WORK_ROOT / STATE_BUNDLE_NAME

    with tarfile.open(archive_path, "w:gz") as archive:
        for path in iter_state_paths():
            archive.add(path, arcname=path.relative_to(WORK_ROOT).as_posix())

    client.upload_file(str(archive_path), bucket, key)
    archive_path.unlink(missing_ok=True)


def iter_state_paths() -> list[Path]:
    paths: list[Path] = []
    for directory in STATE_DIRS:
        root = WORK_ROOT / directory
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                paths.append(path)

    for file_name in STATE_FILES:
        path = WORK_ROOT / file_name
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(paths)


def run_telegram_webhook(update: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    home_dir = WORK_ROOT / ".home"
    codex_home = WORK_ROOT / ".codex"
    package_cache = WORK_ROOT / ".bun-cache"
    uv_cache = WORK_ROOT / ".uv-cache"
    for directory in (home_dir, codex_home, package_cache, uv_cache):
        directory.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", required_env("OPENAI_API_KEY"))
    env.setdefault("TELEGRAM_BOT_TOKEN", required_env("TELEGRAM_BOT_TOKEN"))
    prepend_venv_path(env)
    env["HOME"] = str(home_dir)
    env["CODEX_HOME"] = str(codex_home)
    env["BUN_INSTALL_CACHE_DIR"] = str(package_cache)
    env["UV_CACHE_DIR"] = str(uv_cache)
    env["XDG_CACHE_HOME"] = str(WORK_ROOT / ".cache")
    env["XDG_CONFIG_HOME"] = str(WORK_ROOT / ".config")
    env["XDG_DATA_HOME"] = str(WORK_ROOT / ".local" / "share")
    env["VAULT_DISABLE_CODEX_THREAD_RESUME"] = "1"
    env["VAULT_ROOT"] = str(WORK_ROOT)

    command = [
        python_executable(),
        str(WORK_ROOT / "tools" / "telegram_inbox.py"),
        "webhook",
        "--vault-root",
        str(WORK_ROOT),
        "--session-name",
        os.environ.get("TELEGRAM_SESSION_NAME", "telegram-live"),
        "--agent-model",
        os.environ.get("VAULT_AGENT_MODEL", "gpt-5.4"),
        "--agent-reasoning-effort",
        os.environ.get("VAULT_AGENT_REASONING_EFFORT", "medium"),
    ]

    allowed_chat_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if allowed_chat_ids:
        command.extend(["--allowed-chat-ids", allowed_chat_ids])

    result = subprocess.run(
        command,
        input=json.dumps(update),
        text=True,
        capture_output=True,
        cwd=WORK_ROOT,
        env=env,
        timeout=int(os.environ.get("VAULT_PROCESS_TIMEOUT_SECONDS", "840")),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "telegram webhook processor failed")
    return parse_json_output(result.stdout)


def run_heartbeat(*, dry_run: bool = False) -> dict[str, Any]:
    home_dir = WORK_ROOT / ".home"
    uv_cache = WORK_ROOT / ".uv-cache"
    for directory in (home_dir, uv_cache):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("TELEGRAM_BOT_TOKEN", required_env("TELEGRAM_BOT_TOKEN"))
    env["HOME"] = str(home_dir)
    env["VAULT_ROOT"] = str(WORK_ROOT)
    env["UV_CACHE_DIR"] = str(uv_cache)
    prepend_venv_path(env)
    command = [
        python_executable(),
        str(WORK_ROOT / "tools" / "vault_heartbeat.py"),
        "--vault-root",
        str(WORK_ROOT),
    ]
    chat_id = os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID", "").strip()
    if chat_id and not dry_run:
        command.extend(["--chat-id", chat_id])
    else:
        command.append("--dry-run")
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        cwd=WORK_ROOT,
        env=env,
        timeout=int(os.environ.get("VAULT_HEARTBEAT_TIMEOUT_SECONDS", "420")),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "vault heartbeat failed")
    return parse_json_output(result.stdout)


def state_prefix() -> str:
    return clean_prefix(os.environ.get("VAULT_STATE_PREFIX", "state"))


def state_bundle_key() -> str:
    return f"{state_prefix()}{STATE_BUNDLE_NAME}"


def clean_prefix(raw: str) -> str:
    prefix = str(raw or "").strip().strip("/")
    return f"{prefix}/" if prefix else ""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_json_output(raw: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    text = str(raw or "").strip()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            parsed, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        trailing = text[index + end :].strip()
        if trailing:
            continue
        if not isinstance(parsed, dict):
            raise RuntimeError("Webhook processor did not return a JSON object")
        return parsed
    raise RuntimeError(f"Webhook processor did not return parseable JSON: {text[:400]}")


def http_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, sort_keys=True),
    }
