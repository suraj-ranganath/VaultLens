# VaultLens Cloud Telegram Webhook

This deploys the VaultLens Telegram agent as a webhook-first AWS path.

## Architecture

- Telegram sends messages to an AWS Lambda Function URL.
- The receiver Lambda validates Telegram's `X-Telegram-Bot-Api-Secret-Token` header.
- The receiver stores the raw update in S3 and asynchronously invokes a processor Lambda.
- The processor Lambda restores the ignored vault state bundle from S3 into `/tmp/vault-lens`.
- The processor waits briefly, collects pending webhook events from S3, then runs `tools/telegram_inbox.py webhook` with a small batch so rapid Telegram messages can be coalesced instead of forcing separate agent turns.
- Codex runs default to `VAULT_CODEX_SANDBOX=full_access`, so the agent can write vault files and run multi-step shell workflows inside the restored working tree.
- Saved Telegram content gets an agentic `vault-work` pass by default (`VAULT_AGENTIC_WORK_AUTO=true`). That pass can inspect the vault, run shell workflows, follow links, write canonical notes, update topics/projects/decisions/systems, improve memory-review artifacts, and adjust dashboards/templates/indexes when retrieval quality benefits.
- The Telegram worker uses preview-message edits for progress, then edits that same preview into the final answer or acknowledgement when possible.
- Telegram command-center requests (`/today`, `/queue`, `/status`, `/trace`) and inline button callbacks are handled by deterministic local code inside the same processor, so common state updates do not spend LLM tokens.
- The query path compiles `.vault/cache/`, runs local SQLite FTS retrieval, and only then calls the Codex-backed answer agent.
- Cache compilation writes digest/search/report artifacts atomically and produces claim-health reports for contradictions, stale claims, open questions, and low-confidence evidence. Search defaults to local SQLite FTS/BM25; OpenAI API embeddings have been removed.
- Web, Telegram, and morning-brief runs write redacted trajectory sidecars under `.vault/trajectories/`; Telegram outbound messages are queued under `.vault/telegram-delivery-queue/` if sendMessage fails and retried on the next worker run.
- X/Twitter links use the lightweight `tools/x_content.py` adapter during metadata enrichment. In cloud this normally uses public oEmbed; locally it can use `xurl` first when installed and authenticated.
- Browser-heavy sources such as X, LinkedIn, Instagram, and explicit "fully extract this" requests can trigger a separate browser worker Lambda with Playwright installed. That worker restores the same S3 state bundle, captures artifacts under `raw/web-clips/browser-artifacts/`, updates notes, rebuilds dashboards/cache, and uploads the state bundle back to S3.
- After processing, the processor writes one compressed state bundle back to S3.

The processor has `ReservedConcurrentExecutions: 1` so two Telegram messages cannot race with each other. The processor, heartbeat, and browser worker also take a small S3 mutex around state download/run/upload so separate worker classes do not overwrite the same bundle. The short S3 pending-event sweep reduces redundant runs during bursts.

## Why This Is Cost Effective

- No always-on instance.
- No API Gateway bill: Lambda Function URL is used directly.
- S3 stores the ignored vault data cheaply.
- S3 request cost stays low because the ignored vault state is stored as one compressed bundle instead of thousands of tiny objects.
- Lambda runs only when Telegram sends a message or the daily brief schedule fires.
- The browser worker has its own image and only runs when browser-heavy enrichment is triggered, so ordinary Telegram messages do not pay for Playwright startup.
- Scheduled morning brief surfacing is disabled by default. If enabled, EventBridge Scheduler invokes the existing processor once per day instead of deploying another image.
- Secrets use encrypted Lambda environment variables rather than Secrets Manager, avoiding a recurring Secrets Manager charge.

This is optimized for a personal bot with occasional bursts. If usage becomes heavy, the next upgrade is SQS FIFO between receiver and processor, but that is intentionally not the default.

## Prerequisites

- AWS CLI configured with the account that has your AWS credits.
- AWS SAM CLI installed.
- Docker installed for Lambda image builds.
- Local Codex ChatGPT auth from `codex login --device-auth`, unless you have `CODEX_ACCESS_TOKEN`.
- `.env.local` containing:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
AWS_REGION=us-west-2
STACK_NAME=vault-lens-telegram
VAULT_CODEX_AUTH_S3_KEY=codex-auth/auth.json
VAULT_CODEX_SANDBOX=full_access
VAULT_AGENTIC_WORK_AUTO=true
VAULT_WORK_WEB_SEARCH_ENABLED=true
VAULT_WORK_REASONING_EFFORT=medium
VAULT_TELEGRAM_STREAMING_ENABLED=true
VAULT_TELEGRAM_STREAMING_TRANSPORT=auto
VAULT_BROWSER_AUTO_TRIGGER=true
HEARTBEAT_ENABLED=false
```

For ChatGPT Pro/Plus, `bun run cloud:deploy` uploads your local `~/.codex/auth.json` to the private state bucket at `VAULT_CODEX_AUTH_S3_KEY`, then Lambda restores it into `CODEX_HOME` at runtime. For ChatGPT Business/Enterprise, `CODEX_ACCESS_TOKEN` remains supported and is passed as a no-echo CloudFormation parameter instead.

`TELEGRAM_ALLOWED_CHAT_IDS` is optional, but strongly recommended so only your Telegram chat can use the bot.

## Google Calendar Actions

Calendar writes use the Google Workspace CLI (`gws`) installed through Bun from `package.json`. The Telegram worker always asks for clarification or confirmation before calling `gws`.

Local setup:

```bash
gws auth setup
gws auth login --scopes https://www.googleapis.com/auth/calendar
```

Cloud setup, preferred durable path:

1. Create a service account in the same Google Cloud project.
2. Create a JSON key for that service account.
3. Share the target Google Calendar with the service account email as `Make changes to events`.
4. Put the service-account JSON and real target calendar ID into `.env.local`:

```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON='{"type":"service_account", "...":"..."}'
VAULT_CALENDAR_ID='your_calendar_id@example.com'
```

This avoids user OAuth refresh-token expiry in Lambda. For service accounts, do not use `primary` unless you intentionally want the service account's own calendar.

Cloud setup, OAuth fallback:

```bash
gws auth export --unmasked > /tmp/gws-credentials.json
```

Then put the one-line JSON value into `.env.local`:

```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON='{"...":"..."}'
VAULT_CALENDAR_ID='your_calendar_id@example.com'
```

The deploy script passes credentials as a no-echo CloudFormation parameter. The Lambda writes them to `/tmp/vault-lens/.runtime/google-workspace-credentials.json` at runtime, points `gws` at that file, and forces `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` so the cloud worker does not depend on an OS keyring. Do not commit this value.

## Deploy

```bash
bun run cloud:deploy
```

The deploy script:

- generates `TELEGRAM_WEBHOOK_SECRET` if it is missing and appends it to `.env.local`
- builds the Lambda container image
- builds the separate browser-worker image with Playwright browsers
- installs an ECR lifecycle policy so old deployment images do not accumulate indefinitely
- deploys the CloudFormation stack
- syncs local Codex ChatGPT auth to the private state bucket when `CODEX_ACCESS_TOKEN` is not set
- registers the Lambda Function URL with Telegram via `setWebhook`

If you re-run `codex login --device-auth`, rotate ChatGPT auth, or see cloud auth failures, refresh the uploaded auth file:

```bash
bun run cloud:sync-codex-auth
```

### Cost Cleanup

Deploys push container images to ECR. The current deploy script keeps a small rollback window by default:

```bash
ECR_KEEP_APP_IMAGES=4
ECR_KEEP_BROWSER_IMAGES=2
ECR_KEEP_TOTAL_IMAGES=6
ECR_UNTAGGED_EXPIRE_DAYS=1
```

To apply lifecycle policies to current and legacy VaultLens repositories, prune stale images immediately, and disable old `my-vault-telegram-*` morning-brief schedules:

```bash
bun run cloud:cleanup
```

Preview first with:

```bash
bun run cloud:cleanup -- --dry-run
```

The cleanup command protects image digests still referenced by Lambda functions. It does not delete S3 vault state buckets, because old state bundles are safer to keep as recovery backups until you explicitly decide to remove them.

If you also want to delete legacy ECR repositories that no Lambda function references anymore:

```bash
bun run cloud:cleanup -- --delete-unused-legacy-repos
```

## Seed Or Refresh Cloud Vault State

The cloud worker cannot see local ignored files unless they are synced to the state bucket.

Run this after the first deploy and whenever you want to push local-only vault data up to the cloud worker:

```bash
bun run cloud:sync-state
```

The script uploads one compressed `state/vault-state.tar.gz` bundle containing:

- `.vault/`
- `dashboards/`
- `imports/`
- `items/`
- `outputs/`
- `projects/`
- `raw/`
- `topics/`
- `hot.md`
- `index.md`
- `log.md`

`.vault/` is included because AWS is the canonical vault state and the compiled cache/search/event surfaces are durable runtime state, but it remains ignored by Git.

## Browser Enrichment Worker

The browser worker is deployed separately from the fast Telegram processor. It is invoked automatically for browser-heavy Telegram messages when `VAULT_BROWSER_AUTO_TRIGGER=true`, and can be run manually:

```bash
bun run cloud:browser-enrich
```

The worker downloads the canonical S3 state bundle, runs `tools/enrich_with_browser.mjs`, captures screenshots/JSON artifacts under `raw/web-clips/browser-artifacts/`, follows a bounded number of linked targets, rebuilds dashboards and `.vault/cache/`, then uploads the state bundle back to S3.

## Optional Daily Morning Brief

Daily morning briefs are off by default to keep costs down. To enable the focused 9am Pacific Telegram brief, deploy with:

```bash
HEARTBEAT_ENABLED=true TELEGRAM_HEARTBEAT_CHAT_ID=123456789 bun run cloud:deploy
```

The schedule defaults to `cron(0 9 * * ? *)` in `America/Los_Angeles`. Override with `HEARTBEAT_SCHEDULE` and `HEARTBEAT_SCHEDULE_TIMEZONE` if needed.

The heartbeat path reuses the processor function and runs `tools/vault_heartbeat.py`. That script cheaply builds a candidate pack from deadlines, reminders, jobs, opportunities, today's Google Calendar events, recent saves, profile context, dashboards, and hot context, then calls the shared Codex Python runner to choose and write the actual brief. The deterministic layer is only a shortlist; the agent decides what is relevant and personalized enough to send. Web search is disabled for the scheduled brief by default to keep it grounded in the canonical vault.

Calendar events are fetched through the same `gws` credentials used by Telegram calendar actions. The default calendar is `VAULT_CALENDAR_ID`, falling back to `VAULT_BRIEF_CALENDAR_ID`, then `primary`. Set `VAULT_BRIEF_INCLUDE_CALENDAR=false` to disable calendar fetching if credentials are unavailable.

## Local Webhook Test

You can test the non-polling path locally by piping a Telegram update JSON into:

```bash
bun run telegram:webhook:test < /tmp/telegram-update.json
```

## Operational Notes

- CloudWatch Logs for the processor show the Codex and ingest failures if a message fails.
- Telegram retries failed webhook deliveries, and the local processed-update ledger prevents duplicate processing once state has been synced.
- The processor image intentionally remains lightweight. Browser-heavy enrichment runs in `BrowserWorkerFunction`, which has Playwright Chromium installed and defaults to a small bounded pass over recent weak notes.
- Vault Q&A responses stream in Telegram by default. `VAULT_TELEGRAM_STREAMING_TRANSPORT=auto` uses `sendMessageDraft` for private chats when Telegram accepts it, then falls back to edit-message streaming for groups or draft failures. Tune `VAULT_TELEGRAM_DRAFT_INTERVAL_MS`, `VAULT_TELEGRAM_EDIT_INTERVAL_MS`, and `VAULT_TELEGRAM_STREAMING_MAX_CHARS` if you hit API-rate or preview-length limits.
- Set `VAULT_AGENTIC_WORK_AUTO=false` only if you want saved Telegram items to use deterministic ingest without the full Codex filing/synthesis pass. The default should remain `true` for a personalized, agent-maintained vault.
- X/Twitter post text does not require Playwright in the common case: the processor first tries the `x_content` adapter through live metadata enrichment. Browser enrichment is the fallback for posts, cards, redirects, and linked targets that oEmbed/live adapters cannot recover.
- Set `VAULT_BROWSER_AUTO_TRIGGER=false` if you want browser enrichment to be manual only. Tune `VAULT_BROWSER_ENRICH_LIMIT`, `VAULT_BROWSER_ENRICH_CONCURRENCY`, `VAULT_BROWSER_ENRICH_LOOKBACK_DAYS`, and `VAULT_BROWSER_MAX_LINK_HOPS` to control cost and runtime.
- `.vault/events/agent-events.jsonl` is the cross-surface event log for web queries, Telegram processing, Codex usage, and future diagnostics.
