# My Vault

This repo contains the infrastructure for a local-first, agent-maintained personal wiki inspired by the wiki-first knowledge base pattern. The goal is not just to save links, but to turn a stream of jobs, articles, tweets, reminders, ideas, systems, and decisions into explicit files that an agent can crawl, update, and query well.

The actual personal data stays local and is intentionally ignored by Git. This repository is only for code, templates, and operating rules.

## What This System Is Trying To Do

- ingest messy capture streams from chat exports and Telegram
- compile them into durable markdown notes with backlinks and structured metadata
- surface the right things at the right time through dashboards and retrieval-oriented note structure
- preserve decisions, reasoning, and personal systems, not just facts and links
- prefer explicit files over opaque app memory so any model or tool can operate on the knowledge base

## Repository Scope

Tracked here:

- `tools/` for ingestion, dashboard rebuilds, artifact attachment, and browser enrichment
- `templates/` for canonical note shapes
- `config/obsidian/` for tracked Obsidian defaults that can be installed into a local vault
- `hooks/` for hot-cache style hook definitions
- `bin/setup-vault.sh` for local vault bootstrap
- `AGENTS.md` for the operating rules the agent follows
- `WIKI.md`, `CLAUDE.md`, `GEMINI.md`, and agent bootstrap files for cross-tool consistency
- `package.json` for the Codex SDK and Playwright toolchain
- `.env.example` for local configuration shape

Ignored from Git:

- `items/`, `topics/`, `projects/`, `dashboards/`, `raw/`, `imports/`, and `outputs/`
- `.env.local` and any other secret-bearing env files
- `hot.md`, `index.md`, and `log.md`
- Obsidian local state and dependency directories

## Model And Agent Path

- primary agent model: `gpt-5.4`
- agent runtime for Telegram ingestion: Codex SDK
- browser fallback for weak pages: Playwright

The browser fallback is intentionally focused on recent material. The current policy is to run Playwright enrichment only for notes discovered in the last 30 days, with special attention to X and other pages that are weak or blocked under plain HTTP fetches.

Borrowed and adapted from the `claude-obsidian` pattern:

- `hot.md` as a cross-session cache
- `dashboards/dashboard.base` as a native Obsidian Bases view
- `dashboards/vault-health.md` as a lint-style maintenance surface
- tracked Obsidian config templates plus a setup script
- multi-agent bootstrap docs so different tools share the same vault contract

## Core Workflows

### 0. Install local Obsidian defaults

```bash
npm run vault:setup
```

### 1. Ingest a chat export

```bash
python3 tools/ingest_chat_export.py --help
python3 tools/ingest_whatsapp_inbox.py --vault-root .
```

### 2. Rebuild dashboards from current notes

```bash
npm run rebuild:dashboards
```

This rebuild also refreshes:

- `hot.md`
- `index.md`
- `dashboards/dashboard.base`
- `dashboards/dashboard.md`
- `dashboards/vault-health.md`

### 3. Enrich recent weak links with Playwright

```bash
npm run enrich:browser:recent
```

Equivalent direct command:

```bash
node tools/enrich_with_browser.mjs . 120 4 30
```

Arguments are:

1. vault root
2. max notes to inspect
3. concurrency
4. lookback window in days

### 4. Run Telegram ingestion

```bash
cp .env.example .env.local
```

Set `OPENAI_API_KEY` and `TELEGRAM_BOT_TOKEN`, then:

```bash
npm run telegram:sync
npm run telegram:run
```

The Telegram receiver stores raw updates locally, appends a normalized inbox stream, lets a local Codex agent decide how to classify or file each message, and acknowledges successful ingestion with `👍`. If the machine is down, the next sync catches up from Telegram history.

### 5. Run a health check directly

```bash
npm run vault:health
```

### 6. Run the local web Q&A interface

```bash
npm run vault:web
```

Then open `http://localhost:4318`.

The web interface runs a local Codex-backed query route over the vault. It is designed for retrieval-first answering:

- local vault files are the primary source of truth
- the agent starts from `hot.md`, dashboards, and canonical notes
- answers return citations to vault files plus a live agent feed
- the live feed shows reasoning summaries, plan updates, shell commands, web searches, MCP calls, and surfaced agent messages
- useful answers can be filed back into `outputs/` directly from the UI
- web search is optional and off by default so the query path stays vault-first

Environment knobs:

- `VAULT_QUERY_PORT`
- `VAULT_QUERY_DEFAULT_MODEL`
- `OPENAI_API_KEY`

## Knowledge Design Principles

- explicit and inspectable over hidden memory
- file over app
- local-first ownership of data
- newest shared material should surface first in dashboards
- job notes should preserve website-posted date when known
- decisions and systems should be first-class notes
- recent context should be available through a compact hot cache
- maintenance should be explicit through a lint-style health page
- outputs from future queries should be filed back into the wiki so the corpus compounds
