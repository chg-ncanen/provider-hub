# jexpress plugin (JiraExpressAI)

Plugin that orchestrates the full lifecycle of a PDE **AI-Work** Jira ticket — discovery,
implementation, PR review, and merge — driving Jira status transitions automatically and
stopping at each human review gate until a developer moves the ticket forward.

**Built for Claude Code.** Sub-agents are launched via detached `claude -p "/<skill>"` sessions
(`nohup`'d, `--permission-mode=bypassPermissions`). This plugin originally ran on GitHub Copilot
CLI; it's since been converted to Claude Code — see "Claude Code session identity" below for the
one behavioral difference that conversion introduced.

## Contents

- **`JIRA_EXPRESS_AI_CONTRACT.md`** — the single source of truth for what any specialist
  (`ticket-discovery`, `ticket-implementation`, `ticket-review`, `ticket-merge`) may
  do with the shared `$REPOS_DIR`: clone a missing repo, pull an existing one to
  latest, read code read-only, or create-if-missing its own git worktree to make
  changes in — plus the per-repo locking each of those requires. Every specialist's
  `SKILL.md` points here rather than restating the rules.
- **`JIRA_EXPRESS_AI_TRUST_CONTRACT.md`** — the single source of truth for treating
  Jira/PR/repo content as untrusted data rather than instructions: what the fixed
  capability ceiling is regardless of what that content asks for, and what to do
  (refuse, reply, log, and — for genuine manipulation attempts — escalate the
  comment's visibility) when it tries to reach outside that ceiling. Every
  specialist's `SKILL.md` points here, tailored to the untrusted surface it
  actually reads (Jira comments, PR comments, repo content, or PR/CI metadata).
- **`skills/ticket-orchestrator/`** — stateless, cron-safe dispatcher (`orchestrator.py`). Queries
  Jira fresh every run for `project = PDE AND labels = "AI-Work" AND statusCategory != Done`,
  decides per ticket whether to start a new session or resume an existing one based on Jira status
  + whether the ticket's `.worker.lock` is currently held, archives tickets no longer active in
  Jira, and launches a `ticket-worker` session per ticket. Makes no Jira writes at all, and owns
  nothing else inside a ticket's directory — no context file, no copied skill files, no state
  file, just the lock. Sanity-checking Jira's status against what's actually been done (rewind)
  is the worker's job, not the orchestrator's — see that skill's entry below. Run this on a
  schedule (cron every 5–15 minutes) from inside the project directory being automated — see that
  skill's `SKILL.md` for the exact invocation and required env vars.
- **`skills/ticket-worker/`** — the lifecycle/state-machine manager for one ticket. Its dispatch
  logic runs as `worker.py` (mirroring `ticket-orchestrator`'s own script-backed design), not AI
  inference — reads Jira status, sanity-checks it against what's actually in its own directory
  (rewinding Jira if a human skipped a stage — moved here from the orchestrator, since it requires
  knowing worker-domain artifact filenames), launches the matching sub-agent below, validates its
  output artifact, and transitions Jira. Kept as a genuine `claude -p` session rather than a direct
  script launch, unlike the orchestrator — see `ticket-worker/SKILL.md`'s "Why this stays a skill"
  for why. Doesn't do any discovery/implementation/review/merge work itself. No local state file —
  an earlier version tracked status/stage/timestamps in `.session.state`, but nothing ever read any
  of it back (not the orchestrator, not the worker's own routing), so it was removed for v1 rather
  than carried forward unverified. Jira's own status is the only state that matters.
- **`skills/ticket-discovery/`** — sub-agent: researches the ticket, writes `discovery.md`.
- **`skills/ticket-implementation/`** — sub-agent: implements `discovery.md`'s plan in an isolated
  git worktree, pushes the branch and opens the PR itself (it's the only one with direct
  knowledge of which repo/branch it actually touched), writes `implementation-notes.md` and
  `review-context.md`.
- **`skills/ticket-review/`** — sub-agent: replies to open PR review comments, pushes fixes if
  needed, writes `review-notes.md`.
- **`skills/ticket-merge/`** — sub-agent: checks CI/approval gates, merges the PR, monitors
  post-merge CI, writes `merge-notes.md`.

Every sub-agent signals completion with a sentinel file (`.discovery-agent-done`, etc.) and has a
`BLOCKED` path for anything requiring human judgment — the worker transitions the ticket to
`Blocked` and posts a comment rather than guessing. A sub-agent timeout or a crashed validation
check (not a deliberate `BLOCKED`, just something that didn't work) instead posts a `🤖 ⚠️`-marked
comment and leaves the ticket where it is for the first two occurrences, letting the next
orchestrator run retry automatically — only escalating to `Blocked` on the third consecutive
failure with no progress in between. All comments this plugin posts start with 🤖.

## Ticket status → action

| Jira status | Who acts |
|---|---|
| To Do | `ticket-orchestrator` starts a new session (only if assigned to the current user) |
| In Discovery | Resume → `ticket-discovery` |
| QA Review | Human gate — orchestrator does nothing (the worker re-posts the handoff comment only if resumed directly into this status) |
| In Progress | Resume → `ticket-implementation`, or `ticket-review` if implementation is already done |
| In Review | Human gate — orchestrator does nothing. A human addresses PR comments by moving the ticket back to `In Progress`, not by anything happening at `In Review` itself |
| UAT Review | Resume → `ticket-merge` |
| Done / Backlog / Cancelled / Released | Terminal — untouched |

If a human manually moves a ticket ahead of its actual completed stages (e.g. straight to
`In Progress` with no `discovery.md` present), the **worker** rewinds Jira to the earliest
incomplete stage rather than skipping work — see `ticket-worker/SKILL.md`'s Startup section.
See `ticket-orchestrator/SKILL.md` for the worker-lock mechanism this plugin dispatches on instead.

## Scope note: PDE-specific by design

The Atlassian cloud ID, the `project = PDE` JQL filter, and the Jira transition-ID table are all
hardcoded to the PDE project (not read from `userConfig`). This is intentional for now — it's
PDE's own ticket-automation tooling, just packaged as an installable plugin for distribution.
Generalizing it to other Jira projects would mean turning those into configuration, which hasn't
been done.

## Usage

`/jexpress:ticket-orchestrator` is the one meant for a human (or cron) to actually invoke. The
other five (`ticket-worker`, `ticket-discovery`, `ticket-implementation`, `ticket-review`,
`ticket-merge`) are all `user-invocable: true` too — not because they're meant to be run by hand,
but because they have to be: Claude Code's `-p "/<skill>"` headless invocation is blocked by
`user-invocable: false` exactly the same as the interactive `/` menu is (verified directly — there's
no way to tell "a script passed this via `-p`" apart from "a human typed it"), and the
orchestrator/worker launch every one of these via `-p`. There's no way to hide them from a human's
menu while keeping that working. Invoking one directly would mostly just fail confusingly anyway —
`ticket-worker` and its specialists expect to run inside a specific ticket directory (with
`review-context.md`, etc. already in place where a given stage needs it) and to receive their
repos-directory argument as text following the skill name in the prompt, not typed in by hand.

Specialist skills are invoked by their bare name (`/ticket-discovery`, not
`/jexpress:ticket-discovery`) — verified directly that an installed plugin's skills resolve by bare
name too, from any directory, as long as it's unambiguous. No skill files are copied into a
ticket's directory to make this work; the installed plugin is already globally reachable.

## Claude Code session identity

Like Copilot CLI, Claude Code resolves `--resume` by the name set via `--name` at launch, fully
non-interactively — verified directly against the real CLI, no session UUID needed. So the ticket
key itself is both the display name and the resume handle: `--name="<KEY>"` on first launch,
`--resume="<KEY>"` on every later one, exactly like the original Copilot design.

One real difference: Claude Code session names aren't unique. A second fresh launch reusing a
`--name` already in use creates a second, distinct session sharing that name, and `--resume` then
hard-errors demanding a session ID to disambiguate, instead of just picking one. So a fresh "To
Do" launch still needs the same fix Copilot's design used for the same reason — rename any
existing same-named session out of the way first — just done through Claude Code's own `/rename`
command (which works fine as a plain headless `-p` prompt, also verified directly) instead of
hand-editing session files.

## Orchestrator/worker decoupling

The orchestrator owns exactly two things inside a ticket's directory: the directory itself, and
`tickets/<KEY>/.worker.lock`. Nothing else — no context file, no copied skill files, no state file.

Liveness is answered by that lock (a plain OS `flock`), not by a stored PID plus a liveness check.
The lock is acquired non-blockingly by the orchestrator *before* launching, handed to the new
`claude` process across the launch itself (inherited, not re-acquired by the child), and released
automatically by the kernel the instant that process's file descriptors go away — clean exit,
uncaught error, `kill -9`, an OOM kill, or the machine losing power, all the same. A stored PID
can't make that guarantee, and can even be legitimately reused by an unrelated process after a
reboot; a held-or-not lock can't be fooled that way.

This also means the orchestrator never waits after launching to confirm a session actually started
— there's no corrective action it would take differently if it knew, since whatever caused a
launch to fail, the next scheduled run finds the lock free again and just retries. So it dispatches
every ticket for the run and exits immediately, no polling.

Everything else about a ticket — its own progress tracking, the repos-directory path (passed as
plain text following the skill name in the launch prompt, not a file), every real artifact — is
the worker's territory alone.

## Prerequisites

- **Claude Code CLI** (`claude`) on `PATH` — the orchestrator and worker launch sub-agent sessions
  through it (`claude -p "/<skill>" --permission-mode=bypassPermissions ...`, detached with
  `nohup`).
- Python 3 with the `requests` package installed on the machine running `orchestrator.py` and
  `worker.py` (not yet automated via a bootstrap hook the way `pde-mcp`'s venv is — install it
  yourself for now: `pip install requests`).
- `git` and `gh` (GitHub CLI, authenticated with push + PR access to the target repos) on `PATH`.
- `ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN` — prompted for on install via this plugin's
  `userConfig`; propagated to `orchestrator.py` and every sub-agent it spawns as
  `CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL`/`CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN` (normal OS env
  inheritance carries them into nested subprocesses automatically). If running the script directly
  outside a plugin-managed session, export `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN` yourself instead.
- A target project directory containing (or able to clone) the repos being worked on, plus a
  writable `tickets/` subdirectory — this is where the orchestrator is run *from* (see
  `ticket-orchestrator/SKILL.md`'s "Working directory" section).

## Tests

`orchestrator.py` and `worker.py` are both plain, deterministic Python — no LLM involved in the
logic itself — so they're covered by a real `unittest` suite under `tests/`: locks (including a
regression guard for a real concurrency bug an earlier version of `try_repo_lock()` had), cleanup/
archival, the rewind/sanity-check table, sub-agent dispatch, artifact parsing, and the 3-strikes
failure-escalation counting. Everything that touches Jira, `git`/`gh`, or the `claude` CLI is
mocked — the suite never makes a real network call or spawns a real subprocess.

```bash
cd plugins/jira-express-ai
python3 -m unittest discover -s tests -t . -q
```

The four specialist skills (`ticket-discovery`, `ticket-implementation`, `ticket-review`,
`ticket-merge`) are genuine LLM-driven `SKILL.md` prose, not scripts, so there's nothing to unit
test there — verifying those means an actual end-to-end run against a real ticket.

## Installing

```bash
claude plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
claude plugin install jexpress@provider-hub
```

Then start a new session before using it, same as any plugin install. This plugin can still be
listed in the marketplace for Copilot CLI, but installing it there won't get you a working
orchestrator — `orchestrator.py` shells out to the `claude` binary specifically.
