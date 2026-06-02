# Security Policy

VaultLens handles sensitive personal data by design. Treat every vault as private unless the owner explicitly says otherwise.

## Supported Versions

The `main` branch is the only supported branch for security fixes.

## Reporting A Vulnerability

Please use GitHub private vulnerability reporting if it is enabled for the repository. If not, open a minimal public issue that says a security report is available, but do not include exploit details, credentials, private vault data, Telegram payloads, screenshots, or calendar details.

## Secret Handling

Never commit:

- `.env.local`, `.env`, or any `*.env` file
- Codex access tokens or copied Codex account auth files
- Telegram bot tokens
- Google OAuth exports or service-account JSON keys
- AWS credentials
- raw Telegram updates
- chat exports
- screenshots or images from a private vault
- `.vault/`, `raw/`, `imports/`, `items/`, `topics/`, `projects/`, `dashboards/`, `outputs/`, or `memory/`

Before publishing a fork or opening a PR, run:

```bash
git status --short --ignored
rg -n "CODEX_ACCESS_TOKEN|TELEGRAM_BOT_TOKEN|BEGIN PRIVATE KEY|GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON|AWS_SECRET_ACCESS_KEY" -S --hidden -g '!node_modules' -g '!.git'
```

If a secret was committed, rotate it immediately. Removing it from the latest commit is not enough once it has been pushed.

## Deployment Notes

- The AWS deployment uses encrypted Lambda environment variables by default.
- S3 buckets created by the template block public access and enable server-side encryption.
- The processor Lambda uses single concurrency to reduce state-write races.
- `TELEGRAM_ALLOWED_CHAT_IDS` is strongly recommended for any bot that is reachable from Telegram.
- Service-account calendar credentials should be scoped by sharing only the required calendar, not an entire Google Workspace domain.

## Privacy Model

VaultLens is not a hosted service. Operators are responsible for their own Codex account, Telegram, AWS, Google, and browser-session privacy posture. The repo aims to keep private data in ignored local/cloud vault state, but each deployment should be reviewed before use with sensitive material.
