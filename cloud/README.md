# Cloud Telegram Webhook

This deploys the vault Telegram agent as a webhook-first AWS path.

## Architecture

- Telegram sends messages to an AWS Lambda Function URL.
- The receiver Lambda validates Telegram's `X-Telegram-Bot-Api-Secret-Token` header.
- The receiver stores the raw update in S3 and asynchronously invokes a processor Lambda.
- The processor Lambda restores the ignored vault state bundle from S3 into `/tmp/my-vault`.
- The processor waits briefly, collects pending webhook events from S3, then runs `tools/telegram_inbox.py webhook` with a small batch so rapid Telegram messages can be coalesced instead of forcing separate agent turns.
- The Telegram worker uses preview-message edits for progress, then edits that same preview into the final answer or acknowledgement when possible.
- The query path compiles `.vault/cache/`, runs local SQLite FTS retrieval, and only then calls the Codex-backed answer agent.
- X/Twitter links use the lightweight `tools/x_content.py` adapter during metadata enrichment. In cloud this normally uses public oEmbed; locally it can use `xurl` first when installed and authenticated.
- After processing, the processor writes one compressed state bundle back to S3.

The processor has `ReservedConcurrentExecutions: 1` so two Telegram messages cannot race while writing the same vault files. The short S3 pending-event sweep reduces redundant runs during bursts.

## Why This Is Cost Effective

- No always-on instance.
- No API Gateway bill: Lambda Function URL is used directly.
- S3 stores the ignored vault data cheaply.
- S3 request cost stays low because the ignored vault state is stored as one compressed bundle instead of thousands of tiny objects.
- Lambda runs only when Telegram sends a message or the daily brief schedule fires.
- Scheduled morning brief surfacing is disabled by default. If enabled, EventBridge Scheduler invokes the existing processor once per day instead of deploying another image.
- Secrets use encrypted Lambda environment variables rather than Secrets Manager, avoiding a recurring Secrets Manager charge.

This is optimized for a personal bot with occasional bursts. If usage becomes heavy, the next upgrade is SQS FIFO between receiver and processor, but that is intentionally not the default.

## Prerequisites

- AWS CLI configured with the account that has your AWS credits.
- AWS SAM CLI installed.
- Docker installed for Lambda image builds.
- `.env.local` containing:

```bash
OPENAI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
AWS_REGION=us-west-2
STACK_NAME=my-vault-telegram
```

`TELEGRAM_ALLOWED_CHAT_IDS` is optional, but strongly recommended so only your Telegram chat can use the bot.

## Google Calendar Actions

Calendar writes use the Google Workspace CLI (`gws`) bundled through npm. The Telegram worker always asks for clarification or confirmation before calling `gws`.

Local setup:

```bash
gws auth setup
gws auth login --scopes calendar
```

Cloud setup:

```bash
gws auth export --unmasked > /tmp/gws-credentials.json
```

Then put the one-line JSON value into `.env.local`:

```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON='{"...":"..."}'
```

The deploy script passes this as a no-echo CloudFormation parameter. The Lambda writes it to `/tmp/my-vault/.runtime/google-workspace-credentials.json` at runtime and points `gws` at that file. Do not commit this value.

## Deploy

```bash
npm run cloud:deploy
```

The deploy script:

- generates `TELEGRAM_WEBHOOK_SECRET` if it is missing and appends it to `.env.local`
- builds the Lambda container image
- deploys the CloudFormation stack
- registers the Lambda Function URL with Telegram via `setWebhook`

## Seed Or Refresh Cloud Vault State

The cloud worker cannot see local ignored files unless they are synced to the state bucket.

Run this after the first deploy and whenever you want to push local-only vault data up to the cloud worker:

```bash
npm run cloud:sync-state
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

## Optional Daily Morning Brief

Daily morning briefs are off by default to keep costs down. To enable the focused 8am Pacific Telegram brief, deploy with:

```bash
HEARTBEAT_ENABLED=true TELEGRAM_HEARTBEAT_CHAT_ID=123456789 npm run cloud:deploy
```

The schedule defaults to `cron(0 8 * * ? *)` in `America/Los_Angeles`. Override with `HEARTBEAT_SCHEDULE` and `HEARTBEAT_SCHEDULE_TIMEZONE` if needed.

The heartbeat path reuses the processor function, runs `tools/vault_heartbeat.py`, and avoids an LLM call. It sends a Telegram message only when the canonical vault has urgent next-7-day deadlines/reminders, fresh high-impact jobs/opportunities, or one high-value recent reading. If there is no meaningful brief, it exits after a cheap local scan without messaging you.

## Local Webhook Test

You can test the non-polling path locally by piping a Telegram update JSON into:

```bash
npm run telegram:webhook:test < /tmp/telegram-update.json
```

## Operational Notes

- CloudWatch Logs for the processor show the Codex and ingest failures if a message fails.
- Telegram retries failed webhook deliveries, and the local processed-update ledger prevents duplicate processing once state has been synced.
- Playwright browser enrichment is intentionally not bundled with browsers in this Lambda image. Browser-heavy enrichment should stay local or move to a separate scheduled worker if it becomes essential in the cloud.
- X/Twitter post text does not require Playwright in the common case: the cloud worker first tries the `x_content` adapter through live metadata enrichment. Browser enrichment remains the local fallback for posts that oEmbed/live adapters cannot recover.
- `.vault/events/agent-events.jsonl` is the cross-surface event log for web queries, Telegram processing, costs, and future diagnostics.
