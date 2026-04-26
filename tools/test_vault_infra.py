#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class VaultInfraTests(unittest.TestCase):
    def test_compile_cache_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "tools"
            tools.mkdir()
            for name in ["vault_compile_cache.py", "vault_search.py"]:
                (tools / name).write_text((REPO_ROOT / "tools" / name).read_text(), encoding="utf-8")
            item_dir = root / "items" / "articles"
            item_dir.mkdir(parents=True)
            (item_dir / "2026-04-26 retrieval systems.md").write_text(
                """---
type: article
title: Retrieval Systems For Personal Vaults
url: https://example.com/retrieval
discovered_on: 2026-04-26
published_on: 2026-04-25
status: open
priority: high
tags: [retrieval, agents]
topics: [personal-knowledge]
why_saved: Useful design notes for faster agentic vault search.
---

# Retrieval Systems For Personal Vaults

Hybrid search combines lexical matching, recency, and diverse snippets for cheaper answers.
""",
                encoding="utf-8",
            )

            compile_proc = subprocess.run(
                ["python3", str(tools / "vault_compile_cache.py"), "--vault-root", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_proc.returncode, 0, compile_proc.stderr)
            self.assertTrue((root / ".vault" / "cache" / "agent-digest.json").exists())
            self.assertTrue((root / ".vault" / "cache" / "claims.jsonl").exists())
            self.assertTrue((root / ".vault" / "cache" / "search.sqlite").exists())

            search_proc = subprocess.run(
                [
                    "python3",
                    str(tools / "vault_search.py"),
                    "search",
                    "--vault-root",
                    str(root),
                    "--query",
                    "agentic retrieval search",
                    "--limit",
                    "3",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(search_proc.returncode, 0, search_proc.stderr)
            payload = json.loads(search_proc.stdout)
            self.assertTrue(payload["results"])
            self.assertEqual(payload["results"][0]["path"], "items/articles/2026-04-26 retrieval systems.md")

    def test_telegram_update_coalescing(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        updates = [
            self._telegram_update(101, 1_777_000_000, "first link https://example.com/a"),
            self._telegram_update(102, 1_777_000_010, "second thought"),
        ]
        groups = telegram_inbox.coalesce_update_groups(updates, set())
        self.assertEqual(len(groups), 1)
        normalized = telegram_inbox.normalize_message(groups[0][-1])
        merged = telegram_inbox.merge_normalized_message_group(groups[0], normalized)
        self.assertIn("Message 1", merged["raw_text"])
        self.assertIn("first link", merged["raw_text"])
        self.assertIn("second thought", merged["raw_text"])
        self.assertEqual(merged["collected_update_ids"], [101, 102])

    def _telegram_update(self, update_id: int, timestamp: int, text: str) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id + 1000,
                "date": timestamp,
                "text": text,
                "chat": {"id": 42, "type": "private", "title": ""},
                "from": {"id": 1, "first_name": "Suraj"},
            },
        }


if __name__ == "__main__":
    unittest.main()
