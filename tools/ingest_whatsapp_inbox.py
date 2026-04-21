#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

try:
    from ingest_chat_export import ingest
except ModuleNotFoundError:
    from tools.ingest_chat_export import ingest


CURRENT_DATE = date.today()
MANIFEST_NAME = ".processed.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_manifest(path: Path, manifest: dict[str, dict[str, str]]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def canonical_raw_export_path(vault_root: Path, source_path: Path) -> Path:
    raw_exports_dir = (vault_root / "raw" / "chat-exports").resolve()
    try:
        source_path.resolve().relative_to(raw_exports_dir)
        return source_path.resolve()
    except ValueError:
        pass

    target_name = f"{CURRENT_DATE.isoformat()} {source_path.name}"
    target_path = vault_root / "raw" / "chat-exports" / target_name
    if not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    counter = 2
    while True:
        candidate = target_path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def record_signature(path: Path) -> dict[str, str]:
    stat = path.stat()
    return {
        "sha256": file_sha256(path),
        "size": str(stat.st_size),
        "mtime": str(int(stat.st_mtime)),
    }


def ingest_file(vault_root: Path, source_path: Path, manifest: dict[str, dict[str, str]], dry_run: bool) -> dict[str, str]:
    signature = record_signature(source_path)
    manifest_key = source_path.name
    previous = manifest.get(manifest_key)
    if previous == signature:
        return {
            "source": str(source_path),
            "status": "skipped_already_processed",
        }

    raw_target = canonical_raw_export_path(vault_root, source_path)
    if not dry_run:
        raw_target.parent.mkdir(parents=True, exist_ok=True)
        if raw_target.resolve() != source_path.resolve():
            shutil.copy2(source_path, raw_target)
        ingest(vault_root, raw_target)
        manifest[manifest_key] = signature

    return {
        "source": str(source_path),
        "status": "ingested" if not dry_run else "would_ingest",
        "raw_copy": str(raw_target),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest WhatsApp self-group exports from a dedicated inbox folder into the vault.")
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path.cwd(),
        help="Vault root directory. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--inbox-dir",
        type=Path,
        default=None,
        help="Directory containing exported WhatsApp .txt files. Defaults to imports/whatsapp-inbox under the vault root.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Optional single WhatsApp export file to ingest directly.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    vault_root = args.vault_root.resolve()
    inbox_dir = args.inbox_dir.resolve() if args.inbox_dir else (vault_root / "imports" / "whatsapp-inbox")
    manifest_path = inbox_dir / MANIFEST_NAME
    manifest = load_manifest(manifest_path)

    if args.source:
        source_files = [args.source.resolve()]
    else:
        if not inbox_dir.exists():
            raise FileNotFoundError(f"Missing inbox directory: {inbox_dir}")
        source_files = sorted(path for path in inbox_dir.glob("*.txt") if path.is_file())

    results = []
    for source_path in source_files:
        results.append(ingest_file(vault_root, source_path, manifest, args.dry_run))

    if not args.dry_run and not args.source:
        save_manifest(manifest_path, manifest)

    print(json.dumps({"processed": results}, indent=2))


if __name__ == "__main__":
    main()
