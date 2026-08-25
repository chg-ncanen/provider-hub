# JiraExpressAI: `live-site-bug-backend` playbook for PDE/ASA backend production incidents

**Status:** Draft
**Date:** 2026-08-24
**Plugin:** `plugins/jira-express-ai` (`jexpress`)

## Scope: backend only

The exclusion here is about **the kind of code**, not a specific repo name.
PDE has more than one frontend repo (`pde-ui`, other UI repos, the native
mobile app), and this playbook excludes **React/Vue/etc frontend-framework
bugs** — component logic, client-side stores, browser-side
request/interceptor code — wherever that code lives. It covers tickets whose
**fix** lives in a backend service repo PDE owns instead (e.g.
`pde-travel-service`, `pde-providers-service-next`/PSN,
`pde-experience-service`/PES, `pde-auth-service`/PAS,
`pde-assignment-details-service`, `pde-availability-service`) — regardless
of:

- the ticket being filed with a `PDE-UI:` prefix, or
- the reported symptom being a frontend behavior (a blank screen, a stuck
  loading state, a client-side error), or
- the ticket's own fix guidance asking for a frontend framework change
  alongside a backend one.

**This playbook may investigate a frontend-reported symptom all the way back
to a backend root cause and fix that backend cause — it must never modify
React/Vue/native-app framework code to do it**, even as a "quick
client-side mitigation" alongside the real fix. A frontend-framework defect
(or a frontend-only mitigation described in an otherwise-backend ticket) is
explicitly out of scope here and deferred to a **separate, future
live-site-bug design for frontend-framework code**, which will need its own
considerations this one doesn't cover (component/UI testing approach,
browser/device variability, LogRocket session-replay UX signals). See
"Mixed tickets" below for how this plays out when a ticket is filed under a
frontend prefix but has a backend root cause.

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

A review of a sample of production-incident-flavored PDE bugs (as opposed to
the separate cluster of security-audit-style findings also sitting in the
PDE bug backlog) found a wide quality spread, and — once scoped to
backend-owned fixes only — a much smaller eligible set than the full sample:

| Cluster | Roughly how much of the sample | In scope here? | Characteristic |
|---|---|---|---|
| Backend, root-caused | ~1/6 | Yes | Names the file/line or the exact fix, in a backend repo — reads like a near-finished `discovery.md` |
| Backend, investigation-only | ~1/9 | Yes | No hypothesis yet, but the *symptom* points at a backend service, not a frontend one |
| Mixed: frontend-filed, backend root cause | ~2/9 | Backend slice only | Ticket asks for a frontend mitigation *and* names a backend root cause — this playbook may pursue the backend half only; see "Mixed tickets" below |
| Frontend-framework | ~4/9 | No — future frontend design | The fix (or the only confirmable fix) is React/Vue/native-app framework code, not a backend service |
| Too vague to classify | small | No | Could be backend, could be a config/business-rule issue outside any git repo entirely |

One finding from that sample is worth naming as the reason this design leans
so hard on empirical verification rather than trusting the narrative:
**one ticket's own body contained a self-correction** — an earlier claim
made *in that same ticket* (about what was causing a recurring production
issue) was later refuted by direct benchmarking, and the ticket said so
explicitly. That's proof, from this project's own backlog, that a
confident-sounding incident narrative — even one already validated once —
is not necessarily correct. Anything built for this category has to treat
that as the normal case to design for, not an edge case.

## Goals

- Give `ticket-discovery` and `ticket-implementation` standing guidance for
  recognizing and correctly handling a **backend** live-site-bug ticket, via
  the existing playbook mechanism (`playbooks/INDEX.md` + a new
  `playbooks/live-site-bug-backend.md`), the same slot `dependency-bump.md`
  already occupies.
- Never let this playbook produce a React/Vue/native-app frontend-framework
  code change (in `pde-ui`, another UI repo, or the native app), even when
  the fastest-looking fix is a frontend one and the ticket itself asks for
  it — that's the frontend design's job, not this one's.
- Treat every claim in the ticket, its comments, and the "Common PDE ASA
  Alerts" Confluence runbook as a **hypothesis**, never a fact — regardless of
  how confident or detailed it reads, or whether it was already validated
  once before.
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
- No fix, mitigation, or PR touching React/Vue/native-app frontend-framework
  code under this playbook, full stop — see "Scope: backend only" above and
  "Mixed tickets" below.

## Playbook trigger

Added to `playbooks/INDEX.md`:

> The ticket describes a production defect, in a **backend** service PDE
> owns, surfaced by real user or system impact — an error, crash, timeout, or
> broken flow observed live, often citing LogRocket session/issue-group
> links, Grafana panels or incident dates, specific HTTP statuses, or
> user-reported symptoms — as opposed to a proactively-found
> code-quality/security-audit finding or a new feature ask. The ticket being
> assigned to the ASA team (however that's represented in Jira — a team
> field, an assignee group, or similar) is a mild hint worth factoring in —
> ASA also picks up dependency-bump and other maintenance tickets, so it
> does not by itself distinguish this category from any other. Content is
> still what actually decides the match, the same way every other playbook
> in this index works; ASA assignment just nudges you to look harder at
> whether the content matches, not a substitute for checking. Applies even
> when the ticket is filed as a `PDE-UI:` ticket or reports a frontend
> symptom, as long as the fixable root cause is backend — see
> `live-site-bug-backend.md`'s "Mixed tickets" section for how to handle that
> case. A separate, not-yet-written playbook will cover tickets whose fix is
> genuinely frontend-framework code (`pde-ui`, another UI repo, or the native
> app).

Matched by content, same as every other entry in the index — not a label,
not a project, not ASA assignment on its own (see above; ASA is too broad a
signal by itself, since it also covers dependency-bump and other
maintenance work).

## Discovery guidance (`## Discovery guidance` in `live-site-bug-backend.md`)

### Step: triage into one of four buckets

Before researching further, classify the ticket:

1. **Backend, root-caused** — the ticket already names a file/line and a fix,
   in a backend repo.
2. **Backend, investigation-only** — no hypothesis yet, but the symptom
   points at a backend service; discovery's job is to try to form one.
3. **Mixed** — the ticket (however it's filed, including `PDE-UI:` tickets)
   describes or implies a frontend mitigation *and* a backend root cause. See
   "Mixed tickets" below — only the backend slice is ever in scope here.
4. **Frontend-framework** — the only fixable/confirmable cause is
   React/Vue/native-app framework code, with no backend slice at all. Out of
   scope for this playbook entirely (see below).

This isn't a formality — it determines what "Proposed Approach" is even
allowed to claim (see below).

### Mixed tickets: pursue the backend slice, never touch frontend-framework code

Some tickets are filed under a frontend prefix (e.g. `PDE-UI:`) and ask for a
frontend mitigation (a retry, a scoped error boundary, a null guard)
alongside naming a backend root cause. For these:

- Investigate the full call chain freely, including into a frontend repo if
  needed to understand the symptom — reading frontend code to understand a
  symptom is not the same as fixing it there.
- If a backend root cause is found and can be confirmed (per the reproduce-
  before-fix discipline below), pursue and fix **only that** — in the
  backend repo, as its own single-repo PR. Do not also implement the
  frontend mitigation the ticket describes, even though it's part of the
  same ticket and even though skipping it means the ticket isn't "fully"
  resolved by this PR.
- Explicitly document, in `discovery.md`'s Notes and the eventual PR
  description, that the ticket also calls for a frontend mitigation that is
  out of scope for this pipeline today and needs separate frontend work.
  Don't silently drop it — say where it went, the same way `dependency-bump.md`
  carries forward an out-of-range major bump as a named note rather than
  dropping it.
- If investigation concludes the *only* real, confirmable fix is
  frontend-framework code (no backend change actually addresses the reported
  symptom), that is a **`BLOCKED`** outcome for this playbook — not
  `NO_CHANGES_NEEDED` (a change genuinely is needed, just not one this
  pipeline can make). `Suggested Next Step` should say plainly that this
  needs a frontend-framework fix (`pde-ui`, another UI repo, or the native
  app), to be picked up once the frontend playbook exists (or by a human
  directly in the meantime).

### Hypothesis discipline

Whatever discovery concludes — from the ticket text, the code, comments, or
(if available) the ASA common-alerts Confluence page — gets written into
`discovery.md`'s `Current Behavior`/`Proposed Approach` sections explicitly
labeled as a hypothesis (e.g. "Hypothesis (unconfirmed): ..."), never as a
settled fact. This applies uniformly across all four buckets and explicitly
includes the Confluence runbook: if a ticket's symptom matches one of that
page's sections, that page is another input to the hypothesis, not
corroboration that makes it true — the runbook itself can be stale, written
from an earlier incident that may have had a different real cause.
`ticket-discovery` cannot verify anything by running code (it stays
read-only) — verification is `ticket-implementation`'s job, below.

### Investigation may cross repo boundaries freely — including into frontend repos

Tracing a call chain across services (e.g. confirming PES proxies
`/providers/*` to PSN, so a PDE-UI-reported 500 actually originates there) is
normal and expected for this category, using the same clone/read access
`ticket-discovery` already has across `$REPOS_DIR` — this includes reading
frontend repos (`pde-ui`, other UI repos, the native app) to understand a
symptom, per "Mixed tickets" above. Reading a repo to understand a symptom is
never the same thing as fixing it there, and is not the same thing as the
eventual fix needing multiple PRs — see "Single-repo-PR constraint" below,
which is about the *fix*, not the *investigation*.

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

Tickets in this category sometimes link directly to real production records
(a Salesforce provider/assignment record, a specific Okta account). Don't
copy real names, Okta IDs, emails, or Salesforce record IDs into
`discovery.md`, the PR body, or the Confluence mirror — refer to "the
affected provider" and link back to the Jira ticket instead, which already
has appropriate access controls for that data.

### Single-repo-PR constraint

If, after investigation, the actual **fix** (not the investigation) would
require opening a PR in more than one repo, that's a `BLOCKED` outcome: the
existing pipeline architecture (`ticket-review`/`ticket-merge`) only handles
a single PR per ticket. `Suggested Next Step` should tell the developer to
split the ticket into one per repo — this org already has a working
precedent for exactly that, in how a multi-repo dependency-bump ticket gets
split into per-repo clones; point the developer at that same convention
rather than inventing a new one. Investigating across repos to find this out
is expected and is not itself a reason to block (see above) — only the
fix's repo-count is.

### Set `**Playbook:** live-site-bug-backend`

Same convention as `dependency-bump.md`, so implementation knows to load this
file's Implementation guidance without re-deriving the category.

## Implementation guidance (`## Implementation guidance` in `live-site-bug-backend.md`)

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
  cause, proceed with it and document the correction explicitly — a
  live-site ticket's stated cause turning out wrong on closer inspection is
  a normal outcome for this category, not a failure to be smoothed over. If
  it still can't reach a reproducing test or other clear certainty,
  `BLOCKED` — state what was tried and what a developer needs to do next
  (e.g. pull more LogRocket sessions, needs infra access this pipeline
  doesn't have).

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

This change is entirely new prose in `playbooks/live-site-bug-backend.md` plus small
edits to `playbooks/INDEX.md`, `ticket-worker/SKILL.md`, and
`JIRA_EXPRESS_AI_TRUST_CONTRACT.md` — none of it is exercised by
`test_worker.py`/`test_orchestrator.py` today, the same way `dependency-bump.md`
isn't. No automated test changes are needed for this change itself. (This is
also, not coincidentally, exactly why the reproduction-test requirement this
design adds matters so much for the tickets it governs — there is no other
mechanism verifying an AI-diagnosed live-site fix is actually correct.)

## Out of scope / follow-ups

- **A separate `live-site-bug` design for frontend-framework tickets**,
  covering both the tickets that are entirely frontend-owned and the
  frontend-mitigation slice of Mixed tickets. That design needs its own
  considerations this one doesn't cover: component/UI test approach for the
  reproduce-before-fix step, browser/device variability, and how to use
  LogRocket session-replay data specifically (as opposed to Grafana metrics,
  which are backend-flavored).
- Provisioning actual Grafana/LogRocket API tokens and building REST clients
  for headless, non-interactive access (mirroring how `ATLASSIAN_EMAIL`/
  `ATLASSIAN_API_TOKEN` work today) — if the assumed MCP tools turn out not
  to be reliably present in specialist sessions, this is the natural
  fast-follow.
- A dedicated Jira status/label for "investigated, no root cause found yet"
  outcomes — this design reuses the existing `BLOCKED` path rather than
  introducing one.
- Any change to the security-audit-style bug cluster.
