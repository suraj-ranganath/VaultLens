# AGENTS.md

This vault is an AWS-backed, agent-maintained markdown wiki for personal knowledge capture, opportunity tracking, decision tracking, and retrieval. The canonical vault state lives in AWS so Telegram can remain the always-on ingestion path. Local checkouts, Obsidian views, and web interfaces are working copies and operator surfaces over that canonical state. The agent owns structure, bookkeeping, surfacing, filing, sync discipline, and maintenance. The user owns source curation, priority, and intent.

## Operating Philosophy

- Build for agent retrieval first. A strong file layout, stable summaries, backlinks, and clear indexes beat opaque app memory.
- Keep all important knowledge explicit and inspectable in markdown and portable assets.
- Prefer universal files over provider-specific memory systems.
- Treat the compiled wiki as a long-term artifact that multiple AI tools can operate on.
- Store decisions, verdicts, and systems alongside knowledge so future work starts from prior reasoning instead of from scratch.
- Treat the AWS-hosted vault state as canonical. Local environments may enrich, inspect, or render the vault, but they should sync back to the cloud state instead of silently diverging from it.
- Optimize new development around two goals: enriching the canonical AWS knowledge base and making it easier to interface with through Telegram, web chat, Obsidian, and future surfaces.

## Directory Contract

- `raw/`: immutable source corpus within the vault state
- `raw/chat-exports/`: chat exports and message dumps
- `raw/web-clips/`: clipped web articles and pages
- `raw/images/`: images, screenshots, and visual inspiration
- `raw/docs/`: local documents, notes, and attachments
- `imports/chat-exports/`: compatibility location for chat exports provided by the user
- `items/jobs/`: canonical job opportunity notes
- `items/articles/`: canonical article notes
- `items/thoughts/`: canonical notes for personal thoughts, ideas, observations, and self-notes worth revisiting
- `items/events/`: canonical event notes
- `items/tweets/`: canonical tweet or post notes
- `items/reminders/`: canonical reminder notes
- `items/opportunities/`: canonical non-job opportunity notes
- `items/resources/`: canonical notes for docs, repos, videos, papers, talks, tools, and other reusable references
- `items/decisions/`: canonical decisions, verdicts, and their reasoning
- `items/systems/`: canonical notes for recurring workflows, personal operating principles, and systems
- `items/misc/`: canonical notes that do not fit another bucket
- `topics/`: durable synthesis, comparison, and thematic pages
- `projects/`: active and archived project pages that collect relevant knowledge and decisions
- `dashboards/`: operational views for urgency and retrieval
- `outputs/`: reusable generated artifacts such as briefs, research notes, slide decks, and answer files
- `memory/`: ignored daily session memory and reviewable dreamed memory promotions used to personalize future agent runs
- `templates/`: note templates used during ingest and maintenance
- `index.md`: catalog of major pages
- `hot.md`: compact cross-session cache of recent context
- `log.md`: append-only operational log
- `raw/.manifest.json`: ingest manifest for source hashes and output summaries

## Canonical State Contract

- AWS is the canonical source of truth for vault data.
- Canonical state currently consists of:
  - the vault state bundle stored in S3
  - immutable Telegram webhook event payloads stored in S3
  - cloud-side processing that updates the vault state from new Telegram messages
- GitHub is only for code, templates, infra, and documentation. Never commit personal vault data.
- Local vault files are editable working copies for development, debugging, Obsidian browsing, browser-based enrichment, and repair work.
- Any local workflow that changes vault content must be designed to sync changes back to the canonical AWS state.
- Browser automation enrichment may remain local-only when that is materially cheaper or operationally simpler, but any durable metadata or note updates produced from it should be written back into canonical state.
- Browser automation should write browser artifact packs under `raw/web-clips/browser-artifacts/` when it gets past blocked or dynamic pages. These packs are durable evidence, not just summaries.

## Source Handling

- Treat everything inside `raw/` and append-only ingress logs under `imports/` as immutable once captured into canonical state.
- Never rewrite or delete a user-provided export.
- Record source provenance in canonical notes via `source_export` and `source_excerpt`.
- Prefer storing source artifacts in `raw/`.
- Use the source file as the durable raw artifact even after the compiled wiki is updated.
- Treat `imports/whatsapp-inbox/` as the staging area for self-group WhatsApp exports before they are copied into `raw/chat-exports/`.
- Treat `imports/telegram-inbox/` as the live normalized stream for bot-captured Telegram messages.
- Treat `raw/telegram-updates/` as the immutable append-only raw source for Telegram bot updates.
- Treat S3-backed canonical state as the master copy for those artifacts and notes. Local copies are mirrors or temporary workspaces unless explicitly synced back.

## Canonical Note Rules

- Create exactly one canonical note per revisit-worthy item.
- De-duplicate by canonical URL first.
- If no URL exists, de-duplicate by normalized title plus source context.
- Update canonical notes in place when later exports provide better metadata.
- Prefer human-readable filenames that remain stable once created.
- Prefer topical retrievability over strict source fidelity when choosing titles and tags.
- Maintain backlinks from topic pages and project pages whenever an item becomes important to them.
- Keep concise summaries near the top of each page so an agent can crawl quickly.

### Filename Rules

- Jobs: `items/jobs/YYYY-MM-DD company - role.md` when `posted_on` is known
- Jobs with unknown posted date: `items/jobs/undated company - role.md`
- Decisions: `items/decisions/YYYY-MM-DD short-verdict.md`
- Systems: `items/systems/short-system-name.md`
- Other item types: `items/<type>/YYYY-MM-DD short-title.md` when `published_on` is known
- Other item types with unknown published date: use `discovered_on`
- Projects: `projects/project-name.md`
- Outputs: `outputs/YYYY-MM-DD short-output-title.md`

## Frontmatter Contract

All canonical item notes must include these common fields:

```yaml
type:
title:
url:
source_export:
source_excerpt:
discovered_on:
published_on:
deadline:
status:
priority:
tags: []
topics: []
why_saved:
revisit_after:
last_relevant_on:
timeliness:
interest_signals: []
date_confidence:
```

Jobs must also include:

```yaml
company:
role:
location:
employment_type:
posted_on:
application_status:
deadline_type:
requires_referral:
```

Decisions should also include:

```yaml
decision_domain:
verdict:
rationale:
related_projects: []
review_after:
supersedes:
```

Systems should also include:

```yaml
system_domain:
goal:
inputs: []
outputs: []
cadence:
failure_modes: []
related_projects: []
```

## Controlled Values

Use these values unless the user explicitly overrides them.

### `type`

`job`, `article`, `thought`, `event`, `tweet`, `reminder`, `opportunity`, `resource`, `decision`, `system`, `misc`

### `status`

`open`, `watching`, `done`, `closed`, `archived`

### `priority`

`low`, `medium`, `high`, `critical`

### `timeliness`

`timely`, `seasonal`, `evergreen`

### `verdict`

`go`, `test`, `skip`, `hold`, `unknown`

### `application_status`

`to_review`, `to_apply`, `applied`, `interviewing`, `offer`, `rejected`, `closed`, `archived`

### `date_confidence`

`exact`, `estimated`, `unknown`

### `deadline_type`

`explicit`, `inferred`, `rolling`, `unknown`

## Ingest Workflow

When the user provides a new export:

1. Read the source from `raw/` when available, otherwise from `imports/chat-exports/`.
2. Extract each actionable or referenceable item:
   - links
   - interesting thoughts or observations
   - explicit reminders
   - job opportunities
   - deadlines
   - event mentions
   - technical articles and general reading
   - docs, repos, tools, papers, videos, and other reusable resources
   - explicit or implicit decisions
   - recurring systems, heuristics, and operating principles
   - notable self-notes
3. Normalize each item into the vault taxonomy.
4. For URLs, fetch the live page when possible to extract:
   - page title
   - item type
   - posted date or published date
   - visible deadline
   - company and role for jobs
   - short summary
5. When live pages are blocked, dynamic, or low-signal, prefer adding a first-party supporting artifact into `raw/` instead of trusting the URL alone.
6. Supporting artifacts include saved web clips, pasted job descriptions, screenshots, PDFs, copied article text, transcripts, and profile captures.
7. Attach supporting artifacts back to canonical notes so retrieval does not depend on the external website remaining accessible.
8. X posts should first go through the lightweight `tools/x_content.py` adapter, which normalizes X/Twitter status URLs, uses local `xurl` when available, and falls back to Twitter oEmbed for cloud-safe post text, author, and date extraction.
9. When simple HTTP fetching is not enough, use browser automation agentically before giving up.
10. Preferred browser fallback tools are Playwright or Selenium, especially for dynamic social sites and client-rendered pages.
11. X posts remain a priority case for local browser fallback when `x_content` cannot recover enough context because plain fetches are often weak, blocked, or incomplete.
12. Never invent `posted_on` or `deadline`.
13. If the website does not expose a date, leave the field blank and set `date_confidence: unknown`.
14. Assign topics, tags, `why_saved`, and `timeliness` based on the likely reason the item is worth revisiting.
15. Create or update the canonical note for the item.
16. If the source contains a judgment, preference, or repeated policy, create or update a `decision` or `system` note rather than burying it inside another note.
17. Update relevant project pages when an item materially affects active work.
18. Create backlinks between source items, topics, decisions, systems, and projects when the connection is durable.
19. Refresh impacted dashboards, including relevance-oriented dashboards, not just deadline-oriented ones.
20. Append an entry to `log.md`.
21. Update `index.md` if a new durable page type or topic page was created.
22. If the ingest reveals a reusable question answer, brief, or synthesis, write it to `outputs/` and link it back into the wiki.
23. Maintain an artifact capture queue for notes whose live context is too weak for reliable retrieval.

## Cloud-First Development Rules

- Default to building against the AWS-backed canonical vault, not a purely local vault.
- New ingestion paths should assume the machine may be offline and should recover cleanly from queued cloud-side events.
- New interfaces should read from and write to canonical AWS state, either directly or through a sync layer that preserves cloud canon.
- Prefer designs that expose agent traces, citations, decisions, and retrieval context cleanly across Telegram and web surfaces.
- Keep browser-based enrichment optional and local when it is too expensive or brittle for cloud execution.
- When adding features, prioritize:
  - better canonical data quality
  - better retrieval and surfacing
  - better interoperability across interfaces
  - safer sync semantics between local tools and AWS state
  - explicit task lifecycle state, so when the user says something is done/applied/read/skipped/cancelled, the ledger treats that as authoritative

## Job-Specific Rules

- Jobs are primarily organized by `posted_on`, not `discovered_on`.
- If `posted_on` is missing, keep the job in the unknown-date bucket.
- If a visible application deadline exists, store it in `deadline`.
- If a deadline is not visible but the posting is clearly rolling, use `deadline_type: rolling` and leave `deadline` blank.
- `application_status` tracks the candidate pipeline. `status` tracks note lifecycle.

## Query Workflow

When answering questions:

1. Read `hot.md` first when it exists.
2. Start with the most relevant dashboard page.
3. Read the canonical item notes referenced there.
4. Read topic pages, decision notes, and project pages when synthesis, history, or intent-tracking is required.
5. Surface items by relevance, combining:
   - explicit deadlines and dates
   - recency of discovery or publication
   - overlap with the user's recurring interests and recent questions
   - manual priority and `why_saved`
   - prior verdicts and recurring systems
6. Cite canonical notes, not imports, whenever possible.
7. If the answer creates durable value, file it into `topics/`, `projects/`, or `outputs/`.
8. Use `memory/` and `outputs/dreams/` as reviewable personalization surfaces; do not hide durable user understanding in opaque app memory.

## Surfacing Rules

- Treat jobs, deadlines, and reminders as time-sensitive by default.
- Treat technical articles, thoughts, and resources as interest-sensitive by default.
- Treat decisions and systems as high-priority context when the user is building, choosing, or evaluating something.
- Use `timeliness` to distinguish items that should be surfaced because they are urgent versus items that are evergreen but topically relevant.
- Prefer surfacing items that match:
  - the current date window
  - upcoming deadlines or events
  - active job search status
  - the user's current topic of curiosity
  - active projects
  - prior decisions and verdicts
  - repeated mentions across exports
- Morning briefs must be agentic, not deterministic. Deterministic code may shortlist candidates and enforce cost/recency bounds, but the Codex morning brief agent should make the final call using the user's profile, decisions, reminders, current interests, jobs, recent saves, and active systems.
- Scheduled morning briefs should be concise and intentional: only urgent next actions, high-impact opportunities, explicit reminders, and at most one genuinely valuable recent reading.
- When the user asks about a topic, look for both direct matches and adjacent topics that often co-occur in saved items.
- When the user asks for advice or ideation, privilege pages that capture taste, preferences, prior inspirations, and explicit decisions.
- Refresh `hot.md`, the native Bases dashboard, and the health report after substantial ingest or enrichment changes.
- Treat `.vault/reports/claim-health.md`, `.vault/reports/contradictions.md`, `.vault/reports/low-confidence.md`, `.vault/reports/stale-claims.md`, and `.vault/reports/memory-palace.md` as first-pass diagnostic context when answer quality, freshness, or contradictions matter.
- Cache/index writers should use atomic temp-file replacement. Do not leave half-written `agent-digest.json`, `claims.jsonl`, or `search.sqlite` artifacts.
- Optional semantic search is controlled by `VAULT_EMBEDDINGS_ENABLED=true`; keep it off for cost-sensitive cloud paths unless deliberately enabled.
- Web, Telegram, and morning-brief runs should leave redacted trajectory events under `.vault/trajectories/` so agent behavior is inspectable later.
- Web and Telegram runs should also leave structured `.vault/events/agent-events.jsonl` records with `run_id`, `seq`, and `stream` so tool calls and lifecycle state are easy to inspect.
- The task ledger lives under `.vault/tasks/` and renders a human-facing view at `dashboards/tasks.md`.
- Telegram outbound delivery should use the durable queue path, not raw one-shot sends, for user-facing messages that should not be silently lost.

## Project and Output Workflow

- Create a project page when the user is actively building something that will likely accumulate repeated context.
- Project pages should link to relevant topics, decisions, inspirations, and outputs.
- Store reusable generated artifacts in `outputs/` instead of leaving them only in chat.
- Prefer markdown deliverables, but use other file formats when the query specifically benefits from them.
- File important outputs back into related topics or projects so later queries can build on them.

## Maintenance Workflow

Run periodic lint passes that check for:

- duplicate URLs or near-duplicate canonical notes
- jobs missing `application_status`
- open jobs with past deadlines
- items with `date_confidence: unknown`
- items missing `why_saved` when the source context clearly implies a reason
- thoughts and resources with weak tags or no topic links
- decisions missing verdicts or rationale
- project pages with stale links or no linked decisions
- orphan topic pages
- broken wiki links
- stale dashboard snapshots
- outputs that should be backlinked into the main wiki but are not

## Writing Style

- Keep notes concise and structured.
- Use markdown links and Obsidian wiki links.
- Prefer short factual summaries over long prose.
- Preserve ambiguity explicitly instead of guessing.
- Write notes so they are easy to retrieve by topic later, not just easy to ingest once.
- Put the most agent-useful summary first.
