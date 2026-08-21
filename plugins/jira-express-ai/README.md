# jexpress plugin (JiraExpressAI)

Plugin that orchestrates the full lifecycle of a PDE **AI-Work** Jira ticket — discovery,
implementation, PR review, and merge — driving Jira status transitions automatically and
stopping at each human review gate until a developer moves the ticket forward.

**Built for Claude Code.** Sub-agents are launched via detached `claude -p "/<skill>"` sessions
(`nohup`'d, `--permission-mode=bypassPermissions`). This plugin originally ran on GitHub Copilot
CLI; it's since been converted to Claude Code — see "Claude Code session identity" below for the
one behavioral difference that conversion introduced.

## Contents

- **`skills/ticket-orchestrator/`** — stateless, cron-safe dispatcher (`orchestrator.py`). Queries
  Jira fresh every run for `project = PDE AND labels = "AI-Work" AND statusCategory != Done`,
  decides per ticket whether to start a new session or resume an existing one based on Jira status
  + the ticket's local `.session.state`, detects crashed sessions, archives completed tickets, and
  launches a `ticket-worker` session per ticket. Run this on a schedule (cron every 5–15 minutes)
  from inside the project directory being automated — see that skill's `SKILL.md` for the exact
  invocation and required env vars.
- **`skills/ticket-worker/`** — the lifecycle/state-machine manager for one ticket. Doesn't do any
  discovery/implementation/review/merge work itself; reads Jira status, launches the matching
  sub-agent below, validates its output artifact, transitions Jira, and updates `.session.state`.
- **`skills/ticket-discovery/`** — sub-agent: researches the ticket, writes `discovery.md`.
- **`skills/ticket-implementation/`** — sub-agent: implements `discovery.md`'s plan in an isolated
  git worktree, writes `implementation-notes.md`.
- **`skills/ticket-review/`** — sub-agent: replies to open PR review comments, pushes fixes if
  needed, writes `review-notes.md`.
- **`skills/ticket-merge/`** — sub-agent: checks CI/approval gates, merges the PR, monitors
  post-merge CI, writes `merge-notes.md`.

Every sub-agent signals completion with a sentinel file (`.discovery-agent-done`, etc.) and has a
`BLOCKED` path for anything requiring human judgment — the worker transitions the ticket to
`Blocked` and posts a comment rather than guessing.

## Ticket status → action

| Jira status | Who acts |
|---|---|
| To Do | `ticket-orchestrator` starts a new session (only if assigned to the current user) |
| In Discovery | Resume → `ticket-discovery` |
| QA Review | Human gate — orchestrator does nothing |
| In Progress | Resume → `ticket-implementation`, or `ticket-review` if implementation is already done |
| In Review | Human gate — orchestrator re-posts the review comment and waits |
| UAT Review | Resume → `ticket-merge` |
| Done / Backlog / Cancelled / Released | Terminal — untouched |

If a human manually moves a ticket ahead of its actual completed stages (e.g. straight to
`In Progress` with no `discovery.md` present), the orchestrator rewinds Jira to the earliest
incomplete stage rather than skipping work. See `ticket-orchestrator/SKILL.md` for the full
state-file schema, transition-ID table, and rewind logic.

## Scope note: PDE-specific by design

The Atlassian cloud ID, the `project = PDE` JQL filter, and the Jira transition-ID table are all
hardcoded to the PDE project (not read from `userConfig`). This is intentional for now — it's
PDE's own ticket-automation tooling, just packaged as an installable plugin for distribution.
Generalizing it to other Jira projects would mean turning those into configuration, which hasn't
been done.

## Usage

Only `/jexpress:ticket-orchestrator` is meant for a human (or cron) to invoke directly — it's the
only skill in this plugin with `user-invocable: true`. The other five (`ticket-worker`,
`ticket-discovery`, `ticket-implementation`, `ticket-review`, `ticket-merge`) are `user-invocable:
false`: they're launched by the orchestrator/worker as detached sub-agent sessions against a
specific ticket directory's own copied `SKILL.md` and would fail confusingly if invoked directly,
without that ticket directory's `.context.md` / `.session.state` / `review-context.md` already in
place.

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

## Prerequisites

- **Claude Code CLI** (`claude`) on `PATH` — the orchestrator and worker launch sub-agent sessions
  through it (`claude -p "/<skill>" --permission-mode=bypassPermissions ...`, detached with
  `nohup`).
- Python 3 with the `requests` package installed on the machine running `orchestrator.py` (not yet
  automated via a bootstrap hook the way `pde-mcp`'s venv is — install it yourself for now:
  `pip install requests`).
- `git` and `gh` (GitHub CLI, authenticated with push + PR access to the target repos) on `PATH`.
- `ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN` — prompted for on install via this plugin's
  `userConfig`; propagated to `orchestrator.py` and every sub-agent it spawns as
  `CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL`/`CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN` (normal OS env
  inheritance carries them into nested subprocesses automatically). If running the script directly
  outside a plugin-managed session, export `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN` yourself instead.
- A target project directory containing (or able to clone) the repos being worked on, plus a
  writable `tickets/` subdirectory — this is where the orchestrator is run *from* (see
  `ticket-orchestrator/SKILL.md`'s "Working directory" section).

## Installing

```bash
claude plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
claude plugin install jexpress@provider-hub
```

Then start a new session before using it, same as any plugin install. This plugin can still be
listed in the marketplace for Copilot CLI, but installing it there won't get you a working
orchestrator — `orchestrator.py` shells out to the `claude` binary specifically.
