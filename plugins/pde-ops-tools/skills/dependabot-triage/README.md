# Dependabot Triage

AI skill for triaging GitHub Dependabot alerts by whether each flagged package is actually
*reachable* at runtime — not just installed — then dismissing confirmed non-actionable alerts on
GitHub and filing one Jira ticket per repo with the full triage report.

## Overview

Severity labels from GitHub/npm advisories describe the vulnerability in the abstract. This skill
answers, for each flagged package: is it direct or transitive, does the code that pulls it in
actually run in production, and is the specific vulnerable code path ever reached. A large
fraction of alerts in a typical repo turn out to be eslint/jest/commitlint transitive deps that
never ship, or install-time tools like `node-gyp` that never run while serving traffic — this
tells you which alerts are which before you spend time on any of them.

## Dependencies

### Required tools

- **`gh`** (GitHub CLI), authenticated (`gh auth status`), with write access to the target repo's
  security alerts — needed to pull alerts and to dismiss confirmed non-actionable ones.
- **An Atlassian/Jira MCP connector**, connected and with permission to create issues in the
  `PDE` Jira project — needed for ticket filing and the up-front auth check this skill runs
  before triaging anything.
  - **Easiest path**: run the `setup-companion-tools` skill (in this same plugin) and pick
    Atlassian — it checks for a pre-existing `claude.ai`-provisioned connector that may already
    cover this, and walks through OAuth if not.

### Environment & tools for the batch scripts

- `git`, `npm`, `python3` on PATH.
- `run_batch.py` additionally needs the `claude` CLI on PATH (it shells out to a headless
  `claude -p ... --dangerously-skip-permissions` per repo).

Both scripts are stdlib-only Python 3 — no `pip install` needed, and they run unmodified on
Windows, Linux, or macOS.

## Running

### Single repo, interactively

From inside any repo, in a live Claude Code session:

```
/pde:dependabot-triage
```

or hand it a scope directly:

```
/pde:dependabot-triage triage all open critical and high severity alerts for this repo
```

It checks `gh auth status` and Jira access up front, asks for scope if you didn't give one, then
pulls alerts, classifies each package (direct/transitive, dev-only/install-time/live-in-prod,
reachable/not), reports tiers, dismisses confirmed non-actionable alerts on GitHub, and files one
Jira ticket per repo. Dismissals and ticket filing happen automatically once confirmed — the
`gh api`/`createJiraIssue` tool-permission prompts are the approval checkpoint, not a separate
confirmation question. See `SKILL.md` for the full step-by-step spec.

### Many repos, in batch

Two companion scripts live alongside `SKILL.md` in this directory:

**`list_target_repos.py`** — finds which repos in a GitHub org your team actually owns, verified
via each candidate's `CODEOWNERS` file (not just a name-prefix guess):

```bash
python3 list_target_repos.py                  # org=chghealthcare, team=@chghealthcare/pde
python3 list_target_repos.py --libraries-only  # shared libraries only (name has "library"/"-utils")
python3 list_target_repos.py --services-only   # everything else
```

**`run_batch.py`** — clones (if missing), `npm ci`s, and triages every repo in scope, in
parallel, headlessly and **report-only** (it never dismisses alerts or files tickets — a headless
subprocess has no TTY to answer those permission prompts):

```bash
python3 run_batch.py ~/dev chghealthcare 6                   # everything, 6 at a time
python3 run_batch.py --libraries-only ~/dev chghealthcare 4  # libraries first
```

Scope a run to specific repos with `REPO_FILTER` (extended regex):

```bash
REPO_FILTER='chg-work-schedule-service|pde-alerting-library' python3 run_batch.py /tmp/test chghealthcare 2
```

Output lands in a timestamped directory: one `<repo>.md` report and one `<repo>.log` trace per
repo, plus a final OK/SKIP/FAIL summary. Once a batch finishes, ask Claude in a live interactive
session to pick up from those reports — it dismisses confirmed non-actionable alerts and files
one ticket per repo, same as the single-repo flow, now that there's a TTY to answer the
permission prompts.

## Configuration

Ships configured for the `chghealthcare` org / `PDE` Jira project — already correct for this
repo, no changes needed. See the Configuration table near the top of `SKILL.md` if this skill is
ever pointed at a different org or Jira project.

## Files

- `SKILL.md` — full technical specification for the single-repo triage flow.
- `list_target_repos.py` — finds team-owned repos for a batch run.
- `run_batch.py` — clones/installs/triages many repos in parallel, report-only.
