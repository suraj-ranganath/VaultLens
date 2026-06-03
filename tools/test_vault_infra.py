#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
            self.assertTrue((root / ".vault" / "cache" / "claim-health.json").exists())
            self.assertTrue((root / ".vault" / "cache" / "search.sqlite").exists())
            self.assertTrue((root / ".vault" / "reports" / "claim-health.md").exists())
            self.assertTrue((root / ".vault" / "reports" / "memory-palace.md").exists())

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

    def test_telegram_delivery_queue_retries_failed_send(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_delivery_queue  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(telegram_delivery_queue, "send_telegram_payload", side_effect=RuntimeError("network down")):
                queued = telegram_delivery_queue.send_or_queue_telegram_message(
                    vault_root=root,
                    token="token",
                    chat_id=123,
                    text="hello",
                    idempotency_key="test-send",
                )
            self.assertTrue(queued["queued"])
            pending = list((root / ".vault" / "telegram-delivery-queue").glob("*.json"))
            self.assertEqual(len(pending), 1)

            with mock.patch.object(telegram_delivery_queue, "send_telegram_payload", return_value={"ok": True}):
                summary = telegram_delivery_queue.drain_telegram_delivery_queue(vault_root=root, token="token")
            self.assertEqual(summary["sent"], 1)
            self.assertFalse(pending[0].exists())

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

    def test_telegram_recent_context_pack_keeps_prior_links_and_images(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox_dir, _ = telegram_inbox.ensure_directories(root)
            session_name = "telegram-live"
            stream_file = telegram_inbox.stream_path(inbox_dir, session_name)
            stream_file.write_text(
                "[4/28/26, 8:01:00 AM] Suraj: Anthropic role https://anthropic.com/jobs/mts\n"
                "[4/28/26, 8:03:00 AM] Suraj: screenshot of event\n",
                encoding="utf-8",
            )
            telegram_inbox.append_jsonl(
                telegram_inbox.agent_decisions_path(inbox_dir, session_name),
                [
                    {
                        "logged_at": "2026-04-28T08:03:10",
                        "update_id": 301,
                        "chat_id": 42,
                        "message_id": 1301,
                        "message": {
                            "raw_text": "screenshot of event\n\nAttachment context:\nsummary: AI meetup flyer",
                            "attachments": [
                                {
                                    "kind": "photo",
                                    "artifact_path": "raw/images/telegram/event.png",
                                    "summary": "AI meetup flyer",
                                    "extracted_text": "AI meetup, May 2, 6 PM",
                                    "event_clues": ["May 2, 6 PM", "AI meetup"],
                                }
                            ],
                        },
                        "decision": {"classification": "event", "storeInVault": True, "summary": "Saved event screenshot."},
                        "actions": [{"tool": "append_message_to_stream"}, {"tool": "run_vault_ingest"}],
                        "tool_results": [{"tool": "run_vault_ingest", "status": "ok"}],
                    }
                ],
            )
            current = {
                "update_id": 302,
                "message_id": 1302,
                "chat_id": 42,
                "timestamp_iso": "2026-04-28T08:04:00+00:00",
                "sender": "Suraj",
                "raw_text": "what do you think about that link and screenshot?",
                "attachments": [],
            }

            context = telegram_inbox.build_recent_telegram_context(
                vault_root=root,
                inbox_dir=inbox_dir,
                session_name=session_name,
                chat_id=42,
                normalized_message=current,
            )

            self.assertIn("Anthropic role", context)
            self.assertIn("https://anthropic.com/jobs/mts", context)
            self.assertIn("raw/images/telegram/event.png", context)
            self.assertIn("AI meetup, May 2, 6 PM", context)
            self.assertTrue(telegram_inbox.looks_like_contextual_followup_question(current["raw_text"]))

    def test_telegram_query_runner_injects_recent_conversation_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env["VAULT_QUERY_CONTEXT_ONLY"] = "1"
            proc = subprocess.run(
                ["bun", str(REPO_ROOT / "tools" / "telegram_vault_query.mjs")],
                input=json.dumps(
                    {
                        "workingDirectory": str(root),
                        "question": "what do you think about that role?",
                        "includeWebSearch": False,
                        "recentConversationContext": "Previous Telegram message: Anthropic role https://anthropic.com/jobs/mts",
                    }
                ),
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertIn("telegram/recent-conversation-context", payload["vaultContext"])
            self.assertIn("Anthropic role", payload["vaultContext"])

    def test_webhook_event_prefixes_are_replayable(self) -> None:
        import sys
        import types
        from datetime import datetime, timezone
        from unittest import mock

        sys.path.insert(0, str(REPO_ROOT))
        sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *_args, **_kwargs: None))
        from cloud import telegram_webhook_lambda as webhook  # type: ignore

        update = self._telegram_update(201, 1_777_000_020, "queued message")
        captured: dict[str, str] = {}

        class FakeS3:
            def put_object(self, **kwargs):
                captured["key"] = kwargs["Key"]

        with mock.patch.dict(
            webhook.os.environ,
            {"VAULT_STATE_BUCKET": "bucket", "VAULT_WEBHOOK_EVENTS_PREFIX": "webhook-events"},
            clear=False,
        ), mock.patch.object(webhook.boto3, "client", return_value=FakeS3()), mock.patch.object(
            webhook.time, "time", return_value=1234
        ), mock.patch.object(
            webhook, "datetime", wraps=webhook.datetime
        ) as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 4, 26, tzinfo=timezone.utc)
            webhook.put_raw_update(update, "201")

        self.assertTrue(captured["key"].startswith("webhook-events/2026/04/26/"))
        self.assertNotIn("webhook-events//", captured["key"])

    def test_lambda_restores_codex_auth_from_s3_when_no_access_token(self) -> None:
        import sys
        import types
        from unittest import mock

        sys.path.insert(0, str(REPO_ROOT))
        sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *_args, **_kwargs: None))
        from cloud import telegram_webhook_lambda as webhook  # type: ignore

        auth_payload = b'{"tokens":{"id_token":"redacted"},"last_refresh":"2026-06-02T00:00:00Z"}'

        class Body:
            def read(self) -> bytes:
                return auth_payload

        class FakeS3:
            def get_object(self, **kwargs):
                self.kwargs = kwargs
                return {"Body": Body()}

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            env = {"CODEX_ACCESS_TOKEN": ""}
            fake_s3 = FakeS3()
            with mock.patch.dict(
                webhook.os.environ,
                {
                    "CODEX_ACCESS_TOKEN": "",
                    "VAULT_STATE_BUCKET": "bucket",
                    "VAULT_CODEX_AUTH_S3_KEY": "codex-auth/auth.json",
                },
                clear=True,
            ), mock.patch.object(webhook.boto3, "client", return_value=fake_s3):
                webhook.prepare_codex_auth(codex_home, env)

            self.assertNotIn("CODEX_ACCESS_TOKEN", env)
            self.assertEqual((codex_home / "auth.json").read_bytes(), auth_payload)

    def test_lambda_prefers_codex_access_token_when_present(self) -> None:
        import sys
        import types
        from unittest import mock

        sys.path.insert(0, str(REPO_ROOT))
        sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *_args, **_kwargs: None))
        from cloud import telegram_webhook_lambda as webhook  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            env: dict[str, str] = {}
            with mock.patch.dict(webhook.os.environ, {"CODEX_ACCESS_TOKEN": "token"}, clear=True), mock.patch.object(
                webhook.boto3, "client", side_effect=AssertionError("S3 should not be used when token is present")
            ):
                webhook.prepare_codex_auth(codex_home, env)

            self.assertEqual(env["CODEX_ACCESS_TOKEN"], "token")
            self.assertFalse((codex_home / "auth.json").exists())

    def test_codex_runner_defaults_to_full_access_sandbox(self) -> None:
        import sys
        from unittest import mock

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import codex_agent_runner  # type: ignore

        with mock.patch.dict(codex_agent_runner.os.environ, {}, clear=True):
            self.assertEqual(codex_agent_runner.sandbox_name_from_payload({}), "full_access")
        with mock.patch.dict(codex_agent_runner.os.environ, {"VAULT_CODEX_SANDBOX": "read-only"}, clear=True):
            self.assertEqual(codex_agent_runner.sandbox_name_from_payload({}), "read_only")

        instructions = codex_agent_runner.thread_developer_instructions(
            include_web_search=True,
            sandbox_name="full_access",
        )
        self.assertIn("Current sandbox profile: full_access", instructions)
        self.assertIn("You may write canonical vault notes", instructions)
        self.assertIn("External side effects are controlled by deterministic VaultLens code", instructions)

    def test_cloud_template_wires_full_access_browser_worker(self) -> None:
        template = (REPO_ROOT / "cloud" / "template.yaml").read_text(encoding="utf-8")
        self.assertIn("VAULT_CODEX_SANDBOX: full_access", template)
        self.assertIn("BrowserWorkerFunction:", template)
        self.assertIn("cloud/browser-worker.Dockerfile", template)
        self.assertIn("VAULT_BROWSER_WORKER_FUNCTION_NAME", template)

    def test_browser_heavy_updates_trigger_browser_worker(self) -> None:
        import sys
        import types

        sys.path.insert(0, str(REPO_ROOT))
        sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *_args, **_kwargs: None))
        from cloud import telegram_webhook_lambda as webhook  # type: ignore

        x_update = self._telegram_update(601, 1_777_000_120, "save https://x.com/someone/status/123")
        self.assertTrue(webhook.should_trigger_browser_enrichment([x_update], {}))

        explicit_update = self._telegram_update(602, 1_777_000_121, "fully extract this https://example.com/wrapped")
        self.assertTrue(webhook.should_trigger_browser_enrichment([explicit_update], {}))

        normal_update = self._telegram_update(603, 1_777_000_122, "save this note for later")
        self.assertFalse(webhook.should_trigger_browser_enrichment([normal_update], {}))

    def test_browser_enricher_skips_hidden_appledouble_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tweet_dir = root / "items" / "tweets"
            tweet_dir.mkdir(parents=True)
            (tweet_dir / "._2026-02-17 tweet-from-dejavucoder.md").write_text("not markdown frontmatter", encoding="utf-8")

            proc = subprocess.run(
                ["bun", str(REPO_ROOT / "tools" / "enrich_with_browser.mjs"), str(root), "0", "1", "30"],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["checked"], 0)

    def test_daily_brief_delegates_final_selection_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "items" / "jobs").mkdir(parents=True)
            (root / "items" / "articles").mkdir(parents=True)
            (root / "items" / "reminders").mkdir(parents=True)

            (root / "items" / "jobs" / "2026-04-25 ai lab - research engineer.md").write_text(
                """---
type: job
title: AI Lab - Research Engineer
url: https://example.com/job
discovered_on: 2026-04-25
posted_on: 2026-04-25
status: open
priority: high
application_status: to_apply
tags: [ai, research]
topics: [job-search]
why_saved: Strong fit and fresh role.
---
""",
                encoding="utf-8",
            )
            (root / "items" / "jobs" / "2026-04-24 done job.md").write_text(
                """---
type: job
title: Already Applied Role
url: https://example.com/applied
discovered_on: 2026-04-24
posted_on: 2026-04-24
status: open
priority: high
application_status: applied
tags: [ai]
---
""",
                encoding="utf-8",
            )
            (root / "items" / "reminders" / "2026-04-27 submit form.md").write_text(
                """---
type: reminder
title: Submit scholarship form
discovered_on: 2026-04-20
deadline: 2026-04-27
status: open
priority: critical
why_saved: Explicit reminder.
---
""",
                encoding="utf-8",
            )
            (root / "items" / "articles" / "2026-04-26 agent systems.md").write_text(
                """---
type: article
title: Agent Systems Design
url: https://example.com/agents
discovered_on: 2026-04-26
status: open
priority: high
tags: [agents, systems]
topics: [ai, coding]
why_saved: High-impact ideas for vault agent design.
---
""",
                encoding="utf-8",
            )
            (root / "items" / "articles" / "2026-04-01 low signal.md").write_text(
                """---
type: article
title: Low Signal Reading
url: https://example.com/low
discovered_on: 2026-04-01
status: open
priority: low
tags: [misc]
---
""",
                encoding="utf-8",
            )

            mock_agent_result = {
                "should_send": True,
                "telegram_text": "Morning brief\n1. Submit scholarship form\n2. Apply to AI Lab - Research Engineer\nReading: Agent Systems Design",
                "selected_actions": [
                    {
                        "title": "Submit scholarship form",
                        "kind": "deadline",
                        "date": "2026-04-27",
                        "source": "items/reminders/2026-04-27 submit form.md",
                        "why_today": "Deadline is tomorrow.",
                        "suggested_next_step": "Submit it today.",
                    },
                    {
                        "title": "AI Lab - Research Engineer",
                        "kind": "apply_early",
                        "date": "2026-04-25",
                        "source": "https://example.com/job",
                        "why_today": "Fresh high-fit role.",
                        "suggested_next_step": "Review and apply.",
                    },
                ],
                "recommended_reading": {
                    "title": "Agent Systems Design",
                    "source": "https://example.com/agents",
                    "why_this_matters": "Useful for vault agent design.",
                },
                "rationale": "Selected urgent reminder, fresh high-fit job, and one high-impact reading.",
            }
            env = os.environ.copy()
            env["VAULT_MORNING_BRIEF_AGENT_MOCK_JSON"] = json.dumps(mock_agent_result)
            env["VAULT_BRIEF_INCLUDE_CALENDAR"] = "false"
            proc = subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "tools" / "vault_heartbeat.py"),
                    "--vault-root",
                    str(root),
                    "--today",
                    "2026-04-26",
                    "--dry-run",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["should_send"])
            self.assertEqual(payload["mode"], "agentic_daily_brief")
            self.assertEqual(payload["action_count"], 2)
            self.assertEqual(payload["recommended_reading"]["title"], "Agent Systems Design")
            self.assertIn("Submit scholarship form", payload["text"])
            self.assertIn("AI Lab - Research Engineer", payload["text"])
            self.assertIn("Selected urgent reminder", payload["agent_rationale"])
            self.assertGreaterEqual(payload["candidate_action_count"], 2)
            self.assertGreaterEqual(payload["candidate_reading_count"], 1)
            self.assertNotIn("Already Applied Role", payload["text"])
            self.assertNotIn("Low Signal Reading", payload["text"])

    def test_daily_brief_collects_today_calendar_events_for_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "items" / "reminders").mkdir(parents=True)
            bin_dir = root / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            fake_gws = bin_dir / "gws"
            fake_gws.write_text(
                """#!/usr/bin/env python3
import json

print(json.dumps({
    "items": [
        {
            "id": "calendar-event-1",
            "status": "confirmed",
            "summary": "Research lab meeting",
            "location": "CSE building",
            "htmlLink": "https://calendar.google.com/event?eid=demo",
            "start": {"dateTime": "2026-05-08T09:00:00-07:00", "timeZone": "America/Los_Angeles"},
            "end": {"dateTime": "2026-05-08T10:00:00-07:00", "timeZone": "America/Los_Angeles"},
            "attendees": [
                {"email": "user@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "lab@example.com", "responseStatus": "accepted"}
            ],
            "creator": {"email": "user@example.com"},
            "organizer": {"email": "lab@example.com"}
        },
        {
            "id": "declined-event",
            "status": "confirmed",
            "summary": "Declined hold",
            "start": {"dateTime": "2026-05-08T11:00:00-07:00"},
            "end": {"dateTime": "2026-05-08T11:30:00-07:00"},
            "attendees": [{"email": "user@example.com", "self": True, "responseStatus": "declined"}]
        }
    ]
}))
""",
                encoding="utf-8",
            )
            fake_gws.chmod(0o755)

            mock_agent_result = {
                "should_send": True,
                "telegram_text": "Morning brief\nToday: Research lab meeting at 9am.",
                "selected_actions": [
                    {
                        "title": "Research lab meeting",
                        "kind": "calendar_event_today",
                        "date": "2026-05-08",
                        "source": "https://calendar.google.com/event?eid=demo",
                        "why_today": "Important calendar event today.",
                        "suggested_next_step": "Leave time to get to CSE building.",
                    }
                ],
                "recommended_reading": None,
                "rationale": "Included the relevant calendar event and ignored the declined hold.",
            }
            env = os.environ.copy()
            env["VAULT_MORNING_BRIEF_AGENT_MOCK_JSON"] = json.dumps(mock_agent_result)
            env["VAULT_BRIEF_INCLUDE_CALENDAR"] = "true"
            env["VAULT_DEFAULT_TIMEZONE"] = "America/Los_Angeles"
            proc = subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "tools" / "vault_heartbeat.py"),
                    "--vault-root",
                    str(root),
                    "--today",
                    "2026-05-08",
                    "--dry-run",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["calendar_event_count"], 1)
            self.assertIsNone(payload["calendar_error"])
            self.assertEqual(payload["candidate_calendar_events"][0]["title"], "Research lab meeting")
            self.assertEqual(payload["candidate_calendar_events"][0]["kind"], "calendar_event_today")
            self.assertEqual(payload["candidate_calendar_events"][0]["priority"], "high")
            self.assertIn("Research lab meeting", payload["text"])
            self.assertNotIn("Declined hold", json.dumps(payload["candidate_calendar_events"]))

    def test_gws_credentials_normalize_double_escaped_service_account_key(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore
        import vault_heartbeat  # type: ignore

        raw = json.dumps(
            {
                "type": "service_account",
                "client_email": "vault-calendar-worker@example.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\\nabc123\\n-----END PRIVATE KEY-----\\n",
            }
        )

        for module in [telegram_inbox, vault_heartbeat]:
            normalized = json.loads(module.normalize_gws_credentials_json(raw))
            self.assertEqual(
                normalized["private_key"],
                "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----\n",
            )
            self.assertNotIn("\\n", normalized["private_key"])

    def test_calendar_update_target_resolves_from_recent_history(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        plan = {
            "calendarIntent": True,
            "operation": "update",
            "targetEventId": None,
            "targetCalendarId": "",
            "needsClarification": False,
            "needsConfirmation": False,
            "userConfirmed": True,
            "events": [{"summary": "Updated event"}],
        }
        history = [
            {
                "chat_id": 123,
                "operation": "create",
                "calendar_id": "primary",
                "event_id": "evt_older",
                "summary": "Older event",
                "start": "2026-05-08T10:00:00-07:00",
            },
            {
                "chat_id": 123,
                "operation": "create",
                "calendar_id": "primary",
                "event_id": "evt_latest",
                "summary": "Latest event",
                "start": "2026-05-09T10:00:00-07:00",
            },
        ]

        resolved = telegram_inbox.resolve_calendar_target_event(plan, None, history)

        self.assertEqual(resolved["targetEventId"], "evt_latest")
        self.assertEqual(resolved["targetCalendarId"], "primary")
        self.assertEqual(resolved["targetResolution"]["source"], "recent_calendar_history")
        self.assertEqual(resolved["targetResolution"]["summary"], "Latest event")

    def test_calendar_update_without_history_asks_clarification(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        plan = {
            "calendarIntent": True,
            "operation": "update",
            "targetEventId": "",
            "targetCalendarId": "primary",
            "needsClarification": False,
            "needsConfirmation": False,
            "userConfirmed": True,
        }

        clarified = telegram_inbox.plan_calendar_target_clarification(plan, [])

        self.assertTrue(clarified["needsClarification"])
        self.assertFalse(clarified["userConfirmed"])
        self.assertIn("Which calendar event should I update?", clarified["clarificationQuestion"])
        self.assertEqual(clarified["confidence"], "low")

    def test_calendar_batch_update_targets_match_history_by_summary_and_start(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        plan = {
            "calendarIntent": True,
            "operation": "update",
            "targetEventId": None,
            "targetCalendarId": "primary",
            "needsClarification": False,
            "needsConfirmation": False,
            "userConfirmed": True,
            "events": [
                {
                    "summary": "Amazon SDE Intern (Fall) interview - Ryan Dalby",
                    "start": "2026-05-13T13:00:00-07:00",
                    "end": "2026-05-13T14:00:00-07:00",
                },
                {
                    "summary": "Amazon SDE Intern (Fall) interview - Nick Meyer",
                    "start": "2026-05-13T14:00:00-07:00",
                    "end": "2026-05-13T15:00:00-07:00",
                },
            ],
        }
        history = [
            {
                "event_id": "ryan_event",
                "summary": "Amazon SDE Intern (Fall) interview - Ryan Dalby",
                "start": {"dateTime": "2026-05-13T13:00:00-07:00", "timeZone": "America/Los_Angeles"},
                "calendar_id": "primary",
            },
            {
                "event_id": "nick_event",
                "summary": "Amazon SDE Intern (Fall) interview - Nick Meyer",
                "start": {"dateTime": "2026-05-13T14:00:00-07:00", "timeZone": "America/Los_Angeles"},
                "calendar_id": "primary",
            },
        ]

        resolved = telegram_inbox.resolve_calendar_target_event(plan, None, history)

        self.assertEqual(resolved["targetEventIds"], ["ryan_event", "nick_event"])
        self.assertEqual(resolved["targetEventId"], "ryan_event")
        self.assertFalse(telegram_inbox.calendar_target_missing(resolved))
        self.assertEqual(resolved["targetResolution"]["mode"], "batch_summary_start_match")

    def test_calendar_confirmation_accepts_yes_pls(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        self.assertIsNotNone(telegram_inbox.CALENDAR_CONFIRM_RE.match("Yes pls"))

    def test_x_oembed_payload_becomes_agent_friendly_metadata(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import x_content  # type: ignore

        payload = {
            "url": "https://twitter.com/PeterHndrsn/status/2019063646425694648",
            "author_name": "Peter Henderson",
            "author_url": "https://twitter.com/PeterHndrsn",
            "html": """
            <blockquote class="twitter-tweet">
              <p lang="en" dir="ltr">I am collecting applications for a visiting fellow position.<br>
              Especially interested in embodied RL <a href="https://t.co/demo">https://t.co/demo</a></p>
              &mdash; Peter Henderson (@PeterHndrsn)
              <a href="https://twitter.com/PeterHndrsn/status/2019063646425694648">February 4, 2026</a>
            </blockquote>
            """,
        }

        post = x_content.parse_oembed_payload(payload, payload["url"])
        meta = post.to_meta()

        self.assertEqual(post.tweet_id, "2019063646425694648")
        self.assertEqual(post.url, "https://x.com/PeterHndrsn/status/2019063646425694648")
        self.assertEqual(post.author_handle, "@PeterHndrsn")
        self.assertEqual(post.published_on, "2026-02-04")
        self.assertIn("visiting fellow", meta["context_summary"][0])
        self.assertIn("Author handle: @PeterHndrsn", meta["context_highlights"])
        self.assertEqual(meta["source_platform"], "X")

    def test_live_metadata_uses_x_adapter_without_browser(self) -> None:
        import sys
        from unittest import mock

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import enrich_live_metadata  # type: ignore
        import x_content  # type: ignore

        post = x_content.XPost(
            url="https://x.com/example/status/12345",
            tweet_id="12345",
            author_name="Example Author",
            author_handle="@example",
            text="A useful thread about local-first agent knowledge bases.",
            published_on="2026-04-26",
            source="twitter_oembed",
        )

        with mock.patch.object(enrich_live_metadata, "fetch_x_post", return_value=post):
            meta = enrich_live_metadata.extract_from_page("https://twitter.com/example/status/12345?ref_src=twsrc", {"type": "tweet"})

        self.assertEqual(meta["final_url"], "https://x.com/example/status/12345")
        self.assertEqual(meta["published_on"], "2026-04-26")
        self.assertEqual(meta["author_handle"], "@example")
        self.assertIn("local-first agent", meta["context_summary"][0])

    def test_task_ledger_treats_user_completion_as_authoritative(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import vault_tasks  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note_dir = root / "items" / "jobs"
            note_dir.mkdir(parents=True)
            note_path = note_dir / "2026-04-26 example ai - research engineer.md"
            note_path.write_text(
                """---
type: job
title: Example AI - Research Engineer
url: https://example.com/job
company: Example AI
role: Research Engineer
discovered_on: 2026-04-26
posted_on: 2026-04-26
status: open
priority: high
application_status: to_apply
---

# Example AI - Research Engineer
""",
                encoding="utf-8",
            )

            sync = vault_tasks.sync_from_vault(root)
            self.assertEqual(sync["created"], 1)
            result = vault_tasks.complete_from_message(
                root,
                message_text="I applied to Example AI, done",
                source_id="telegram:123",
            )
            self.assertEqual(result["completed"], 1)
            tasks = vault_tasks.list_tasks(root, status="all")
            self.assertEqual(tasks[0]["status"], "done")
            updated_note = note_path.read_text(encoding="utf-8")
            self.assertIn("application_status: applied", updated_note)
            self.assertTrue((root / "dashboards" / "tasks.md").exists())

    def test_task_ledger_infers_completion_from_recent_context(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import vault_tasks  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note_dir = root / "items" / "jobs"
            note_dir.mkdir(parents=True)
            note_path = note_dir / "2026-04-26 anthropic - member of technical staff.md"
            note_path.write_text(
                """---
type: job
title: Anthropic - Member of Technical Staff
url: https://anthropic.com/jobs/mts
company: Anthropic
role: Member of Technical Staff
discovered_on: 2026-04-26
posted_on: 2026-04-26
status: open
priority: medium
application_status: to_apply
---

# Anthropic - Member of Technical Staff
""",
                encoding="utf-8",
            )

            vault_tasks.sync_from_vault(root)
            result = vault_tasks.update_from_message(
                root,
                message_text="oh I applied there",
                context_text="Yesterday I saved this Anthropic MTS role https://anthropic.com/jobs/mts",
                source_id="telegram:124",
            )
            self.assertEqual(result["completed"], 1)
            updated_note = note_path.read_text(encoding="utf-8")
            self.assertIn("application_status: applied", updated_note)

    def test_priority_update_marks_matching_note_and_task(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import vault_tasks  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note_dir = root / "items" / "jobs"
            note_dir.mkdir(parents=True)
            note_path = note_dir / "2026-04-26 example robotics - ml engineer.md"
            note_path.write_text(
                """---
type: job
title: Example Robotics - ML Engineer
url: https://example.com/robotics-job
company: Example Robotics
role: ML Engineer
discovered_on: 2026-04-26
posted_on: 2026-04-26
status: open
priority: medium
application_status: to_apply
---

# Example Robotics - ML Engineer
""",
                encoding="utf-8",
            )

            vault_tasks.sync_from_vault(root)
            result = vault_tasks.update_from_message(
                root,
                message_text="this is high priority",
                context_text="Saved Example Robotics ML Engineer https://example.com/robotics-job",
                source_id="telegram:125",
            )
            self.assertEqual(result["priority_updates"], 2)
            self.assertIn("priority: high", note_path.read_text(encoding="utf-8"))
            tasks = vault_tasks.list_tasks(root, status="open")
            self.assertEqual(tasks[0]["priority"], "high")

    def test_telegram_today_command_renders_without_agent(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note_dir = root / "items" / "jobs"
            note_dir.mkdir(parents=True)
            (note_dir / "2026-04-26 example ai - research engineer.md").write_text(
                """---
type: job
title: Example AI - Research Engineer
url: https://example.com/job
company: Example AI
role: Research Engineer
discovered_on: 2026-04-26
posted_on: 2026-04-26
deadline: 2026-04-30
status: open
priority: high
application_status: to_apply
---

# Example AI - Research Engineer
""",
                encoding="utf-8",
            )

            sent: list[dict[str, object]] = []

            def fake_send_message(*args: object, **kwargs: object) -> dict[str, object]:
                sent.append(kwargs)
                return {"ok": True, "result": {"message_id": 10}}

            update = {
                "update_id": 100,
                "message": {
                    "message_id": 20,
                    "date": 1777248000,
                    "text": "/today",
                    "chat": {"id": 123, "type": "private"},
                    "from": {"first_name": "Suraj"},
                },
            }
            with mock.patch.object(telegram_inbox, "telegram_send_message", side_effect=fake_send_message), mock.patch.object(
                telegram_inbox, "telegram_send_chat_action", return_value={"ok": True}
            ):
                result = telegram_inbox.process_update_batch(
                    vault_root=root,
                    token="dummy",
                    session_name="test",
                    allowed_chat_ids=set(),
                    ingest_after_sync=True,
                    agent_model="gpt-5.4",
                    agent_reasoning_effort="medium",
                    updates=[update],
                    mode="test",
                )

            self.assertEqual(result["agent_runs"], 0)
            self.assertEqual(result["answered_messages"], 1)
            self.assertIn("Today’s command center", str(sent[0]["text"]))
            self.assertIn("Example AI", str(sent[0]["text"]))
            self.assertIn("inline_keyboard", sent[0]["reply_markup"])
            self.assertIn("Details", sent[0]["reply_markup"]["inline_keyboard"][0][0]["text"])

    def test_telegram_queue_sends_item_cards_without_details_roundtrip(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / ".vault" / "cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "agent-digest.json").write_text(
                json.dumps(
                    {
                        "schema": "my-vault-agent-digest-v1",
                        "page_count": 1,
                        "pages": [
                            {
                                "path": "items/articles/2026-04-28 fast agents.md",
                                "title": "Fast Agents",
                                "type": "article",
                                "url": "https://example.com/fast-agents",
                                "status": "open",
                                "priority": "medium",
                                "discovered_on": "2026-04-28",
                                "summary": "A useful article about making agent systems faster.",
                                "why_saved": "Relevant to vault latency.",
                                "mtime": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            sent: list[dict[str, object]] = []

            update = {
                "update_id": 103,
                "message": {
                    "message_id": 21,
                    "date": 1777248000,
                    "text": "/queue",
                    "chat": {"id": 123, "type": "private"},
                    "from": {"first_name": "Suraj"},
                },
            }
            with mock.patch.object(telegram_inbox, "telegram_send_message", side_effect=lambda *args, **kwargs: sent.append(kwargs) or {"ok": True}), mock.patch.object(
                telegram_inbox, "telegram_send_chat_action", return_value={"ok": True}
            ):
                result = telegram_inbox.process_update_batch(
                    vault_root=root,
                    token="dummy",
                    session_name="test",
                    allowed_chat_ids=set(),
                    ingest_after_sync=True,
                    agent_model="gpt-5.4",
                    agent_reasoning_effort="medium",
                    updates=[update],
                    mode="test",
                )

            self.assertEqual(result["agent_runs"], 0)
            self.assertEqual(result["answered_messages"], 1)
            self.assertEqual(len(sent), 2)
            self.assertIn("Latest saved queue", str(sent[0]["text"]))
            self.assertIn("Fast Agents", str(sent[1]["text"]))
            labels = [button["text"] for row in sent[1]["reply_markup"]["inline_keyboard"] for button in row]
            self.assertIn("⭐ Prioritize", labels)
            self.assertIn("✅ Mark read/done", labels)
            self.assertIn("Open source", labels)
            self.assertFalse(any("Details" in label for label in labels))

    def test_telegram_callback_can_mark_task_applied(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import telegram_inbox  # type: ignore
        import vault_tasks  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note_dir = root / "items" / "jobs"
            note_dir.mkdir(parents=True)
            note_path = note_dir / "2026-04-26 example ai - research engineer.md"
            note_path.write_text(
                """---
type: job
title: Example AI - Research Engineer
url: https://example.com/job
company: Example AI
role: Research Engineer
discovered_on: 2026-04-26
posted_on: 2026-04-26
deadline: 2026-04-30
status: open
priority: high
application_status: to_apply
---

# Example AI - Research Engineer
""",
                encoding="utf-8",
            )
            vault_tasks.sync_from_vault(root)
            text, markup = telegram_inbox.render_today_command(root)
            self.assertIn("Example AI", text)
            details_callback_data = markup["inline_keyboard"][0][0]["callback_data"]

            update = {
                "update_id": 101,
                "callback_query": {
                    "id": "cb-1",
                    "data": details_callback_data,
                    "message": {"message_id": 30, "chat": {"id": 123, "type": "private"}},
                    "from": {"first_name": "Suraj"},
                },
            }
            sent: list[dict[str, object]] = []
            with mock.patch.object(telegram_inbox, "telegram_answer_callback_query", return_value={"ok": True}), mock.patch.object(
                telegram_inbox, "telegram_send_message", side_effect=lambda *args, **kwargs: sent.append(kwargs) or {"ok": True}
            ):
                result = telegram_inbox.process_update_batch(
                    vault_root=root,
                    token="dummy",
                    session_name="test",
                    allowed_chat_ids=set(),
                    ingest_after_sync=True,
                    agent_model="gpt-5.4",
                    agent_reasoning_effort="medium",
                    updates=[update],
                    mode="test",
                )

            self.assertEqual(result["agent_runs"], 0)
            self.assertEqual(result["answered_messages"], 1)
            self.assertIn("Actions: mark it done/applied", str(sent[0]["text"]))
            applied_callback_data = sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

            apply_update = {
                "update_id": 102,
                "callback_query": {
                    "id": "cb-2",
                    "data": applied_callback_data,
                    "message": {"message_id": 31, "chat": {"id": 123, "type": "private"}},
                    "from": {"first_name": "Suraj"},
                },
            }
            with mock.patch.object(telegram_inbox, "telegram_answer_callback_query", return_value={"ok": True}), mock.patch.object(
                telegram_inbox, "telegram_send_message", side_effect=lambda *args, **kwargs: sent.append(kwargs) or {"ok": True}
            ):
                result = telegram_inbox.process_update_batch(
                    vault_root=root,
                    token="dummy",
                    session_name="test",
                    allowed_chat_ids=set(),
                    ingest_after_sync=True,
                    agent_model="gpt-5.4",
                    agent_reasoning_effort="medium",
                    updates=[apply_update],
                    mode="test",
                )

            self.assertEqual(result["agent_runs"], 0)
            self.assertEqual(result["answered_messages"], 1)
            self.assertIn("Marked applied", str(sent[-1]["text"]))
            updated_note = note_path.read_text(encoding="utf-8")
            self.assertIn("application_status: applied", updated_note)

    def test_session_memory_append_creates_daily_reviewable_file(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import vault_session_memory  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = vault_session_memory.append_memory(
                root,
                surface="telegram",
                summary="User prefers direct useful bot responses.",
                raw_text="make it direct and helpful",
                metadata={"classification": "preference"},
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("User prefers direct useful bot responses", text)
            self.assertIn("classification", text)

    def test_dream_pass_writes_reviewable_output_with_mock_agent(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import vault_dream  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "tools"
            tools.mkdir()
            (tools / "vault_events.py").write_text((REPO_ROOT / "tools" / "vault_events.py").read_text(), encoding="utf-8")
            mock_result = {
                "durable_facts": ["Suraj wants the vault to be agent-first."],
                "preferences": ["Keep responses direct and useful."],
                "decisions_or_systems": ["Use Telegram as always-on ingestion."],
                "task_implications": [],
                "topic_updates": ["Personal knowledge systems are a recurring topic."],
                "should_promote": ["Direct response style"],
                "summary": "Promote direct style and agent-first vault preferences.",
            }
            result = vault_dream.run_dream(root, mock_json=json.dumps(mock_result))
            output = root / result["output"]
            self.assertTrue(output.exists())
            self.assertIn("Promote direct style", output.read_text(encoding="utf-8"))
            self.assertTrue((root / "memory" / "DREAMS.md").exists())

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
