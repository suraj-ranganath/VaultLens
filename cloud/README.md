# Cloud Telegram Webhook

This deploys the vault Telegram agent as a webhook-first AWS path.

## Architecture

- Telegram sends messages to an AWS Lambda Function URL.
- The receiver Lambda validates Telegram's `X-Telegram-Bot-Api-Secret-Token` header.
- The receiver stores the raw update in S3 and asynchronously invokes a processor Lambda.
- The processor Lambda restores ignored vault state from S3 into `/tmp/my-vault`.
- The processor runs `tools/telegram_inbox.py webhook`, which uses the same Codex-backed decision, ingest, query, and acknowledgement path as local polling.
- After processing, the processor syncs updated vault state back to S3.

The processor has `ReservedConcurrentExecutions: 1` so two Telegram messages cannot race while writing the same vault files.

## Why This Is Cost Effective

- No always-on instance.
- No API Gateway bill: Lambda Function URL is used directly.
- S3 stores the ignored vault data cheaply.
- Lambda runs only when Telegram sends a message.
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

The script uploads:

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

## Local Webhook Test

You can test the non-polling path locally by piping a Telegram update JSON into:

```bash
npm run telegram:webhook:test < /tmp/telegram-update.json
```

## Operational Notes

- CloudWatch Logs for the processor show the Codex and ingest failures if a message fails.
- Telegram retries failed webhook deliveries, and the local processed-update ledger prevents duplicate processing once state has been synced.
- Playwright browser enrichment is intentionally not bundled with browsers in this Lambda image. Browser-heavy enrichment should stay local or move to a separate scheduled worker if it becomes essential in the cloud.
