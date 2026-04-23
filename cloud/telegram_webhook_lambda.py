from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3


TASK_ROOT = Path(os.environ.get("LAMBDA_TASK_ROOT", "/var/task")).resolve()
WORK_ROOT = Path(os.environ.get("VAULT_WORK_ROOT", "/tmp/my-vault")).resolve()

STATE_DIRS = [
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
    "node_modules",
    "package-lock.json",
    "package.json",
    "templates",
    "tools",
]
STATE_BUNDLE_NAME = "vault-state.tar.gz"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    mode = os.environ.get("VAULT_LAMBDA_MODE", "receiver").strip().lower()
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

    prepare_work_root()
    download_state()
    result = run_telegram_webhook(update)
    upload_state()

    return {
        "ok": True,
        "mode": "processor",
        "update_id": update.get("update_id"),
        "result": result,
    }


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


def put_raw_update(update: dict[str, Any], update_id: str) -> str:
    bucket = required_env("VAULT_STATE_BUCKET")
    prefix = clean_prefix(os.environ.get("VAULT_WEBHOOK_EVENTS_PREFIX", "_webhook-events"))
    now = datetime.now(timezone.utc)
    key = f"{prefix}/{now:%Y/%m/%d}/{update_id}-{int(time.time() * 1000)}.json"
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


def run_telegram_webhook(update: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", required_env("OPENAI_API_KEY"))
    env.setdefault("TELEGRAM_BOT_TOKEN", required_env("TELEGRAM_BOT_TOKEN"))
    env.setdefault("PATH", os.environ.get("PATH", ""))

    command = [
        "python3",
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
    return json.loads(result.stdout)


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


def http_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, sort_keys=True),
    }
