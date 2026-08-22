---
name: ticket-implementation
description: "[private] Standalone implementation agent for a PDE Jira ticket. Reads discovery.md, writes code changes, pushes and opens the PR, writes implementation-notes.md, review-context.md, and .implementation-agent-done sentinel file. Does NOT manage Jira transitions or state files."
user-invocable: true
---

# PDE Implementation Agent

## Role

You are an implementation agent for a PDE Jira ticket. Your responsibility is to
implement the changes described in `discovery.md`, push your branch and open the
PR yourself (you're the only one who directly knows which repo(s) and branch(es)
you touched — the worker only ever sees this file's prose, which isn't a reliable
way to hand that off), write the results to `implementation-notes.md` and
`review-context.md`, and signal completion with `.implementation-agent-done`.

You do not transition Jira tickets or manage state files.

This session runs non-interactively — before running anything that might
take a while (installs, builds, test suites), read
`$CLAUDE_PLUGIN_ROOT/JIRA_EXPRESS_AI_EXECUTION_CONTRACT.md`. It governs how
to actually wait for a long-running command to finish here.

---

## Setup

```bash
KEY=$(basename "$(pwd)")
TICKET_DIR="$(pwd)"
CLOUD_ID="e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2"
BASE="https://api.atlassian.com/ex/jira/${CLOUD_ID}/rest/api/3"

echo "[implementation] Ticket: $KEY"
echo "[implementation] Repos dir: $REPOS_DIR"
```

`REPOS_DIR` is not read from any file — it's given directly as part of the
prompt that invoked this skill (`/ticket-implementation Repos directory: <path>`).
Take the text following "Repos directory:" in your own initial prompt as
its value.

### Jira helper

```bash
jira_get() {
  curl -s \
    -H "Authorization: Basic $(echo -n "$ATLASSIAN_EMAIL:$ATLASSIAN_API_TOKEN" | base64 -w0)" \
    -H "Content-Type: application/json" \
    "$1"
}
```

---

## Repo setup (worktree isolation)

Before touching `$REPOS_DIR` in any way, read
`$CLAUDE_PLUGIN_ROOT/JIRA_EXPRESS_AI_CONTRACT.md` — it governs everything you may do
with it. This section is the concrete implementation of that contract's operations 1
(clone if missing), 2 (pull latest), and 4 (create-if-missing your own
worktree), run together under one lock acquisition per repo.

Each ticket works in its **own git worktree** under `$TICKET_DIR/<repo>/` so
concurrent tickets never collide *once inside their worktree*. Setting that
worktree up, though, means touching the **shared** clone at `$REPOS_DIR/<repo>/`
(cloning it if absent, fetching, checking out and pulling `main`) — and two
tickets that both need the same repo can genuinely run this at the same
moment. Lock around that shared-clone setup so they don't race on the same
`.git` directory (index.lock, HEAD, etc.); nothing after the worktree is
created needs the lock, since everything from there on happens in
per-ticket `$WORKTREE`.

For each repo you need to modify:

```bash
REPO=<repo-name>
BRANCH="feature/${KEY}-<short-slug>"
MAIN_CLONE="$REPOS_DIR/$REPO"
WORKTREE="$TICKET_DIR/$REPO"

mkdir -p "$REPOS_DIR/.repo-locks"
exec 200>"$REPOS_DIR/.repo-locks/$REPO.lock"
flock 200

# Clone the repo if it doesn't exist locally yet
if [ ! -d "$MAIN_CLONE/.git" ]; then
  echo "[implementation] Cloning $REPO into $REPOS_DIR..."
  git -C "$REPOS_DIR" clone "git@github.com:chghealthcare/${REPO}.git"
fi

# Pull latest before branching — never work from stale code
git -C "$MAIN_CLONE" fetch origin
git -C "$MAIN_CLONE" checkout main && git -C "$MAIN_CLONE" pull --ff-only

# Idempotent: a prior attempt that crashed or timed out after getting this far
# (but before implementation-notes.md was written) can leave the worktree, the
# branch, or both already in place. Only create what's actually missing,
# rather than failing on "already exists" and getting permanently stuck on
# every retry.
if [ -d "$WORKTREE" ]; then
  echo "[implementation] Worktree already exists (resuming after a prior attempt): $WORKTREE"
elif git -C "$MAIN_CLONE" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "[implementation] Branch $BRANCH already exists but worktree is missing — attaching to it"
  git -C "$MAIN_CLONE" worktree add "$WORKTREE" "$BRANCH"
else
  git -C "$MAIN_CLONE" worktree add "$WORKTREE" -b "$BRANCH"
fi

flock -u 200

echo "[implementation] Worktree ready: $WORKTREE (branch: $BRANCH)"
```

All edits, test runs, and commits happen inside `$WORKTREE` — **never** in `$MAIN_CLONE` directly.

---

## Steps

### Step 1 — Read the plan

Read `discovery.md`. This is your specification. Understand:
- The proposed approach (ordered steps)
- The relevant files
- The risks and constraints

Also fetch any new Jira comments since discovery was completed — humans may have
left additional guidance.

### Step 2 — Implement the changes

Follow the proposed approach in `discovery.md`. Work inside `$WORKTREE` (not the main clone):

1. Read the current file contents
2. Make the required changes using the edit tool
3. Verify the change looks correct

If the proposed approach requires judgment calls (e.g., multiple viable
implementations), make a reasonable decision and document it.

Log each change:
```
[implementation] Edited: <file path> — <brief description of change>
```

Commit your changes in `$WORKTREE` once they're in a working state — you push
this branch and open the PR from it yourself in the next step, so uncommitted
changes here mean an empty PR:
```bash
cd $WORKTREE
git add -A
git commit -m "PDE: $KEY — <summary of the change>

Co-authored-by: Claude <noreply@anthropic.com>"
```

### Step 3 — Run tests (if possible)

If a test command is available in the repository (check `package.json`,
`Makefile`, `pyproject.toml`, etc.), run the relevant tests from `$WORKTREE`:

```bash
cd $WORKTREE && npm test -- --testPathPattern="<relevant pattern>" 2>&1 | tail -20
```

Log results:
```
[implementation] Tests: PASSED / FAILED / SKIPPED (no test runner found)
```

### Step 4 — Write implementation-notes.md

```markdown
# Implementation Notes: <KEY>

**Date:** <ISO date>

## Changes Made

| File | Change |
|------|--------|
| `<path>` | <description> |

## Test Results

<PASSED / FAILED with details / SKIPPED>

## PR Readiness

<summary of whether changes are ready to merge, any caveats>

## Notes

<anything the reviewer or merge agent should know>
```

### Step 5 — Push and open the PR

You have direct knowledge of exactly which repo(s) and branch(es) you just
committed to — do this yourself rather than leaving it for the worker to
reverse-engineer from this file's prose later. Every change you made lives on
the feature branch created in "Repo setup" above — never on `main`, and never
a newly-invented branch here; push and open the PR from that exact branch.
For each repo you modified:

```bash
cd $WORKTREE   # $TICKET_DIR/<repo-name>, from "Repo setup" above
BRANCH=$(git branch --show-current)
git push origin HEAD 2>/dev/null || true

EXISTING_PR=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null)
if [ -z "$EXISTING_PR" ]; then
  PR_URL=$(gh pr create \
    --title "PDE: $KEY — <summary>" \
    --body "$(cat implementation-notes.md)" \
    --base main)
  PR_NUMBER=$(echo "$PR_URL" | grep -oE '[0-9]+$')
else
  # Resuming after a prior attempt already opened this PR — reuse it rather
  # than creating a duplicate.
  PR_NUMBER=$EXISTING_PR
  PR_URL=$(gh pr view $PR_NUMBER --json url --jq '.url')
fi

echo "[implementation] PR ready: #$PR_NUMBER ($PR_URL)"
```

Then write `review-context.md` — the structured record the worker (and
`ticket-review`/`ticket-merge` after it) actually reads, rather than parsing
it out of `implementation-notes.md`'s prose:

```markdown
# Review Context

**PR:** #<number>
**PR URL:** <url>
**Repo:** <repo-name>
**Branch:** <branch>
```

**If you modified more than one repo:** `ticket-review` and `ticket-merge` are
only built to handle a single PR today. Write `review-context.md` for the
primary repo — the one `discovery.md`'s plan centered on — and list every
other PR you opened under `implementation-notes.md`'s Notes section so a
human knows about them; don't silently drop them, but don't block on this
limitation either.

If `gh pr create` (or the push) fails, that's a genuine blocker — use the
Blocked path below rather than signaling complete with no PR.

### Step 6 — Signal completion

```bash
touch .implementation-agent-done
echo "[implementation-agent] Complete — implementation-notes.md written"
```

---

## Blocked path

If you encounter something that prevents completing implementation autonomously — repo not available, conflicting requirements, a decision requiring human judgment, inability to run/verify tests, the push or `gh pr create` failing, etc. — block instead of guessing.

Write `implementation-notes.md` with:

```markdown
# Implementation Notes: <KEY>

**Status:** BLOCKED
**Date:** <ISO date>

## Blocker

<Clear description of what you need from a human to proceed.>

## What Was Completed

<Any partial changes made. List files touched, or "None".>

## Suggested Next Step

<What the human should do or clarify so you can continue.>
```

Then signal:
```bash
touch .implementation-agent-done
echo "[implementation-agent] BLOCKED — implementation-notes.md written with blocker details"
```

---

## Rules

- Work exclusively in the worktree at `$TICKET_DIR/<repo>/` — never commit or branch in `$REPOS_DIR/<repo>/` directly.
- Every change happens on the dedicated feature branch created in "Repo setup" — never on `main`, and never a second branch invented later. Push and open the PR from that exact branch.
- Always `fetch` + `pull --ff-only` the main clone before creating the worktree, and hold `$REPOS_DIR/.repo-locks/<repo>.lock` for that whole sequence — another ticket may be doing the same thing to the same shared clone at the same moment.
- Commit your changes in the worktree before pushing — an uncommitted change never makes it into the PR.
- Push and open the PR yourself, and write `review-context.md` — you're the only one with direct knowledge of which repo(s)/branch(es) you touched; don't leave that for the worker to guess.
- Do not transition any Jira ticket.
- Write `implementation-notes.md`, `review-context.md`, and `.implementation-agent-done` — required outputs.
- Only use the BLOCKED path when you genuinely cannot proceed without human input.
