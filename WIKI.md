# VaultLens Wiki Contract

This file is the reference contract for the vault.

The vault is a persistent, compounding markdown knowledge base. Chat is only the interface. The durable artifact is the file system.

## Layers

1. `raw/` and `imports/` hold immutable source material.
2. `items/`, `topics/`, `projects/`, `dashboards/`, and `outputs/` hold the compiled knowledge system.
3. `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and the other bootstrap files tell agents how to maintain the vault.

## Core Surfaces

- `hot.md`: cross-session hot cache. Rewritten, not appended.
- `index.md`: root navigation page for the vault.
- `log.md`: append-only operational history.
- `dashboards/dashboard.base`: native Obsidian Bases dashboard.
- `dashboards/vault-health.md`: lint-style health report for weak notes and missing metadata.

## Immutable Inputs

- `raw/` and `imports/` are source-only.
- Source exports are never edited in place.
- `raw/.manifest.json` tracks ingested source files, hashes, and output summaries.

## Canonical Knowledge

- `items/` contains exactly one canonical note per revisit-worthy item.
- `topics/` contains durable synthesis and thematic rollups.
- `projects/` contains active workspaces that gather relevant items, decisions, and outputs.
- `outputs/` contains reusable answer artifacts, briefs, and derived deliverables.

## Retrieval Rules

- Latest shared material should surface first in operational dashboards.
- Job notes preserve website-posted date when known, but recency still controls primary surfacing.
- Decisions and systems are first-class context.
- Weak live pages should be strengthened with browser enrichment or first-party artifacts.
- X posts should prefer browser extraction over plain fetches.

## Maintenance Rules

- `hot.md` should stay under about 500 words.
- `log.md` is append-only.
- `vault-health.md` should be regenerated after major ingest or enrichment passes.
- `dashboard.base` should be regenerated when the canonical note corpus changes materially.
