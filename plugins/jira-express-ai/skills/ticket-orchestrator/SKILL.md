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
     key is **not** in the Jira results (ticket is done, cancelled, or released) and
     whose `.session.state` has `status: done`, move it to `tickets/archive/<KEY>/`
     and write log "<KEY>: archived to tickets/archive/<KEY>".
   - **Purge:** delete any `tickets/archive/<KEY>/` folder older than **7 days**.
     Use the folder's modification time or the `updated_at` field in `.session.state`.
     Write log "<KEY>: purged archive (older than 7 days)".
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
| State file | Is a session running, and did it confirm pickup? | `tickets/<KEY>/.session.state` |

Jira is the source of truth for status. Never infer readiness from anything
cached locally — check Jira fresh every run. The query already guarantees
the `AI-Work` label is present; no secondary label check is needed.

## Status rewind (human may set any status)

Humans can manually move a ticket to any Jira status. Before acting, the
orchestrator checks whether the ticket's local artifacts confirm all prerequisite
stages are done. If not, it **rewrites the effective status to the earliest
incomplete stage** and transitions Jira accordingly.

| Stage completed | Earliest incomplete | Deliverable checked |
|---|---|---|
| None | In Discovery | `discovery.md` |
| Discovery only | In Progress | `implementation-notes.md` |
| Discovery + Implementation | UAT Review | `review-notes.md` |

**Examples:**
- Ticket manually moved to `In Progress` before any work → rewind to `In Discovery`,
  transition Jira → `In Discovery`, treat as `In Discovery` this run.
- Ticket manually moved to `UAT Review` with only discovery done → rewind to
  `In Progress`, transition Jira → `In Progress`.
- No ticket directory exists at all → treat as fresh `In Discovery` (like `To Do`
  but session name already exists or didn't exist yet).

The rewind is logged: `"<KEY>: Jira says 'X' but only N/M prerequisite stage(s) are done — rewinding to 'Y'"`

## Status → action table

The orchestrator delegates work in exactly three statuses. Everything else is
either a human gate or a terminal state — touch nothing.

| Status | Category | Action |
|---|---|---|
| To Do | **New session** | Ticket must already be assigned to the user running the orchestrator. If unassigned or assigned to someone else, skip with log "<KEY>: not assigned to current user — skipping". If assigned to current user, start a **new** session (`--name`). |
| In Discovery | **Resume** | Human moved ticket back from QA Review with feedback. Resume the existing session (`--resume`) — the worker reads the rejection comment and re-runs discovery. |
| In Progress | **Resume** | The human approved. Resume the existing session (`--resume`) to run implementation. |
| UAT Review | **Resume** | The PR was approved. Resume the existing session (`--resume`) to merge. |
| QA Review | **Human gate** | Do nothing. |
| In Review | **Resume** | The PR has comments to address. Resume the existing session (`--resume`) to run the review agent. |
| Backlog | **Ignored** | Do nothing. Excluded by JQL query — cannot appear, but documented for completeness. |
| Done | **Terminal** | Do nothing. |

**Rule:** `--name` is only used for `To Do`. Every other actionable status uses `--resume`.
The session is created once per ticket and carries all context across every stage.

Any status not in this table: do nothing, and do not guess.

## State file schema

Each ticket folder contains `.session.state` (JSON). Both the orchestrator
and the session write to it — but for different fields and at different times.

```json
{
  "status": "<running|waiting|done|crashed>",
  "pid": 12345,
  "started_at": "2026-07-13T10:00:00Z",
  "picked_up_at": "2026-07-13T10:00:05Z",
  "updated_at": "2026-07-13T10:04:22Z",
  "stage": "discovery"
}
```

| Field | Written by | Meaning |
|---|---|---|
| `status` | Both (orchestrator resets/marks crashed; session updates during work) | Current lifecycle state |
| `pid` | Orchestrator after launch | Process ID of the worker — used to detect crashes |
| `started_at` | Session on start | When the session began |
| `picked_up_at` | Session, first update | Confirmation that the session loaded and is working |
| `updated_at` | Session, each update | Heartbeat — last time the session wrote anything |
| `stage` | Session | Which stage is active (discovery, implementation, merge, etc.) |

`picked_up_at` is the orchestrator's confirmation signal. If it is absent
after a reasonable wait, the session never started cleanly.

**Developer shortcut:** `copilot --resume="<KEY>"` — the session name is always
the ticket key. Old sessions are renamed to `<KEY>-archived-<date>` on To Do
re-runs, so the name is always unambiguous.

## Jira MCP

All Jira operations use the **Jira MCP tool** provided by the workspace.

- Query tickets: `jira_search` with JQL
- Read ticket: `jira_get_issue`
- Transition ticket: `jira_transition_issue`
- Assign ticket: `jira_assign_issue`

## Before acting on any ticket, in order

1. Read the current status from Jira. Look it up in the Status → action table.
   If the row says "do nothing," write a log entry "<KEY>: <Status> — skipping"
   and stop processing this ticket. **Do not read the state file — Jira status
   alone is sufficient to skip.**

2. For actionable statuses (To Do, In Discovery, In Progress, UAT Review):
   read `.session.state` from `tickets/<KEY>/` if it exists.
   - Does not exist: proceed.
   - `status: running`, PID alive: log "<KEY>: session running (PID <pid>) — skipping" and stop.
   - `status: running`, PID dead: log "<KEY>: crashed session detected (PID <pid>) — proceeding."
    Write `status: crashed` to `.session.state` and proceed.
   - `status: waiting`: human moved the ticket to an actionable status — proceed.
   - `status: done` or `status: crashed`: proceed.

3. **If Jira status is `To Do`:**

   a. Skip if the ticket is not assigned to the current user:
     ```
     if assignee_id != current_user_id:
         log "<KEY>: not assigned to current user — skipping"
         return
     ```

   b. Rename the old session (if any) to free the name `<KEY>` for the new session.
     Scan all `~/.copilot/session-state/*/workspace.yaml` files — no state file
     check needed, always scan:
     ```python
     import re, os
     from datetime import date
     state_root = os.path.expanduser("~/.copilot/session-state")
     for sid in os.listdir(state_root):
         wy = os.path.join(state_root, sid, "workspace.yaml")
         if not os.path.exists(wy): continue
         content = open(wy).read()
         if re.search(rf'^name: {re.escape("<KEY>")}$', content, re.MULTILINE):
             archive_name = f"<KEY>-archived-{date.today()}"
             content = re.sub(r'^name: .*$', f'name: {archive_name}', content, flags=re.MULTILINE)
             content = re.sub(r'^user_named: .*$', 'user_named: false', content, flags=re.MULTILINE)
             open(wy, 'w').write(content)
             break
     ```

   c. Delete the entire `tickets/<KEY>/` directory if it exists. This discards all
     prior working artifacts (notes, cloned code, etc.).

4. Create `tickets/<KEY>/` if it does not exist.

5. Write `.context.md` into `tickets/<KEY>/` with the repos directory path (if available).
   The repos directory is `$(pwd)` (set as `$REPOS_DIR` in step 7):
   ```markdown
   # Session Context

   **Repos directory:** <absolute path to cwd>

   This directory contains local clones organized by repository name.
   Use for code exploration. Falls back to GitHub search if unavailable.
   ```

6. Copy the worker skill into the ticket directory so the worker discovers it
   from its own working directory:
   ```bash
   mkdir -p "tickets/<KEY>/.agents/skills/ticket-worker"
   cp "$CLAUDE_PLUGIN_ROOT/skills/ticket-worker/SKILL.md" \
    "tickets/<KEY>/.agents/skills/ticket-worker/SKILL.md"
   ```

7. Set path variables and write `.session.state` before launch:
   ```bash
   TICKET_DIR="$(pwd)/tickets/<KEY>"
   REPOS_DIR="$(pwd)"
   ```
   - **To Do (fresh start):** full reset — all fields null:
    ```json
    {
      "status": "running",
      "pid": null,
      "started_at": null,
      "picked_up_at": null,
      "updated_at": null,
      "stage": null
    }
    ```
   - **Resume (In Discovery, In Progress, UAT Review):** partial update only —
    reset `picked_up_at` to null and set `status: running`. Preserve all other
    fields (`started_at`, `stage`, etc.) the worker previously wrote:
    ```python
    import json, os
    f = f"{TICKET_DIR}/.session.state"
    s = json.load(open(f)) if os.path.exists(f) else {}
    s["status"] = "running"
    s["picked_up_at"] = None
    json.dump(s, open(f, "w"), indent=2)
    ```

8. Launch the session using `-C` to set the working directory:
   ```bash
   if [ "$JIRA_STATUS" = "To Do" ]; then
    # Fresh session — name was freed by rename in step 3b:
    nohup copilot -C "$TICKET_DIR" --name="<KEY>" \
      --allow-all-tools \
      --allow-all-urls \
      --add-dir "$TICKET_DIR" \
      --add-dir "$REPOS_DIR" \
      -p "/ticket-worker" \
      > "$TICKET_DIR/session.log" 2>&1 &
   else
    # Resume existing session (In Discovery, In Progress, UAT Review):
    nohup copilot -C "$TICKET_DIR" --resume="<KEY>" \
      --allow-all-tools \
      --allow-all-urls \
      --add-dir "$TICKET_DIR" \
      --add-dir "$REPOS_DIR" \
      -p "/ticket-worker" \
      > "$TICKET_DIR/session.log" 2>&1 &
   fi
   SESSION_PID=$!
   ```
   - `-C "$TICKET_DIR"` sets the worker's cwd without requiring `cd`/`cd -`.
   - `--allow-all-tools` auto-approves tool calls (required for non-interactive mode).
   - `--allow-all-urls` allows Jira and GitHub API calls without prompting.
   - `--add-dir` restricts file access to the ticket and repos directories only.
   - Do NOT use `--allow-all-paths` or `--yolo`.
   - `-p "/ticket-worker"` invokes the worker skill as the initial prompt.
   - The Atlassian `cloudId` is `e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2`.
   - **Session name = ticket key, always.** Developer shortcut: `copilot --resume="<KEY>"`.
   - Archived sessions are named `<KEY>-archived-<date>` and retain full history.

   > **Production note:** Run the orchestrator inside a dedicated VM. If a session
   > executes destructive code, it only damages the VM — not the host machine.

9. Write the real PID into `.session.state`:
   ```bash
   python3 -c "
   import json
   f = '$TICKET_DIR/.session.state'
   s = json.load(open(f))
   s['pid'] = $SESSION_PID
   json.dump(s, open(f,'w'), indent=2)
   "
   ```

10. Poll `tickets/<KEY>/.session.state` every 2 seconds until `picked_up_at` is
    present, up to 30 seconds. If absent after 30 seconds, log
    "<KEY>: session did not pick up within 30s — flagged as failed to start" and stop.

11. Log "<KEY>: session launched (PID $SESSION_PID) — moving to next ticket."
    Do not block. Move immediately to the next ticket.

## Orchestrator responsibilities

- Query Jira fresh each run.
- Archive `tickets/*/` folders for tickets no longer active in Jira (status: done in state file).
- Read ticket status and state file.
- Detect and log crashes (dead PID).
- Create ticket folder and write `.context.md`.
- Launch worker session via `copilot` CLI (`nohup ... &`).
- Wait up to 30s for `picked_up_at` confirmation, then move on.
- **Exit after processing all tickets** — no long-running blocking.
- Designed to be run on a schedule (e.g., cron every 5–15 minutes).

## Session responsibilities

- Write and maintain `.session.state` (all fields, including `picked_up_at`).
- Transition Jira tickets as work progresses or fails.
- Decide what "success" and "failure" mean for each stage.
- Set `status: waiting` when handing off to a human gate.
- Clean exit: set `status: done` before exiting normally.
