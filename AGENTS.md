# AGENTS.md

This vault is an Obsidian-first markdown wiki for personal knowledge capture, opportunity tracking, decision tracking, and retrieval. The agent owns the structure, bookkeeping, surfacing, filing, and maintenance. The user owns source curation, priority, and intent.

## Operating Philosophy

- Build for agent retrieval first. A strong file layout, stable summaries, backlinks, and clear indexes beat opaque app memory.
- Keep all important knowledge explicit and inspectable in markdown and local assets.
- Prefer universal files over provider-specific memory systems.
- Treat the compiled wiki as a long-term artifact that multiple AI tools can operate on.
- Store decisions, verdicts, and systems alongside knowledge so future work starts from prior reasoning instead of from scratch.

## Directory Contract

- `raw/`: canonical immutable source corpus
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
- `templates/`: note templates used during ingest and maintenance
- `index.md`: catalog of major pages
- `hot.md`: compact cross-session cache of recent context
- `log.md`: append-only operational log
- `raw/.manifest.json`: ingest manifest for source hashes and output summaries

## Source Handling

- Treat everything inside `raw/` and `imports/` as immutable.
- Never rewrite or delete a user-provided export.
- Record source provenance in canonical notes via `source_export` and `source_excerpt`.
- Prefer storing source artifacts in `raw/`.
- Use the source file as the durable raw artifact even after the compiled wiki is updated.
- Treat `imports/whatsapp-inbox/` as the staging area for self-group WhatsApp exports before they are copied into `raw/chat-exports/`.
- Treat `imports/telegram-inbox/` as the live normalized stream for bot-captured Telegram messages.
- Treat `raw/telegram-updates/` as the immutable append-only raw source for Telegram bot updates.

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
8. When simple HTTP fetching is not enough, use browser automation agentically before giving up.
9. Preferred browser fallback tools are Playwright or Selenium, especially for dynamic social sites and client-rendered pages.
10. X posts are a priority case for browser fallback because plain fetches are often weak, blocked, or incomplete.
11. Never invent `posted_on` or `deadline`.
12. If the website does not expose a date, leave the field blank and set `date_confidence: unknown`.
13. Assign topics, tags, `why_saved`, and `timeliness` based on the likely reason the item is worth revisiting.
14. Create or update the canonical note for the item.
15. If the source contains a judgment, preference, or repeated policy, create or update a `decision` or `system` note rather than burying it inside another note.
16. Update relevant project pages when an item materially affects active work.
17. Create backlinks between source items, topics, decisions, systems, and projects when the connection is durable.
18. Refresh impacted dashboards, including relevance-oriented dashboards, not just deadline-oriented ones.
19. Append an entry to `log.md`.
20. Update `index.md` if a new durable page type or topic page was created.
21. If the ingest reveals a reusable question answer, brief, or synthesis, write it to `outputs/` and link it back into the wiki.
22. Maintain an artifact capture queue for notes whose live context is too weak for reliable retrieval.

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
- When the user asks about a topic, look for both direct matches and adjacent topics that often co-occur in saved items.
- When the user asks for advice or ideation, privilege pages that capture taste, preferences, prior inspirations, and explicit decisions.
- Refresh `hot.md`, the native Bases dashboard, and the health report after substantial ingest or enrichment changes.

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
