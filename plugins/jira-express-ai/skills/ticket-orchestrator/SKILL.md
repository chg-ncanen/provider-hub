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
cd /path/to/pde-ops-agent   # must be the target project root (tickets/, repo clones live here)
python3 "$CLAUDE_PLUGIN_ROOT/skills/ticket-orchestrator/orchestrator.py"
```

`CLAUDE_PLUGIN_ROOT` (set by Claude Code and Copilot CLI to this plugin's install
location) points the script at its own sibling skill files regardless of which
project directory it's run from — see "Working directory" below for the
separate, unrelated cwd requirement.

Required env vars: `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`.

The script is stateless and re-runnable — safe to schedule via cron.

---

## Design reference (for humans, not AI instructions)

## Working directory

All relative paths (`tickets/`, `tickets/archive/`) are resolved relative to the
**current working directory** where the orchestrator is invoked — not the skill
directory. Ensure you are in the correct directory before running.

The **repos directory** defaults to the current working directory itself — repository
clones are expected as direct subfolders of cwd (e.g., `./my-repo/`). There is no
separate `repos/` subdirectory unless explicitly configured otherwise.

## Run loop

Each orchestrator run begins with a fresh Jira query — never a cached list.

1. Query Jira for all tickets matching:
   ```
   project = PDE
     AND labels = "AI-Work"
     AND statusCategory != Done
     AND status != Cancelled
     AND status != Released
     AND status != Backlog
   ```
2. For each ticket returned, verify the key starts with `PDE-`. If it does not,
   write a log entry "<KEY>: key does not start with PDE- — skipping" and skip it.
   This is a hard safety check; do not process non-PDE tickets under any circumstances.
3. **Cleanup pass:**
   - **Archive:** scan all `tickets/*/` directories. For any ticket folder whose
     key is **not** in the Jira results — done, cancelled, released, backlog-ed,
     or the `AI-Work` label was removed — archive it to `tickets/archive/<KEY>/`
     and write log "<KEY>: archived to tickets/archive/<KEY>", **unless its lock
     is currently held** (a session is genuinely still running — leave it alone
     rather than archiving out from under it).
   - **Purge:** delete any `tickets/archive/<KEY>/` folder older than **7 days**,
     using the folder's own modification time. Write log
     "<KEY>: purged archive (older than 7 days)".
     ```bash
     find tickets/archive/ -mindepth 1 -maxdepth 1 -type d -mtime +7 | while read dir; do
       echo "[cleanup] Purging old archive: $dir"
       rm -rf "$dir"
     done
     ```
4. For each ticket returned (PDE keys only), in order, execute the full **"Before acting on any ticket"**
   workflow below. The orchestrator is **stateless and re-runnable** — it fires off workers and exits.
   Do not block waiting for workers to finish. Move immediately to the next ticket.

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
here is now decided purely by whether a directory already exists for the
ticket (see "The two signals" above) — no artifact inspection needed at all.

The rewind check itself still happens, just one level down: see
`ticket-worker/SKILL.md`'s Startup section for how the worker validates
Jira's status against its own directory before routing, and self-corrects
if a human skipped a stage.

## Status → action table

The orchestrator delegates work in exactly four statuses. Everything else is
either a human gate or a terminal state — touch nothing.

| Status | Category | Action |
|---|---|---|
| To Do | **New session** | Ticket must already be assigned to the user running the orchestrator. If unassigned or assigned to someone else, skip with log "<KEY>: not assigned to current user — skipping". If assigned to current user, start a **new** session (`--name`). |
| In Discovery | **Resume** | Human moved ticket back from QA Review with feedback. Resume the existing session (`--resume`) — the worker reads the rejection comment and re-runs discovery. |
| In Progress | **Resume** | The human approved. Resume the existing session (`--resume`) to run implementation. |
| UAT Review | **Resume** | The PR was approved. Resume the existing session (`--resume`) to merge. |
| QA Review | **Human gate** | Do nothing. |
| In Review | **Human gate** | Do nothing — the orchestrator never dispatches for this status. A human addresses PR comments by moving the ticket back to `In Progress`; that's what actually triggers `ticket-worker` to run the review agent (see `ticket-worker/SKILL.md`'s Path: Implementation, second branch). |
| Backlog | **Ignored** | Do nothing. Excluded by JQL query — cannot appear, but documented for completeness. |
| Done | **Terminal** | Do nothing. |

**Rule:** `--name` is only used for `To Do`. Every other actionable status uses `--resume`.
The session is created once per ticket and carries all context across every stage.

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
- **Actually launching** (step 6 below): acquire the same lock the same
  way, but this time *keep holding it* through the launch and hand it off.

**Developer shortcut:** `claude --resume "<KEY>"` — the ticket key is both the
session's display name (`--name="<KEY>"` at launch) and its real `--resume`
handle. Verified directly: Claude Code resolves `--resume` by the name set via
`--name`, non-interactively, no UUID needed — as long as that name is
unambiguous. Names aren't unique, though: a second fresh launch reusing the
same `--name` creates a second, distinct session sharing it, and `--resume`
then hard-errors demanding a session ID to disambiguate. That's why a fresh
"To Do" launch always renames any existing same-named session out of the way
first (via a headless `/rename` prompt — see step 3b) rather than just
reusing the key blindly.

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

2. For actionable statuses (To Do, In Discovery, In Progress, UAT Review), if
   `tickets/<KEY>/` exists, check whether its lock is currently held (see
   "Worker lock" above — a non-blocking, non-consuming check).
   - Held: log "<KEY>: session already running — skipping" and stop.
   - Free, or the directory doesn't exist yet: proceed. No need to distinguish
     "never ran," "finished cleanly," or "crashed" — a free lock means safe to
     proceed either way.

3. **If Jira status is `To Do`:**

   a. Skip if the ticket is not assigned to the current user:
     ```
     if assignee_id != current_user_id:
         log "<KEY>: not assigned to current user — skipping"
         return
     ```

   b. Rename any existing claude session still named `<KEY>` out of the way,
     via a headless prompt — safe to run even if no such session exists (there's
     simply nothing to rename, which isn't an error worth acting on):
     ```bash
     claude --resume="<KEY>" -p "/rename <KEY>-archived-$(date +%Y-%m-%d)" \
       --permission-mode=bypassPermissions
     ```
     This has to happen *before* deleting the ticket directory below and
     *before* launching the fresh session in step 6, which will reuse the
     plain name `<KEY>` — without this, `--resume "<KEY>"` later would match
     both the old and new sessions and hard-error demanding a session ID.

   c. Delete the entire `tickets/<KEY>/` directory if it exists. This discards
     all prior working artifacts (notes, cloned code, etc.).

4. Create `tickets/<KEY>/` if it does not exist — the lock file has to live
   inside it.

5. Acquire `tickets/<KEY>/.worker.lock` — non-blocking, and this time *keep
   holding it* (see "Worker lock" above). If someone else grabbed it in the
   moment since step 2's check, log "<KEY>: lock held by another process at
   launch time — skipping this run" and stop; this is a race so unlikely
   it's not worth more than failing safe.

6. Launch the session (cwd is set directly on the subprocess — Claude Code's
   CLI has no `-C` equivalent, unlike Copilot's). `--name`/`--resume` both
   just use the plain ticket key — step 3b's rename is what keeps that
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
   if [ "$JIRA_STATUS" = "To Do" ]; then
    # Fresh session:
    SESSION_PID=$(cd "$TICKET_DIR" && nohup claude --name="<KEY>" \
      --permission-mode=bypassPermissions \
      --add-dir "$TICKET_DIR" \
      --add-dir "$REPOS_DIR" \
      -p "/ticket-worker Repos directory: $REPOS_DIR" \
      > "${AGENT_CHILD_LOG_DIR:-$TICKET_DIR}/<KEY>.log" 2>&1 & echo $!)
   else
    # Resume existing session (In Discovery, In Progress, UAT Review):
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
   - A fresh "To Do" restart's rename step (3b) already freed up the plain
     key name before this point, so `--name="<KEY>"` here can never collide.

   > **Production note:** Run the orchestrator inside a dedicated VM. If a session
   > executes destructive code, it only damages the VM — not the host machine.

7. Log "<KEY>: session launched (PID $SESSION_PID) — moving to next ticket"
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
- Decide whether to launch or resume, and do it — via the worker's lock file
  and whether a directory already exists, alone; never anything else in the
  ticket directory, which belongs entirely to the worker. Does not
  sanity-check Jira's status against what's actually been done — that's the
  worker's job now (see below), since it requires knowing worker-domain
  artifact filenames the orchestrator has no other reason to know.
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
