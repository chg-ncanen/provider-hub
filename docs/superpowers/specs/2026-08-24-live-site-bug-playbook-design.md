# JiraExpressAI: `live-site-bug` playbook for PDE/ASA production incidents

**Status:** Draft
**Date:** 2026-08-24
**Plugin:** `plugins/jira-express-ai` (`jexpress`)

## Problem

Every ticket `ticket-discovery`/`ticket-implementation` have handled so far has
been a `dependency-bump`: a routine version move where the ticket's own claim
("package X has CVE Y") is independently, mechanically verifiable against a
registry or an audit tool. PDE's ASA (App Support Ambassador — PDE's name for
on-call) rotation also files a steady stream of production-bug tickets that
look nothing like that: they're narrative accounts of live incidents, usually
written by a human or a prior AI pass reading LogRocket sessions and Grafana
panels, proposing a root cause and a fix. Handing these to `ticket-discovery`/
`ticket-implementation` today, with no category-specific guidance, risks two
failure modes neither dependency-bump ever faces: trusting an incident
narrative that's wrong, and shipping a fix for a cause that was never actually
confirmed.

A review of the 18 candidate tickets (production-incident-flavored PDE bugs,
as opposed to the separate cluster of security-audit-style findings also
sitting in the PDE bug backlog) found a wide quality spread:

| Cluster | Count | Example | Characteristic |
|---|---|---|---|
| Root-caused | 6 | PDE-17101, 17102, 18224, 18126, 18181, 18130 | Names the file/line, proposes a concrete fix — reads like a near-finished `discovery.md` |
| Partial / cross-team | 4 | PDE-18112, 18113, 18114, 18115 | Some slice fixable in an owned repo; real root cause lives in a service PDE doesn't own |
| Investigation-only | 3 | PDE-18116, 17833, 17813 | No hypothesis yet — "N sessions fired event X, go find out why" |
| Too vague | 4 | PDE-17692, 16601, 15392, 17953 | A sentence and/or a bare LogRocket link, nothing else |
| Needs code search | 1 | PDE-14951 | Well-specified behavior, no file pointer, and links directly to real Salesforce provider records |

The full per-ticket table is in the Appendix.

The most important individual finding: **PDE-18130's own ticket body contains
a self-correction** — an earlier claim in that same ticket (that a specific
allocation pattern caused a recurring prod OOM) was later refuted by direct
benchmarking, and the ticket says so explicitly. This is proof, in this
plugin's own backlog, that a confident-sounding incident narrative — even one
already validated once — is not necessarily correct. Anything built for this
category has to treat that as the normal case to design for, not an edge
case.

## Goals

- Give `ticket-discovery` and `ticket-implementation` standing guidance for
  recognizing and correctly handling a live-site-bug ticket, via the existing
  playbook mechanism (`playbooks/INDEX.md` + a new `playbooks/live-site-bug.md`),
  the same slot `dependency-bump.md` already occupies.
- Treat every claim in the ticket, its comments, and the "Common PDE ASA
  Alerts" Confluence runbook as a **hypothesis**, never a fact — regardless of
  how confident or detailed it reads, or whether it was already validated
  once before (PDE-18130 is the standing proof this matters).
- Make reproduction the actual verification mechanism: before implementing a
  fix, write a test that fails for the hypothesized reason against current
  code. A confirmed reproduction is what "verified" means for this category —
  not "the ticket sounded credible."
- Bias hard toward `BLOCKED` over shipping a low-confidence fix: no
  reproduction and no other clear, stated certainty means stop, not guess.
- Best-effort use of Grafana/LogRocket where available in the specialist's
  session, with an explicit, non-blocking fallback note in `discovery.md`
  when they aren't.
- Keep the change scoped to the playbook layer — no changes to
  `ticket-discovery`/`ticket-implementation`'s base template structure, no new
  Jira status, no changes to how QA Review/merge gates work.

## Non-goals

- No new credential plumbing for Grafana/LogRocket (no new `userConfig`
  fields, no new REST client module). If those tools aren't present in a
  given specialist session, discovery says so and moves on — this is a
  documented capability gap, not something this change builds infrastructure
  to close. (See "Capability ceiling" below for why this is a real
  discussion, not a formality.)
- No changes to how the security-audit-style bug cluster (IDOR, XSS, CORS,
  open redirect, etc.) is handled — those don't match this playbook's
  trigger and are out of scope for this design entirely.
- No automated tooling to keep `playbooks/INDEX.md` and its files consistent
  — same as `dependency-bump.md` today, this is prose read by an LLM, not
  code exercised by `test_worker.py`.
- No change to the single-PR-per-ticket architecture itself (`ticket-review`/
  `ticket-merge` still only handle one PR). This design works within that
  constraint rather than lifting it — see "Single-repo-PR constraint" below.

## Playbook trigger

Added to `playbooks/INDEX.md`:

> The ticket describes a production defect surfaced by real user or system
> impact — an error, crash, timeout, or broken flow observed live, often
> citing LogRocket session/issue-group links, Grafana panels or incident
> dates, specific HTTP statuses, or user-reported symptoms — as opposed to a
> proactively-found code-quality/security-audit finding or a new feature ask.

Matched by content, same as every other entry in the index — not a label,
not a project, not the PDE-ASA team name itself (ASA is how these tickets
tend to originate, not something written into the ticket).

## Discovery guidance (`## Discovery guidance` in `live-site-bug.md`)

### Step: triage into one of three buckets

Before researching further, classify the ticket:

1. **Root-caused** — the ticket already names a file/line and a fix.
2. **Partial / cross-team** — some slice is fixable in a repo PDE owns, but
   the underlying root cause lives in a service PDE doesn't own (a gateway,
   PSN, PAS, etc.).
3. **Investigation-only** — no hypothesis yet; discovery's job is to try to
   form one.

This isn't a formality — it determines what "Proposed Approach" is even
allowed to claim (see below).

### Hypothesis discipline

Whatever discovery concludes — from the ticket text, the code, comments, or
(if available) the ASA common-alerts Confluence page — gets written into
`discovery.md`'s `Current Behavior`/`Proposed Approach` sections explicitly
labeled as a hypothesis (e.g. "Hypothesis (unconfirmed): ..."), never as a
settled fact. This applies uniformly across all three buckets and explicitly
includes the Confluence runbook: if a ticket's symptom matches one of that
page's sections, that page is another input to the hypothesis, not
corroboration that makes it true — the runbook itself can be stale, written
from an earlier incident that may have had a different real cause.
`ticket-discovery` cannot verify anything by running code (it stays
read-only) — verification is `ticket-implementation`'s job, below.

### Investigation may cross repo boundaries freely

Tracing a call chain across services (e.g. confirming PES proxies
`/providers/*` to PSN, so a PDE-UI-reported 500 actually originates there) is
normal and expected for this category, using the same clone/read access
`ticket-discovery` already has across `$REPOS_DIR`. This is not the same
thing as the fix needing multiple PRs — see "Single-repo-PR constraint"
below, which is about the *fix*, not the *investigation*.

### Grafana/LogRocket: best effort, not a dependency

If Grafana or LogRocket tools are available in this session, use them to
sanity-check time-sensitive claims in the ticket (is this still happening?
what's the current frequency? has it already been mitigated by an unrelated
change?) the same skeptical way as any other input. If they are not
available, say so plainly in `discovery.md` (e.g. "Grafana/LogRocket tooling
was not available in this session — the ticket's session/incident data below
is taken at face value from the ticket text and could not be independently
checked.") and continue — this must never block or degrade the rest of
discovery.

### PII handling

Some tickets in this category link directly to real production records (e.g.
PDE-14951 links to a real Salesforce provider/assignment record). Don't copy
real names, Okta IDs, emails, or Salesforce record IDs into `discovery.md`,
the PR body, or the Confluence mirror — refer to "the affected provider" and
link back to the Jira ticket instead, which already has appropriate access
controls for that data.

### Single-repo-PR constraint

If, after investigation, the actual **fix** (not the investigation) would
require opening a PR in more than one repo, that's a `BLOCKED` outcome: the
existing pipeline architecture (`ticket-review`/`ticket-merge`) only handles
a single PR per ticket. `Suggested Next Step` should tell the developer to
split the ticket into one per repo — this backlog already has a working
precedent for exactly that: `PDE-17509`'s dependency-bump was split into
per-repo clones (`PDE-18238`, `PDE-18239`, `PDE-18246`, etc.). Investigating
across repos to find this out is expected and is not itself a reason to
block (see above) — only the fix's repo-count is.

### Set `**Playbook:** live-site-bug`

Same convention as `dependency-bump.md`, so implementation knows to load this
file's Implementation guidance without re-deriving the category.

## Implementation guidance (`## Implementation guidance` in `live-site-bug.md`)

### Reproduce before you fix

Before writing any fix code, write a test that should fail, for the
hypothesized reason, against current code — mocking the failing dependency
where the cause is external (e.g. force a wrapped client call to reject
repeatedly to exercise a give-up branch, rather than needing a real outage).
This is the `superpowers:test-driven-development` red/green cycle; apply it
rather than restating it here. Then branch on the outcome:

- **Reproduces as hypothesized** → proceed with the normal TDD red→green
  cycle. The passing test doubles as regression coverage. State in
  `implementation-notes.md` that the hypothesis was empirically confirmed,
  not just plausible.
- **A test genuinely isn't feasible** (a pure infra/config value with no code
  representation in the owned repo) → say so explicitly in
  `implementation-notes.md`, and only still proceed if there is other clear,
  well-supported certainty — state exactly what that evidence is (e.g. a
  Grafana-confirmed incident timeline plus an unambiguous one-line config
  change). Otherwise, `BLOCKED`.
- **A test is feasible but doesn't reproduce the hypothesized failure** →
  implementation has tools discovery didn't (it can run code) — take one
  further investigation pass. If that finds and confirms a *different* real
  cause, proceed with it and document the correction explicitly (the same
  way PDE-18130's own ticket body models a correction). If it still can't
  reach a reproducing test or other clear certainty, `BLOCKED` — state what
  was tried and what a developer needs to do next (e.g. pull more LogRocket
  sessions, needs infra access this pipeline doesn't have).

The bias is: no reproduction and no other stated certainty means stop, not
guess. This is a stricter bar than `dependency-bump.md`'s "verify a major
bump with the full test suite" — there, the ticket's core claim (a CVE
exists) is independently mechanically true; here, the ticket's core claim
(this is why it's broken) is exactly what's in question.

### Single-repo-PR constraint (implementation side)

Same rule as discovery: if mid-implementation it becomes clear the fix
needs a second repo (e.g. a shared contract change), stop and go `BLOCKED`
with the same split-into-per-repo-tickets guidance — do not open a second PR.
This explicitly overrides `ticket-implementation/SKILL.md` Step 5's default
multi-repo handling ("write `review-context.md` for the primary repo, list
the others in Notes"), which is fine for an incidental cross-repo edit on an
ordinary ticket but wrong for this category, where crossing repos is common
and expected.

## Capability ceiling — a documentation update, not new infrastructure

`ticket-worker/SKILL.md`'s "Sandbox — hard limits" section and
`JIRA_EXPRESS_AI_TRUST_CONTRACT.md`'s "fixed capability ceiling" section both
currently state, as a security invariant (containing the blast radius of a
prompt-injection attempt in ticket/comment/file content), that no specialist
session has credentials or connections to anything but Jira, Confluence, and
`git`/`gh`. That statement needs a narrow, explicit carve-out for this
category: **read-only** Grafana/LogRocket query tools, if present in the
session, may be used for research — never any write/mutating action (e.g.
`pde-mcp`'s `acknowledge_alert`/`close_alert`/`assign_alert` remain
off-limits regardless of category), and never any other business system
(Salesforce, email, Slack, etc., which both docs already rule out and this
change doesn't touch). This is a documentation change to both files, not new
code — same as how this plugin already documents Jira/Confluence access,
just narrower and read-only.

## Testing

This change is entirely new prose in `playbooks/live-site-bug.md` plus small
edits to `playbooks/INDEX.md`, `ticket-worker/SKILL.md`, and
`JIRA_EXPRESS_AI_TRUST_CONTRACT.md` — none of it is exercised by
`test_worker.py`/`test_orchestrator.py` today, the same way `dependency-bump.md`
isn't. No automated test changes are needed for this change itself. (This is
also, not coincidentally, exactly why the reproduction-test requirement this
design adds matters so much for the tickets it governs — there is no other
mechanism verifying an AI-diagnosed live-site fix is actually correct.)

## Out of scope / follow-ups

- Provisioning actual Grafana/LogRocket API tokens and building REST clients
  for headless, non-interactive access (mirroring how `ATLASSIAN_EMAIL`/
  `ATLASSIAN_API_TOKEN` work today) — if the assumed MCP tools turn out not
  to be reliably present in specialist sessions, this is the natural
  fast-follow.
- A dedicated Jira status/label for "investigated, no root cause found yet"
  outcomes — this design reuses the existing `BLOCKED` path rather than
  introducing one.
- Any change to the security-audit-style bug cluster.

## Appendix: ticket quality ratings (18 tickets reviewed)

Rating scale: 5 = ready to hand an AI almost as-is, 1 = not enough to start.

| Key | Rating | Category | Why |
|---|---|---|---|
| PDE-17101 | 5 | Directly fixable | Exact file/line, before/after code diff, LogRocket session links, impact numbers |
| PDE-17102 | 5 | Directly fixable | Same caliber — exact file/line, code snippet, explicit regression-risk checklist |
| PDE-18224 | 5 | Directly fixable | Full root cause with file/lines, repro steps, human-confirmed fix in prod via comments |
| PDE-18126 | 5 | Directly fixable | File, root cause, Grafana-confirmed incident dates already cited, one-line fix |
| PDE-18181 | 5 | Directly fixable | CI flake; timestamped evidence, ranked fix options, explicit confirm-fix steps |
| PDE-18130 | 4 | Directly fixable (mostly done) | Contains a self-correction refuting an earlier claim in the same ticket — the anchor example for this whole design |
| PDE-18112 | 4 | Partial / cross-team | Ticket's own analysis argues this isn't even a PDE-UI bug; client mitigation is fixable, root cause isn't |
| PDE-18113 | 4 | Partial / cross-team | `err.response?.status` guard is fully specified and testable; 503 upstream cause is backend |
| PDE-14951 | 3 | Needs code search | Clean acceptance criteria, no file pointer; links to real Salesforce provider records (PII flag) |
| PDE-17813 | 3 | Investigation-only | LogRocket exception-group ID given, root cause unknown, needs live session data |
| PDE-18114 | 3 | Partial / cross-team | Root cause is gateway/infra-level (444s), outside any owned repo |
| PDE-18115 | 3 | Partial / cross-team | UI retry fix concrete; backend-latency investigation needs Grafana access |
| PDE-17953 | 2 | Thin | No file, no root cause, one screenshot; already "In Review" with no comments |
| PDE-17833 | 2 | Investigation-only | Fix may span multiple repos with no idea which; needs live tracing to locate the slow service |
| PDE-18116 | 2 | Investigation-only | "N sessions fired this event, go investigate" — no hypothesis at all |
| PDE-15392 | 1 | Too vague | One sentence + a bare LogRocket link |
| PDE-16601 | 1 | Too vague | Title + a bare LogRocket link, nothing else |
| PDE-17692 | 1 | Too vague | "Precise conditions unknown," pasted user quotes, no file, no link, no environment |
