# VaultLens: GitHub Copilot Instructions

This repository tracks the infrastructure for a local-first, agent-maintained markdown knowledge vault. Personal data and note content stay ignored locally. The tracked repo contains tooling, templates, setup assets, and agent instructions.

## Conventions

1. Read `AGENTS.md` for the real contract before editing tooling.
2. `raw/` and `imports/` are immutable source layers.
3. `hot.md` is rewritten on rebuild. `log.md` is append-only.
4. `dashboard.base` is the native Obsidian Bases view. Markdown dashboards remain the fallback operational layer.
5. Latest shared material should surface first.
6. Job notes preserve `posted_on` metadata but are not primarily sorted by it in operational views.
7. Browser enrichment should focus on recent weak links, especially X posts.
