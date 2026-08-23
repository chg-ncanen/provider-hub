# JiraExpressAI: Confluence sync for ticket artifacts

**Status:** Draft
**Date:** 2026-08-22
**Plugin:** `plugins/jira-express-ai` (`jexpress`)

## Problem

Every AI-Work ticket run produces up to five `.md` artifacts in the local,
ephemeral `tickets/<KEY>/` directory: `discovery.md`, `implementation-notes.md`,
`review-notes.md`, `merge-notes.md`, and `review-context.md`. Developers are
told to "look at the md files," but there's no good way to actually do that —
the files live on whatever machine runs the orchestrator, and the directory
is deleted 7 days after a ticket goes inactive (`ticket-orchestrator/SKILL.md`,
Purge step). Visibility into what the AI actually found/did/decided is
effectively zero unless someone knows to SSH in and read files before they're
gone.

This design syncs the four human-readable artifacts to Confluence, under the
existing folder
`https://chghealthcare.atlassian.net/wiki/spaces/PDE/folder/5148311567`, so
developers get a durable, browsable, per-ticket view — and, for the two
artifacts a specialist actually treats as revisable prior state, a lightweight
way to edit that state directly from Confluence instead of a raw file.

## Goals

- Every ticket gets a durable, human-readable record of its AI-Work run in
  Confluence, organized by ticket key, that outlives the local
  `tickets/<KEY>/` directory's 7-day purge.
- `discovery.md` and `implementation-notes.md` become editable from
  Confluence — a human's edit is read back in the next time that stage
  re-runs.
- No dependency on an MCP connector being configured in headless sessions —
  same constraint that already shapes how this plugin talks to Jira
  (`ticket-worker/SKILL.md:104-109`).
- Visibility sync is best-effort and never blocks real ticket progress (Jira
  transitions, specialist launches). The one exception is pulling back a
  human's Confluence edit before it would otherwise be silently discarded —
  see "Pull failures" below.
- Zero changes to any specialist `SKILL.md`. Specialists already only read
  and write local files; that boundary doesn't change.

## Non-goals

- Two-way sync for `review-notes.md` / `merge-notes.md`. Neither specialist
  ever reads its own prior version (both are unconditionally deleted before
  each re-run — `worker.py`'s `_run_review_pass`/`run_merge`), so pulling
  Confluence content back for them would be discarded unread.
- Syncing `review-context.md`. It's a machine-readable handoff record (PR
  URL, repo, branch) for `worker.py`/specialists to parse, not a document a
  human would want to read.
- General-purpose bidirectional wiki sync, conflict merging, or preserving
  arbitrary Confluence formatting. This is artifact mirroring with a narrow,
  well-defined edit surface, not a wiki-as-database.

## Architecture overview

```
worker.py
 ├─ reads discovery.md / implementation-notes.md / review-notes.md / merge-notes.md
 │   (exactly where it already reads them today, for Jira comment-building)
 ├─ calls confluence_sync.push(key, artifact_type, content)   → Confluence
 └─ (discovery + implementation only) calls
     confluence_sync.pull(key, artifact_type)                 ← Confluence
     immediately before launching that stage's specialist
```

`confluence_sync.py` is a new module alongside `worker.py`
(`plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py`), imported
directly — not a subprocess, not an MCP call. It talks to the Confluence REST
API via `requests`, reusing the same `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN`
already loaded into `.env` by `bootstrap-env.sh` for Jira.

Specialists are unaware this exists, in both directions. Where a specialist
already reads a prior artifact as context (`ticket-discovery`'s Step 0,
`ticket-implementation`'s NO_CHANGES_NEEDED redo), it just reads the local
`.md` file as always — `worker.py` has already refreshed that file's content
from Confluence before launching it, if applicable.

## Page structure

Under the existing folder (space `PDE`, parent id `5148311567`):

- One parent page per ticket, titled exactly `<KEY>` (e.g. `PDE-1234`). Body
  is just a link back to the Jira issue. Created the first time any artifact
  for that ticket is synced.
- Up to four child pages per ticket, created only once their stage is
  actually reached: `Discovery`, `Implementation Notes`, `Review Notes`,
  `Merge Notes`. Each mirrors its corresponding `.md` file, overwritten in
  place on every sync — not versioned per run (Confluence's own page history
  already retains prior versions).

Each child page body opens with a fixed banner identifying whether it's
editable, so an engineer knows what they can touch without needing to know
anything about how this system works:

- **Discovery / Implementation Notes:** *"✏️ Edits made on this page are read
  back into the ticket the next time this stage runs (e.g. after a QA
  rejection or a 'no changes needed' redo). Feel free to revise, correct, or
  add context here."*
- **Review Notes / Merge Notes:** *"📌 This page is a read-only mirror for
  reference — it's regenerated fresh each run and edits here are not read
  back into the pipeline."*

Content conversion: `.md` → Confluence storage format via the `markdown`
library (`fenced_code` extension). Plain HTML output from `markdown` is used
directly as the storage-format body — sufficient for the simple structure
(headers, lists, bold, code blocks, links) these templates produce.

## Idempotency — no new local state

No page-ID file is written into `tickets/<KEY>/`. That directory is
ephemeral; a Confluence link shouldn't depend on it surviving. Instead, every
push and pull does a CQL title search scoped to the parent folder
(`space = PDE and ancestor = 5148311567 and title = "<KEY>"` for the parent,
then a second search among its children for the artifact's title) to find the
existing page, updating it if found or creating it if not. This mirrors the
"derive state, don't cache it" pattern already used elsewhere in this plugin
(`worker.py`'s own stage self-correction).

Before every update, fetch the page's current `version.number` fresh (never
cache it) and `PUT` `version + 1`. This means a sync always writes on top of
the true latest version, which avoids most spurious version-conflict failures
from a human having edited the page since the last sync — only a genuinely
simultaneous edit would still race, and that falls into ordinary failure
handling below.

This idempotent lookup is also what makes rewinds work with zero special-case
code: a QA Review rejection re-runs discovery, which rewrites `discovery.md`,
which gets synced again — the title search finds the same existing `Discovery`
child page and updates it in place. No duplicate pages, no rewind-specific
branch.

## Trigger points (push)

`worker.py` already reads each artifact's full content at a fixed set of call
sites, to build the Jira comment for that stage. Confluence push is added at
every one of those sites, using the content already in hand — no new file
reads:

| Call site | Artifact | Comment function |
|---|---|---|
| `run_discovery` (worker.py:482), `run_qa_review_gate` resume (:632) | `discovery.md` | `build_qa_review_comment` |
| `_run_first_implementation_pass` (:529, :542), `run_in_review_gate` resume (:647) | `implementation-notes.md` | `build_in_review_comment` |
| `_run_review_pass` (:574) | `review-notes.md` | inline comment |
| `run_merge` (:604, :609) | `merge-notes.md` | inline comment |
| `apply_blocked_routing` (:438, shared by discovery/implementation/review) | whichever artifact blocked | inline comment |

**Important implementation gotcha:** `apply_blocked_routing` is the one
shared function that already covers BLOCKED handling for discovery,
implementation, and review — adding the sync call there covers those three
stages in one place. But `run_merge` has its **own independent** inline
handling for all three of its outcomes (`SUCCESS` at :604, `PENDING` at
:616-621, `BLOCKED` at :606-615) and never calls `apply_blocked_routing`. The
sync call must be added separately inside `run_merge`, covering all three
branches — including `SUCCESS`'s plain "Merge complete" comment, which today
has no artifact reference at all. If this only gets added via
`apply_blocked_routing`, merge silently never gets synced.

Sync fires whenever an artifact file is read, regardless of its internal
`Status` value (`BLOCKED`, `NO_CHANGES_NEEDED`, `PENDING`, normal) — the
Confluence page should reflect whatever's actually on disk, independent of
whether a Jira comment or transition accompanies it.

## Jira comment changes

Every comment built from an artifact gets a trailing pointer:

- Sync succeeded → `Full details: <confluence-url>`
- Sync failed (or Confluence isn't configured) → falls back to today's
  wording (`See discovery.md for full findings.`, etc.) — the `.md` file is
  still there and still authoritative, so the pointer stays useful.

This touches `build_qa_review_comment`, `build_in_review_comment` (both
gain a `confluence_url: str | None` parameter), and the inline comments in
`_run_review_pass`, `run_merge`, and `apply_blocked_routing`.

## Pull (Confluence → local file)

Applies only to `discovery.md` and `implementation-notes.md` — the two
artifacts a specialist actually re-reads as revisable prior state
(`ticket-discovery`'s Step 0; `ticket-implementation`'s NO_CHANGES_NEEDED
redo dispatch in `run_implementation`). `review-notes.md`/`merge-notes.md`
are unconditionally deleted before every re-run and never read back by their
specialist, so a pull for them would fetch content nothing ever reads.

Implementation is unconditional, not resume-specific: `run_discovery` and
`_run_first_implementation_pass` both attempt the pull at the very top, every
time they run — including a ticket's very first-ever discovery pass. On a
genuine first run, the title search finds no existing child page and it's a
no-op; the same code path handles both "first run" and "revision" without
needing to distinguish them.

Content conversion for the pull direction (storage format → markdown) is
lossier than the push direction, but this is safe: `worker.py`'s own
`extract_section`/`extract_status_text` parsing only ever runs against a file
*after* the specialist has freshly rewritten it in its own fixed template —
never against pre-edit pulled-back content. Specialists already treat their
prior artifact as unstructured prose input (the same way they already treat
Jira rejection comments), then regenerate the file from scratch each time
they write it. A reflowed heading from the round-trip conversion cannot break
anything downstream.

### Pull failures — blocking, unlike push

Push failures are harmless: the local `.md` file is unaffected either way, so
a failed push just delays Confluence visibility and falls back to the old
comment wording. A **pull** failure is different in kind: if it happens right
when `run_discovery`/`_run_first_implementation_pass` is about to re-run, and
a human had made an edit that exists only on that Confluence page, silently
falling back to "use the existing local file" would discard that edit with no
record anywhere.

This reuses the existing `report_failure()` mechanism (`worker.py:392-432`)
verbatim — no new mechanism:

```python
report_failure(key, f"could not fetch prior {artifact} content from Confluence: {error}",
                auth, stage="ticket-discovery")  # or "ticket-implementation"
```

- Comments `🤖 ⚠️ {key}: <reason> — will retry automatically.` and leaves the
  ticket at its current status — the specialist does not launch this cycle.
  The next orchestrator run resumes and retries the pull from scratch.
- `report_failure` counts consecutive `🤖 ⚠️` comments from most recent
  backward, stopping at the first comment that isn't one. In today's
  implementation that's "any other comment"; for this specific failure, that
  default is too loose — an unrelated human comment on the ticket shouldn't
  be read as permission to abandon the pull.
- Escalates to `Blocked` after 3 consecutive failures with no such break, same
  as any other transient failure in this worker.

### The "continue anyway" override — judgment lives in the skill layer, not the script

Retrying the pull itself is always safe (it either succeeds or fails again
cleanly). What requires actual judgment is a human explicitly saying "proceed
without my edit" — that's not something to pattern-match with a keyword
check inside `worker.py`, which is a deterministic script by design
(`ticket-worker/SKILL.md:46-59`). That same section already names this
exact category of decision — "judgment calls about ambiguous human
feedback" — as the intended reason `ticket-worker` stays a real `claude -p`
session wrapping `worker.py`, rather than a bare script invocation like
`orchestrator.py`.

So: `worker.py`'s pull-failure path always defaults to the
`report_failure`-driven retry/escalate behavior above — no override baked
into the script. The override is a step added to the `ticket-worker` skill
itself: before invoking `worker.py`, if the ticket is currently sitting on a
pull-failure retry (evidenced by a recent `🤖 ⚠️` Confluence-pull comment) and
the most recent human comment reads as authorizing continuing anyway, the
skill invokes `worker.py` with `--skip-confluence-pull`, telling the
deterministic script to proceed on the existing local file content for this
run instead of attempting the pull. This is the first piece of real judgment
logic added at that layer, exactly as `ticket-worker/SKILL.md` anticipates —
a small, additive step, not a re-architecture.

## Fresh restart (`To Do`)

When a ticket is moved back to `To Do`, the orchestrator wipes
`tickets/<KEY>/` entirely and launches a fresh (non-resumed) worker session.
`worker.py`'s own startup sequence already detects this exact moment (Step 3:
"If `To Do`: transition to `In Discovery` immediately, before anything
else" — `ticket-worker/SKILL.md:112`). At that point, before the discovery
specialist runs, `worker.py` attempts to **clear** (not delete) each of the
four possible child pages for `<KEY>`: title search for each, and if found,
overwrite its body with a short placeholder — *"Cleared — ticket restarted on
\<date\>. See this page's version history for the previous attempt."* If a
child page doesn't exist (that stage was never reached), the search finds
nothing and it's skipped, same as any other sync.

This keeps the logic entirely inside `worker.py` (already the owner of
artifact-domain knowledge) with no changes to `orchestrator.py`, which stays
a generic dispatcher that never learns about page names. Confluence's own
page version history preserves the prior attempt's content for anyone who
needs to look back — nothing is destroyed, only the currently-visible content
resets.

## Configuration

Two new plugin `userConfig` fields in `plugins/jira-express-ai/.claude-plugin/plugin.json`,
relayed into `.env` by the existing `bootstrap-env.sh` (same pattern as
`REPOS_DIR`):

- `CONFLUENCE_SPACE_KEY` (default `PDE`) — verified directly: this is the
  real space key for "Provider Digital Experience" (space id `929781`).
- `CONFLUENCE_PARENT_PAGE_ID` (default `5148311567`) — verified directly via
  CQL (`id=5148311567`): an existing Confluence **folder** (content type
  `folder`, not `page`), titled "JiraExpress AI Workstreams", already present
  under space `PDE`.

No new credentials — `confluence_sync.py` reuses `ATLASSIAN_EMAIL` /
`ATLASSIAN_API_TOKEN`, already required for Jira access. The Atlassian cloud
ID needed for API calls is `e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2` — already
hardcoded in `ticket-orchestrator/SKILL.md:308` for Jira, so `confluence_sync.py`
reuses the same constant rather than adding new config for it.

**Verify early:** the parent being a *folder* rather than a *page* is
untested against Confluence's v2 pages API `parentId` — folders are a newer
container content-type and should accept child pages the same way a page
does, but this should be smoke-tested as the first implementation step
before building the rest of `confluence_sync.py` against that assumption.

## Testing

- `confluence_sync.py` gets its own unit tests (mocked `requests`): create vs.
  update branching, fresh-version-fetch-before-update, banner selection by
  artifact type, push failure is swallowed and returns `None`, pull failure
  raises/signals distinctly from "not found" (so `worker.py` can route it to
  `report_failure` rather than treating it as a legitimate first-run no-op).
- `test_worker.py` gains assertions that:
  - sync is invoked at all five call sites above, including both branches of
    `run_merge` and `apply_blocked_routing`'s shared path;
  - a push failure doesn't affect the existing Jira-transition/comment
    behavior (comment still posts, with fallback wording);
  - a pull failure routes through `report_failure` and does not launch the
    specialist;
  - the fresh-`To Do` clear step fires exactly once, at the existing
    "To Do → In Discovery" transition point, and is a no-op when no child
    pages exist yet.

## Out of scope / follow-ups

- No change to `review-context.md` handling.
- No Confluence-side cleanup tied to the 7-day local archive purge — the
  Confluence record is meant to outlive the local ephemeral directory, so
  archival/purging remains local-only.
- The "continue anyway" judgment step in the `ticket-worker` skill is scoped
  narrowly to this one failure mode; broader judgment logic for other
  ambiguous-feedback cases mentioned in `ticket-worker/SKILL.md:54-56` is not
  part of this change.
