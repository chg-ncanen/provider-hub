---
name: dependabot-triage
description: Triage GitHub Dependabot alerts for the current repo by tracing whether each flagged package is actually reachable at runtime, rather than trusting severity alone, then dismiss confirmed non-actionable alerts on GitHub and file one Jira ticket per repo with the full triage report. Use when the user asks whether a Dependabot/CVE alert is valid or exploitable in this codebase, wants to triage or prioritize a batch of open alerts, asks "is this actually a problem here," wants a punch list before deciding which alerts to fix vs dismiss, wants dev-only/unreachable alerts dismissed, or wants tickets filed for the results of a triage.
user-invocable: true
---

# Dependabot alert triage

Severity labels from GitHub/npm advisories describe the vulnerability in the abstract. Whether
it matters *here* depends on three questions this skill answers for each package:

1. Is it a direct dependency or transitive?
2. Does the code that pulls it in actually run in production, or is it dev tooling /
   install-time only?
3. Is the specific vulnerable function/code path (not just the package) ever reached?

Don't skip straight to "upgrade everything" — a huge fraction of alerts in a typical repo turn
out to be eslint/jest/commitlint transitive deps that never ship, or install-time tools like
node-gyp/node-pre-gyp that never run while serving requests.

## Configuration — change these for your org

This skill and its companion scripts (`list_target_repos.py`, `run_batch.py`) ship configured
for one team's GitHub org and Jira project. Adapting this for a different org/team/project
means changing only these:

| What | Current value | Where it lives |
|---|---|---|
| GitHub org | `chghealthcare` | default `org` argument in `list_target_repos.py` / `run_batch.py` |
| CODEOWNERS team | `@chghealthcare/pde` | default `team` argument in `list_target_repos.py` |
| Jira site | `chghealthcare.atlassian.net` | the auth check just below, and step 8 |
| Jira project / epic / team field / priority | `PDE` / `PDE-17837` / `customfield_13613` / `P1-High` | step 8 (exact field IDs and payload shape are there, not repeated here) |

Everything else — the reachability-tracing methodology (steps 1-6), the dismissal eligibility
rules (step 7) — isn't org-specific and works unmodified for any Node.js repo on GitHub + Jira.
If you're pointed at a Jira project other than the one in the table, step 8 already knows to
resolve the equivalent fields via `getJiraIssueTypeMetaWithFields` and ask once, rather than
assuming these exact values apply everywhere.

## Authenticate once, up front

A full run touches both GitHub (steps 1, 7) and Jira (step 8). Check both before doing any
triage work, rather than discovering a missing auth mid-batch:

```bash
gh auth status
```

If that fails, tell the user and stop — don't retry individual `gh api` calls hoping auth
appears, and don't attempt `gh auth login` yourself (it's an interactive browser flow that
needs the user's own action).

For Jira, call `mcp__atlassian__getAccessibleAtlassianResources` (or `atlassianUserInfo`) once.
If it errors, or doesn't return `chghealthcare.atlassian.net` as an accessible resource, stop
and tell the user directly — this is the one point in the run where surfacing a blocker is
correct, so it doesn't happen as a surprise three repos into a batch instead.

Once both are confirmed, proceed through the rest of this skill (steps 0-9) without pausing to
ask permission again for the individual `gh`/npm/Jira calls that follow — see "Operating mode"
at the bottom of this file. This check itself needs no confirmation; it's read-only.

## 0. Scope the work

If the user didn't specify which alert(s), ask (via AskUserQuestion) whether they want:
- one specific alert number,
- the single most severe alert, or
- a full triage of all open critical/high alerts.

Don't silently assume "all of them" for a large repo — pulling and triaging 40+ alerts is a lot
of tool calls; confirm scope first unless the user already gave you a number.

## 1. Pull the alerts

Get the repo slug if not obvious from context:

```bash
gh repo view --json nameWithOwner -q .nameWithOwner
```

List open alerts, filtered to the scope decided in step 0:

```bash
gh api repos/{owner}/{repo}/dependabot/alerts --paginate \
  -q '.[] | select(.state=="open") | select(.security_advisory.severity=="critical" or .security_advisory.severity=="high") | "\(.number)\t\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.dependency.manifest_path)\t\(.security_advisory.summary)"'
```

Adjust the severity filter to match scope. Expect many alerts to collapse onto a handful of
unique package names (one package can have many CVE entries) — dedupe by package before doing
per-package investigation.

## 2. Classify direct vs transitive

Check the manifest for direct deps (`package.json` dependencies/devDependencies for Node;
adapt to `requirements.txt`/`Pipfile`, `Gemfile`, `go.mod`, etc. for other ecosystems).

For each unique flagged package that isn't direct, trace the dependency path:

```bash
npm ls <package> --all
```

This shows every top-level dependency that pulls it in — that's the thing to investigate next,
not the leaf package itself.

## 3. Determine if the consumer actually runs in production

For each top-level consumer identified in step 2, decide which bucket it falls into:

- **Dev-only tooling** — eslint, jest, commitlint, husky, lint-staged, sequelize-cli (or
  equivalent CLI/test/lint tooling in other ecosystems). These run in CI/git-hooks/local dev,
  never in the deployed service. Low priority regardless of CVE severity.
- **Install/build-time only** — node-gyp, node-pre-gyp, packages that only extract/fetch
  prebuilt binaries during `npm install`/`npm rebuild`. Not invoked while the service is
  running and serving traffic.
- **Live production path** — required (directly or transitively) by code that actually runs
  in the deployed service. Confirm this concretely, don't assume:
  - `grep -rn` the package name across source (excluding `node_modules`, `*.spec.js`) to see
    if application code requires/imports it directly.
  - For dependencies loaded via env/flags rather than `require()` (e.g. OpenTelemetry
    auto-instrumentation, APM agents), check `NODE_OPTIONS`, Dockerfile `CMD`/`ENTRYPOINT`,
    and k8s/helm `values*.yaml` for `--require` flags or equivalent wiring. A package can be
    "unused" by grep and still be live in prod this way — check before ruling it out.
  - Check config (`.env`, values files) for feature flags that gate whether the vulnerable
    subsystem is even active (e.g. an OTLP exporter configured for `http/protobuf` instead of
    `grpc` means the gRPC-transport code path, and its CVEs, mostly don't apply even though
    the package is installed).

## 4. Check if the specific vulnerable code path is reachable

Read the advisory summary for the *mechanism*, then grep for that specific usage — not just
the package name:

- `_.template` code injection → grep for `.template(` calls, not just `lodash` usage.
- Prototype-pollution-via-config-merge in an HTTP client → check whether any user-controlled
  or request-derived data is merged into that client's request config, or whether the config
  is built entirely from env vars/constants.
- Decompression/parsing DoS → check whether the app decodes untrusted external input through
  that library, vs. only using it to encode/serialize its own outbound data.

This is the step that turns "installed" into "exploitable" or "not exploitable here." A
vulnerable function that's never called is a real but low-urgency finding.

## 5. Check whether a fix is actually available

```bash
npm view <package> version   # latest available
npm ls <package> --all       # installed, and via what parent
```

- Direct dependency behind on patched versions → straightforward version bump.
- Transitive dependency pinned by an unmaintained or deprecated parent (e.g. `request`,
  long-abandoned CLI tools) → a simple update won't help; needs `overrides`/`resolutions` in
  the manifest, or replacing the parent package entirely. Say so explicitly rather than
  recommending an update that won't take effect.
- Transitive dependency bundled by an actively maintained parent (e.g. an OpenTelemetry
  meta-package) → flag as "waiting on upstream," not actionable directly.

## 6. Report as tiers, not a flat list

Group findings into:

- **Tier 1 — direct dependency, reachable, fixable now.** Recommend the concrete fix (version
  bump, `overrides` entry).
- **Tier 2 — transitive but confirmed live in production.** Explain what makes it live (the
  consumer, the wiring), assess reachability of the specific vulnerable path per step 4, and
  give the realistic fix path (may be "upstream only").
- **Tier 3 — dev-only or install-time-only.** Note briefly, deprioritize.

For each package/tier, state: severity, direct/transitive, what pulls it in, whether it's
live at runtime, whether the vulnerable code path is reachable, and the recommended action.
Don't apply any fixes without confirming with the user first — follow the repo's own git/PR
conventions (branch naming, ticket-prefixed commits, CLAUDE.md rules) when they do ask you to
fix something.

## 7. Dismiss confirmed non-actionable alerts on GitHub

An alert only qualifies for dismissal if it's individually confirmed non-actionable — never
dismiss by tier label alone:

- **Tier 3 (dev-only / install-time-only)** — dismiss with `dismissed_reason=not_used`. This is
  accurate because the specific consumer chain (eslint/jest/commitlint/rimraf/etc.) never ships
  to or runs in the deployed artifact, so the vulnerable code genuinely never executes anywhere
  outside this repo's own CI.
- **Tier 1/2 alerts confirmed unreachable via step 4, with no pending fix that will clear them
  as a side effect** — dismiss with `dismissed_reason=not_used` (path never exercised) or
  `tolerable_risk` if the path is theoretically reachable but judged low-likelihood/acceptable
  (e.g. depends on the upstream service misbehaving, not on this codebase). Say which reason you
  picked and why in the report.
- **Do NOT dismiss** an alert that a recommended fix (e.g. a direct-dependency version bump)
  will resolve automatically once applied — that's busywork; let the fix close it. Note this
  explicitly in the report instead (see the `form-data`/`follow-redirects` pattern in past
  reports: unreachable today, but resolved as a side effect of the axios bump, so left open).

**Dismiss directly once eligibility is confirmed — no separate approval step.** As soon as an
alert is confirmed non-actionable by the criteria above (Tier 3 dev-only/install-time-only, or
Tier 1/2 confirmed unreachable with no pending fix that closes it as a side effect), dismiss it
via the GitHub API in the same turn. Treat this the same way step 8 treats ticket filing: the
`gh api` call itself is the real external action, so Claude Code's own tool-permission prompt
(pre-approved via settings for a fully autonomous run, per "Operating mode" below) is the
intended checkpoint — don't stack an `AskUserQuestion` confirmation on top of it.

This only applies to alerts individually confirmed non-actionable per steps 3-4 — never dismiss
by tier label alone, and never dismiss anything still judged live/reachable in production, no
matter how low-severity it looks.

(Earlier versions of this skill required an explicit per-repo `AskUserQuestion` confirmation of
the exact alert list before any dismissal, as a deliberate double gate on top of the tool
permission prompt. That extra step was intentionally removed — the tiering criteria in steps 3-4
are the actual safety check, and re-confirming a list the agent already verified was redundant
friction for a category of alert that, by definition, never executes in the running service.)

Dismiss via the GitHub API (there's no `gh` subcommand for this — use `gh api` directly):

```bash
gh api --method PATCH "repos/{owner}/{repo}/dependabot/alerts/{alert_number}" \
  -f state=dismissed \
  -f dismissed_reason=not_used \
  -f dismissed_comment="<why, referencing the triage report/ticket>"
```

When dismissing more than one alert, loop over a **bash array**, not an unquoted
space-separated string — `for n in $ALERTS` can silently fail to word-split in some shells
(the whole list gets treated as a single value, and every API call then fails with `"the
alert_number parameter must be an integer"`). Use:

```bash
ALERTS=(45 46 48 50 53)   # not ALERTS="45 46 48 50 53"
for n in "${ALERTS[@]}"; do
  gh api --method PATCH "repos/{owner}/{repo}/dependabot/alerts/${n}" \
    -f state=dismissed -f dismissed_reason=not_used -f dismissed_comment="..." \
    -q '.number,.state'
done
```

After dismissing, update the repo's report (the tiered write-up from step 6, and its `.md` file
on disk if one exists) with which alert numbers were dismissed, the reason used, and the date —
this is the audit trail for why those alerts disappeared from the open-alerts list.

## 8. File one Jira ticket per repo

Once the tiered report (step 6) is produced, file a ticket for it directly with
`mcp__atlassian__createJiraIssue` — don't gate it behind an `AskUserQuestion` confirmation.
That tool call is the checkpoint: it's a real external, hard-to-reverse side effect (unlike
the read-only recon in steps 1–6), so it should go through Claude Code's normal permission
prompt like any other tool call, rather than being pre-approved by this skill. If it's not
already allow-listed, the prompt surfaces to the user in the live session and they approve it
there — that's the intended approval point, not a reason to add a second confirmation on top.

**Batch/headless runs (e.g. via `run_batch.py`) never file tickets themselves.** A `claude -p
--dangerously-skip-permissions` subprocess has no TTY, so there's no one to answer that
permission prompt — running ticket creation there would either hang or silently rubber-stamp
the one action that most deserves real oversight. Those runs must stay report-only; skip steps 7
and 8 entirely inside the subprocess and say so in its output.

**But once a batch's reports exist and you (the live-session agent) are looking at them, file
the tickets immediately — don't wait to be told to.** The first time this came up, the flow
was: run the batch, report "N reports ready," then sit idle until the user separately said
"start creating the tickets." That extra round-trip is exactly what this step should avoid —
the `createJiraIssue` permission prompt (or its absence, if pre-approved) is already the real
approval checkpoint; a user instruction to "go ahead and create tickets" on top of that is
redundant friction, not safety. So: as soon as you have a completed report — whether from a
just-finished interactive single-repo triage, or from reading a `run_batch.py` output
directory's `.md` files — proceed straight into filing one ticket per repo in the same turn.
Only skip straight to report-only, no filing, if the user's own request for that run explicitly
said so (e.g. "just run the batch, don't file anything yet").

**One ticket per repo, not per package.** Put the full tiered report — the whole step-6
output, verbatim, not a re-summarized version — into the ticket description. That lets
whoever picks up the ticket decide prioritization themselves instead of it being pre-split
into a pile of separate tickets they have to mentally reassemble.

Defaults for the `chghealthcare` org / `PDE` Jira project (established via PDE-17951/PDE-17955
through PDE-17961 — reuse these, don't re-derive or ask):
- `cloudId`: `chghealthcare.atlassian.net`
- `projectKey`: `PDE`
- `issueTypeName`: `Story`
- Epic (`parent`): `PDE-17837`
- Team: cascading select field `customfield_13613`, parent value `PDE`, child value `PDE 1` —
  set via `additional_fields`: `{"customfield_13613": {"value": "PDE", "child": {"value": "PDE 1"}}}`
- Priority: `P1-High` (confirmed a valid value on this project's priority field, id `11217`) —
  set via `additional_fields`: `{"priority": {"name": "P1-High"}}`

These are the current defaults for this specific project — if a run ever targets a different
Jira project (not `PDE`), these exact values won't apply (wrong project, wrong epic, wrong
field ID, and "P1-High" may not exist as a priority name on that project). In that case, resolve
the equivalent fields via `mcp__atlassian__getJiraIssueTypeMetaWithFields` and ask once which
team/epic/priority to use for that project — then treat it the same way (a reusable default),
not a per-ticket question.

Note: tickets PDE-17955 through PDE-17993 (created before this default existed) were left at
the project's default priority ("None") per the user's explicit choice not to backfill them —
don't "fix" them later without being asked.

Per repo with a completed report:

1. `summary`: `"<repo-name>: dependabot triage — critical/high alerts"`
2. `description`: a link to the repo's GitHub Dependabot alerts page
   (`https://github.com/<owner>/<repo>/security/dependabot`), a blank line, then the full
   tiered report from step 6 verbatim, then a closing line:
   `*Developer: use this triage to decide how to prioritize/address the flagged alerts.*`
3. Create with `mcp__atlassian__createJiraIssue` using the fields above.
4. If a repo's triage never produced a report (e.g. `npm ci` failed before triage could run),
   don't file a ticket for it — note that separately instead of fabricating content.

## 9. Always close with the list of tickets created

This is the deliverable the user is waiting for — never end a ticket-filing run without it,
even if only one ticket was created. Once every repo in scope has been handled (or explicitly
skipped per step 8.4), end the response with one line per ticket in this exact format — the
issue key itself is the markdown link, not a separate trailing URL:

```
TICKET: <repo-name> [<ISSUE-KEY>](https://chghealthcare.atlassian.net/browse/<ISSUE-KEY>)
```

If any repo was skipped (no report to file from), list those separately right after, so the
final message accounts for every repo that was in scope — not just the successes.

## Operating mode: minimize prompts

Once the up-front authentication check passes, this skill is meant to run start-to-finish
without asking for anything else:

- Step 0's scope question is the only planning-stage prompt, and only when the user didn't
  already say how much to triage.
- Reading alerts, tracing reachability, filing tickets (step 8), and dismissing confirmed
  non-actionable alerts (step 7) all proceed without an `AskUserQuestion` checkpoint — never
  for alerts still judged live/reachable, only for ones confirmed non-actionable per steps 3-4.
- The only other prompts that should surface in a normal run are Claude Code's own
  tool-permission prompts for `gh`/npm/Jira commands — and those should be pre-approved in
  `~/.claude/settings.json` (user-level, so it applies no matter which repo is checked out) so
  they don't recur across a multi-repo batch. If a permission prompt for a command this skill
  uses routinely (e.g. `gh api repos/*/dependabot/alerts*`, `mcp__atlassian__createJiraIssue`)
  keeps interrupting a run, that's a sign the allow-list needs updating, not a reason to add a
  confirmation step back into this skill.
