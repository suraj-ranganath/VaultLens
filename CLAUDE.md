# My Vault

This repository is a local-first personal knowledge vault. Read `AGENTS.md` for the full operating contract and `WIKI.md` for the architectural reference.

## Session Startup

1. If `hot.md` exists, read it silently first.
2. Use `index.md` as the root navigation page.
3. Prefer `dashboards/` before drilling into individual notes.

## Core Rules

- Never modify `raw/` or `imports/` source files in place.
- Canonical knowledge lives in `items/`, `topics/`, `projects/`, `dashboards/`, and `outputs/`.
- Latest shared material should surface first.
- Jobs preserve `posted_on` when known, but dashboards are recency-first.
- `log.md` is append-only.
- `hot.md` is overwritten, not appended.

## Key Local Commands

```bash
npm run rebuild:dashboards
npm run enrich:browser:recent
python3 tools/vault_health.py
```
