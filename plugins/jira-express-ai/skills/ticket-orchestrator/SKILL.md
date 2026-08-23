---
name: ticket-orchestrator
description: Governs how the orchestrator decides whether and how to act on AI-Work Jira tickets in the PDE project. Use this before starting, resuming, or skipping any PDE ticket session.
user-invocable: true
---

# Orchestrator dispatch logic

## Role

The orchestrator logic runs as a Python script — not as AI inference. When
this skill is invoked, your only job is to run the script and report results.

## How to run

```bash
cd /path/to/pde-ops-agent   # target project root — tickets/ lives here
"$CLAUDE_PLUGIN_ROOT/.venv/bin/python" "$CLAUDE_PLUGIN_ROOT/skills/ticket-orchestrator/orchestrator.py"
```

`CLAUDE_PLUGIN_ROOT` (set by Claude Code and Copilot CLI to this plugin's install
location) points the script at its own sibling skill files regardless of which
project directory it's run from — see "Working directory" below for the
separate, unrelated cwd requirement. `"$CLAUDE_PLUGIN_ROOT/.venv/bin/python"` — not
bare `python3` — is this plugin's own self-contained virtualenv, provisioned
automatically by the `bootstrap-deps.sh` `SessionStart` hook (see
`requirements.txt` at this plugin's root); it guarantees `requests` is present
before this ever runs.

Don't pre-flight-check and refuse before running this — the script itself
handles the things that look like blockers:
- `tickets/` and `tickets/archive/` don't need to exist yet; the script
  creates them on its own (see `main()`).
- `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN` don't need to be set as shell env
  vars by hand — if this plugin's userConfig has them configured, a
  `SessionStart` hook (`scripts/bootstrap-env.sh`) already mirrored them into
  `.env` at this plugin's root, and `main()` loads that into the environment
  before calling `_auth()` (see `_load_plugin_env()`). Only refuse if
  `_auth()` itself errors — that means neither the env vars nor userConfig
  are actually set.
- Repo clones don't need to already exist just to run the dispatch loop —
  they only matter once there's an actual ticket to work, and the **repos
  directory** location itself is configurable (see below), not necessarily
  cwd.

The script is stateless and re-runnable — safe to schedule via cron.

---

## Design reference (for humans, not AI instructions)

## Working directory

All relative paths (`tickets/`, `tickets/archive/`) are resolved relative to the
**current working directory** where the orchestrator is invoked — not the skill
directory. Ensure you are in the correct directory before running.

The **repos directory** defaults to the current working directory itself — repository
clones are expected as direct subfolders of cwd (e.g., `./my-repo/`) — unless
overridden via `REPOS_DIR` (env var or this plugin's userConfig equivalent,
`CLAUDE_PLUGIN_OPTION_REPOS_DIR`; see `_repos_dir()`).

## Run loop

Each orchestrator run begins with a fresh Jira query — never a cached list.

1. Query Jira for up to `MAX_TICKETS_PER_RUN` (100) tickets matching, ordered
   by rank so higher-priority tickets are considered first:
   ```
   project = PDE
     AND labels = "AI-Work"
     AND statusCategory != Done
     AND status != Cancelled
     AND status != Released
     AND status != Backlog
   ORDER BY Rank ASC
   ```
   100 is a query-size cap, not how many actually get dispatched — see step 4's
   `MAX_DISPATCHES_PER_RUN` note below for that.
2. For each ticket returned, verify the key starts with `PDE-`. If it does not,
   write a log entry "<KEY>: key does not start with PDE- — skipping" and skip it.
   This is a hard safety check; do not process non-PDE tickets under any circumstances.
3. **Cleanup pass:**
   - **Archive:** scan all `tickets/*/` directories. For any ticket folder whose
     key is **not** in the Jira results — done, cancelled, released, backlog-ed,
     or the `AI-Work` label was removed — archive it to `tickets/archive/<KEY>/`
     and write log "<KEY>: archived to tickets/archive/<KEY>", **unless its lock
     is currently held** (a session is genuinely still running — leave it alone
     rather than archiving out from under it). Also copies that ticket's Claude
     Code session transcripts (worker + every specialist it launched, all
     sharing `tickets/<KEY>/` as their cwd) into
     `tickets/archive/<KEY>-<date>/claude-sessions/`, then clears them from
     Claude Code's own storage — best-effort, since the storage location is
     reverse-engineered rather than a documented API (see
     `_claude_project_dir()`). Deletion isn't just tidiness: a future
     relaunch of this same ticket key reuses the exact same `tickets/<KEY>/`
     path, so leaving the old session history in place would mix a future
     run's transcripts into the same bucket as this run's.
   - **Purge:** delete any `tickets/archive/<KEY>/` folder older than **7 days**,
     using the folder's own modification time. Write log
     "<KEY>: purged archive (older than 7 days)".
     ```bash
     find tickets/archive/ -mindepth 1 -maxdepth 1 -type d -mtime +7 | while read dir; do
       echo "[cleanup] Purging old archive: $dir"
       rm -rf "$dir"
     done
     ```
4. For each ticket returned (PDE keys only), in rank order, execute the full **"Before
   acting on any ticket"** workflow below — until `MAX_DISPATCHES_PER_RUN` (5) tickets
   have actually been dispatched (a session genuinely launched or resumed), then stop for
   this run. This cap is deliberately separate from step 1's 100-ticket query cap: among
   up to 100 rank-ordered candidates, an unknown number belong to other engineers (skipped
   by the assignee gate) or already have a session running (skipped by the lock check) —
   neither kind of skip counts against the 5, since nothing was actually launched. Only a
   real dispatch consumes budget; once 5 of those happen, remaining candidates (including
   ones never even reached) wait for the next run. The orchestrator is **stateless and
   re-runnable** — it fires off workers and exits. Do not block waiting for workers to
   finish. Move immediately to the next ticket.

If the query returns zero tickets, write a log entry: "No AI-Work tickets found"
and exit.

## The two signals

| Signal | Answers | Source |
|---|---|---|
| Status | What action does this status call for, if any? | Jira ticket (read fresh each run) |
| Lock | Is a session currently running for this ticket? | `tickets/<KEY>/.worker.lock` |

Jira is the source of truth for status. Never infer readiness from anything
cached locally — check Jira fresh every run. The query already guarantees
the `AI-Work` label is present; no secondary label check is needed.

Nothing else in the ticket directory is the orchestrator's concern — no
context file, no copied skill files, no state file. The lock is the only
thing it owns in there; everything else belongs to the worker and its
specialists.

## Status rewind moved to the worker

Humans can manually move a ticket to any Jira status, including one that's
ahead of what's actually been done (e.g. `In Progress` with no `discovery.md`
yet). Sanity-checking that and self-correcting Jira used to happen here, but
it required knowing which artifact filenames prove which stage is done
(`discovery.md`, `implementation-notes.md`) — domain knowledge that belongs
to the worker and its specialists, not to a generic dispatcher. Fresh-vs-resume
here needs no artifact inspection at all — but it's **not** purely "does a
directory exist," either: it's `jira_status == "To Do" OR no directory exists
yet for this ticket` (see `process_ticket()`). Two consequences worth naming:
a ticket can be Fresh on a status other than `To Do` (a human skipped straight
to `In Discovery`, or further, on a brand-new ticket); and `To Do` is *always*
Fresh even when a directory already exists (a ticket cycled back to `To Do`
after being worked before) — that stale directory gets archived out of the
way, not reused, before the fresh launch (see step 4b below).

The rewind check itself still happens, just one level down, and independently
of whether the launch was Fresh or Resume — a Fresh session gets the exact
same worker-side validation as a resumed one. See `ticket-worker/SKILL.md`'s
Startup section for how the worker validates Jira's status against its own
directory before routing, and self-corrects if a human skipped a stage.

## Status → action table

The orchestrator delegates work in exactly four statuses. Everything else is
either a human gate or a terminal state — touch nothing.

**The assignee gate applies to all four, not just `To Do`.** Before
dispatching any ticket in one of the four actionable statuses, it must
already be assigned to the user running the orchestrator — checked once per
ticket, ahead of the per-status branching below (see `main()`). If
unassigned or assigned to someone else, skip with log "<KEY>: not assigned
to current user (assigned to: <name>) — skipping", regardless of which of
the four statuses it's in. A ticket sitting in `In Discovery`/`In
Progress`/`UAT Review` but assigned to someone else is skipped exactly like
an unassigned `To Do` ticket would be — this isn't an intake-only check.

| Status | Category | Action |
|---|---|---|
| To Do | **Fresh** | Always a fresh session (`--name`), even if a `tickets/<KEY>/` directory already exists from a prior run of this same ticket — that directory is archived out of the way first (see step 4b below), never reused. |
| In Discovery | **Fresh or Resume** | Fresh (`--name`) if no `tickets/<KEY>/` directory exists yet for this ticket (a human skipped straight here on a brand-new ticket); otherwise Resume (`--resume`) the existing session — the worker reads any rejection comment and re-runs discovery. |
| In Progress | **Fresh or Resume** | Same rule as `In Discovery`: Fresh if no directory exists yet, otherwise Resume to run implementation. |
| UAT Review | **Fresh or Resume** | Same rule again: Fresh if no directory exists yet, otherwise Resume to merge. |
| QA Review | **Human gate** | Do nothing. |
| In Review | **Human gate** | Do nothing — the orchestrator never dispatches for this status. A human addresses PR comments by moving the ticket back to `In Progress`; that's what actually triggers `ticket-worker` to run the review agent (see `ticket-worker/SKILL.md`'s Path: Implementation, second branch). |
| Backlog | **Ignored** | Do nothing. Excluded by JQL query — cannot appear, but documented for completeness. |
| Done | **Terminal** | Do nothing. |

**Rule:** `--name` (Fresh) is used whenever `jira_status == "To Do"` OR no
`tickets/<KEY>/` directory exists yet for this ticket — never "only for `To
Do`." Every other case uses `--resume`. Either way, the session is created
once per ticket and carries all context across every stage — a Fresh launch
still gets the same worker-side rewind/sanity-check as a Resumed one (see
"Status rewind moved to the worker" above); Fresh vs. Resume is about
session identity, not about skipping that check.

Any status not in this table: do nothing, and do not guess.

## Worker lock

Liveness is answered by an OS-level `flock` on `tickets/<KEY>/.worker.lock`,
not by a stored PID plus a liveness check. This is more than a style choice:
`flock` is tied to the open file description, not to any cleanup code
actually running — it's released unconditionally by the kernel the instant
the holding process's file descriptors go away, whether that's a clean exit,
an uncaught error, `kill -9`, an OOM kill, or the machine losing power
outright. A stored PID can't make that guarantee (nothing runs to update it
on an abrupt death), and after a reboot a recorded PID can even be
legitimately reused by a totally unrelated process — a held-or-not lock
can't be fooled that way.

The lock is handed to the launched worker across the launch itself: acquire
it non-blockingly *before* deciding to launch, hand its file descriptor to
the new `claude` process (inherited across the process handoff, not
re-acquired by the child itself), then let go of your own reference. Because
`flock` locks live with the open file description — which the child's
inherited descriptor also refers to — the lock stays held by the child for
its entire lifetime, with no gap between "decided to launch" and "the lock
is actually held."

- **Checking if a session is live** (read-only, never holds the lock):
  try a non-blocking exclusive `flock` on `.worker.lock` and immediately
  release it if it succeeds. Held → a session is live, leave it alone.
  Free (or the file doesn't exist) → nothing is running, safe to proceed.
- **Actually launching** (step 7 below): acquire the same lock the same
  way, but this time *keep holding it* through the launch and hand it off.

**Developer shortcut:** `claude --resume "<KEY>"` — the ticket key is both the
session's display name (`--name="<KEY>"` at launch) and its real `--resume`
handle. Verified directly: Claude Code resolves `--resume` by the name set via
`--name`, non-interactively, no UUID needed — as long as that name is
unambiguous. Names aren't unique, though: a second fresh launch reusing the
same `--name` creates a second, distinct session sharing it, and `--resume`
then hard-errors demanding a session ID to disambiguate. That's why every
Fresh launch — not just `To Do` — always renames any existing same-named
session out of the way first (via a headless `/rename` prompt — see step 4a)
rather than just reusing the key blindly.

## Jira MCP

All Jira operations use the **Jira MCP tool** provided by the workspace.

- Query tickets: `jira_search` with JQL
- Read ticket: `jira_get_issue`
- Transition ticket: `jira_transition_issue`
- Assign ticket: `jira_assign_issue`

## Before acting on any ticket, in order

1. Read the current status from Jira. Look it up in the Status → action table.
   If the row says "do nothing," write a log entry "<KEY>: <Status> — skipping"
   and stop processing this ticket. **Do not check the lock — Jira status
   alone is sufficient to skip.**

2. For any of the four actionable statuses (To Do, In Discovery, In Progress,
   UAT Review), skip if the ticket is not assigned to the current user —
   this applies to all four, not just `To Do`:
   ```
   if assignee_id != current_user_id:
       log "<KEY>: not assigned to current user (assigned to: <name>) — skipping"
       return
   ```

3. If `tickets/<KEY>/` exists, check whether its lock is currently held (see
   "Worker lock" above — a non-blocking, non-consuming check).
   - Held: log "<KEY>: session already running — skipping" and stop.
   - Free, or the directory doesn't exist yet: proceed. No need to distinguish
     "never ran," "finished cleanly," or "crashed" — a free lock means safe to
     proceed either way.

4. Determine whether this is a **Fresh** launch: `jira_status == "To Do"` OR
   no `tickets/<KEY>/` directory exists yet for this ticket. If Fresh:

   a. Rename any existing claude session still named `<KEY>` out of the way,
      via a headless prompt — safe to run even if no such session exists
      (there's simply nothing to rename, which isn't an error worth acting on):
      ```bash
      claude --resume="<KEY>" -p "/rename <KEY>-archived-$(date +%Y-%m-%d)" \
        --permission-mode=bypassPermissions
      ```
      This has to happen *before* archiving the ticket directory below and
      *before* launching the fresh session in step 7, which will reuse the
      plain name `<KEY>` — without this, `--resume "<KEY>"` later would match
      both the old and new sessions and hard-error demanding a session ID.

   b. If `tickets/<KEY>/` already exists (a ticket cycled back to `To Do`
      after being worked before — the ordinary "no directory yet" case has
      nothing to do here), **archive** it rather than deleting it: move it to
      `tickets/archive/<KEY>-<date>/`, the same operation the cleanup pass
      uses for tickets that dropped out of Jira's active results (see "Run
      loop" above), including copying its Claude Code session transcripts.
      This preserves the prior attempt's history instead of discarding it,
      and keeps it out of the way of the fresh directory step 5 creates.

5. Create `tickets/<KEY>/` if it does not exist — the lock file has to live
   inside it.

6. Acquire `tickets/<KEY>/.worker.lock` — non-blocking, and this time *keep
   holding it* (see "Worker lock" above). If someone else grabbed it in the
   moment since step 3's check, log "<KEY>: lock held by another process at
   launch time — skipping this run" and stop; this is a race so unlikely
   it's not worth more than failing safe.

7. Launch the session (cwd is set directly on the subprocess — Claude Code's
   CLI has no `-C` equivalent, unlike Copilot's). `--name`/`--resume` both
   just use the plain ticket key — step 4a's rename is what keeps that
   unambiguous for a fresh launch. The repos directory rides along as plain
   text after the skill name in the same prompt — verified directly that
   Claude Code passes text following a skill invocation straight through as
   real input, so no context file is needed to hand it over:
   ```bash
   REPOS_DIR="$(pwd)"
   # The real Python implementation creates this directory first
   # (log_dir.mkdir(parents=True, exist_ok=True)) — without it, a
   # not-yet-existing AGENT_CHILD_LOG_DIR would make the redirect below fail
   # with "No such file or directory" inside this backgrounded subshell,
   # silently.
   mkdir -p "${AGENT_CHILD_LOG_DIR:-$TICKET_DIR}"
   # $! must be read inside the same subshell that backgrounds the process —
   # capturing it via `echo $!` in a command substitution, not outside a
   # plain (...) group, which would already have exited by the time $! is read.
   # The lock's fd must be inherited by the child (not re-acquired by it) —
   # in the real Python implementation this is subprocess.Popen's pass_fds;
   # written here as a conceptual bash equivalent using exec's fd-duplication.
   if [ "$IS_FRESH" = "true" ]; then
    # Fresh session — jira_status was "To Do", or no ticket directory existed
    # yet for this key (see step 4 above); NOT simply "jira_status == To Do":
    SESSION_PID=$(cd "$TICKET_DIR" && nohup claude --name="<KEY>" \
      --permission-mode=bypassPermissions \
      --add-dir "$TICKET_DIR" \
      --add-dir "$REPOS_DIR" \
      -p "/ticket-worker Repos directory: $REPOS_DIR" \
      > "${AGENT_CHILD_LOG_DIR:-$TICKET_DIR}/<KEY>.log" 2>&1 & echo $!)
   else
    # Resume existing session:
    SESSION_PID=$(cd "$TICKET_DIR" && nohup claude --resume="<KEY>" \
      --permission-mode=bypassPermissions \
      --add-dir "$TICKET_DIR" \
      --add-dir "$REPOS_DIR" \
      -p "/ticket-worker Repos directory: $REPOS_DIR" \
      > "${AGENT_CHILD_LOG_DIR:-$TICKET_DIR}/<KEY>.log" 2>&1 & echo $!)
   fi
   ```
   - The `cd ... && ... &` sets the worker's cwd without a `-C` flag.
   - `--name="<KEY>"` on a fresh launch sets both the display label and the
     future `--resume` handle — they're the same thing under Claude Code.
   - `--permission-mode=bypassPermissions` auto-approves every tool call
     (required for non-interactive mode) — the Claude Code equivalent of
     Copilot's `--allow-all-tools --allow-all-urls`.
   - `--add-dir` restricts file access to the ticket and repos directories only.
   - `-p "/ticket-worker Repos directory: $REPOS_DIR"` invokes the worker skill
     as the initial prompt, with the repos directory as its only argument.
     `ticket-worker` is invoked by its bare name, not namespaced under this
     plugin — verified directly that an installed plugin's skills resolve by
     bare name too, from any directory, as long as it's unambiguous.
   - The Atlassian `cloudId` is `e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2`.
   - Output is redirected to `<KEY>.log` in `$AGENT_CHILD_LOG_DIR` if that env
     var is set (a calling process manager collecting logs centrally), or in
     `$TICKET_DIR` otherwise — opened in append mode either way, so a
     resumed session's output keeps accumulating into the same file rather
     than starting over.
   - A fresh restart's rename step (4a) already freed up the plain key name
     before this point, so `--name="<KEY>"` here can never collide.

8. Log "<KEY>: session launched (PID $SESSION_PID) — moving to next ticket"
   and move on immediately. There's nothing to wait for or confirm: whatever
   would cause a launch to silently fail, the next run finds the lock free
   again and just retries — the same outcome as detecting the failure
   immediately, just up to one cron interval later. Nothing in
   `tickets/<KEY>/` needs to be read or written again after this point;
   everything from here on belongs to the worker.

## Orchestrator responsibilities

- Query Jira fresh each run.
- Own the `tickets/` directory structure: create a ticket's folder, and later
  archive/purge it — never anything inside it beyond the lock file.
- Archive `tickets/*/` folders for tickets no longer active in Jira results —
  not just ones that finished cleanly; a ticket that got
  Cancelled/Released/Backlog-ed or lost its `AI-Work` label counts too. The
  only exception is a genuinely in-flight session (lock held) — leave that
  alone rather than archiving out from under it.
- Decide whether to launch fresh or resume, and do it — via the worker's
  lock file, the ticket's Jira status (`To Do` is always fresh), and whether
  a directory already exists, alone; never anything else in the ticket
  directory, which belongs entirely to the worker. Does not sanity-check
  Jira's status against what's actually been done — that's the worker's job
  now (see below), since it requires knowing worker-domain artifact
  filenames the orchestrator has no other reason to know.
- **Exit after dispatching every ticket this run** — no waiting, no polling,
  no long-running blocking of any kind.
- Designed to be run on a schedule (e.g., cron every 5–15 minutes).

## Worker responsibilities

- Hold the lock for its entire lifetime (inherited from the orchestrator at
  launch, released automatically — by the kernel, unconditionally — on any
  exit, clean or otherwise).
- Own everything else inside its ticket directory: every real artifact
  (research, implementation, review, merge notes) and its own logged output.
  No local state file — see `ticket-worker/SKILL.md`'s Startup section for
  why one existed before and was removed.
- Transition Jira tickets as work progresses or fails, and decide what
  "success" and "failure" mean for each stage. This includes sanity-checking
  Jira's status against what's actually in its own directory and
  self-correcting (rewinding) if a human skipped a stage — the orchestrator
  never does this, it only decides whether to launch or resume.
