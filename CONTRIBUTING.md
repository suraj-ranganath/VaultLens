# Contributing To VaultLens

Thanks for helping improve VaultLens. This project is a personal-knowledge system, so contribution quality is partly about software correctness and partly about protecting private data.

## Ground Rules

- Do not commit real vault data, screenshots, Telegram payloads, calendar exports, chat exports, or private notes.
- Do not include API keys, bot tokens, OAuth exports, service-account keys, or `.env.local` values in issues or pull requests.
- Keep the vault file-first and inspectable. Avoid designs that hide important state in opaque provider memory.
- Prefer cheap local retrieval, indexes, and deterministic pre-filtering before model calls.
- Make agent behavior observable through traces, citations, logs, or explainable intermediate artifacts.

## Development Setup

```bash
git clone https://github.com/suraj-ranganath/VaultLens.git
cd VaultLens
npm ci
cp .env.example .env.local
npm run vault:setup
```

Only fill `.env.local` with real credentials on your own machine. It is ignored by Git.

## Useful Commands

```bash
npm test
npm run vault:compile
npm run rebuild:dashboards
npm run vault:web
npm run telegram:webhook:test < /path/to/redacted-update.json
```

For JavaScript entrypoints touched by your change, run `node --check` before opening a PR.

## Pull Request Checklist

- The change does not commit personal vault state or secrets.
- Tests pass, or the PR explains why a test could not be run.
- New user-facing behavior is documented in `README.md`, `cloud/README.md`, or inline help.
- New generated files are either ignored or intentionally tracked.
- Agent prompts include clear source/citation/privacy expectations.
- Cloud changes are cost-aware and do not introduce always-on compute by default.

## Architecture Expectations

VaultLens has a few non-negotiable design constraints:

- Markdown and portable assets are the durable knowledge layer.
- `raw/` and `imports/` are source layers and should not be rewritten in place.
- Canonical notes live under `items/`, `topics/`, `projects/`, `dashboards/`, and `outputs/`.
- `.vault/` is runtime state: caches, indexes, traces, reports, events, and queues.
- AWS, Telegram, and browser automation are interfaces over the vault, not replacements for the vault.

## Reporting Bugs

When filing a bug, include:

- the command or workflow that failed
- relevant sanitized logs
- expected behavior
- actual behavior
- OS, Node, Python, and deployment mode if relevant

Do not paste raw Telegram updates, real calendar event details, private profile notes, or live credentials.

## Feature Requests

Good feature requests explain:

- what capture or retrieval problem you are trying to solve
- whether the feature is local-only, cloud-safe, or both
- expected cost impact if it uses model calls, web search, browser automation, or AWS resources
- how the feature should remain inspectable as files
