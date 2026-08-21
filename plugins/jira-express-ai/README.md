# jexpress plugin (JiraExpressAI)

Plugin that orchestrates the full lifecycle of a PDE **AI-Work** Jira ticket — discovery,
implementation, PR review, and merge — driving Jira status transitions automatically and
stopping at each human review gate until a developer moves the ticket forward.

**Currently built for GitHub Copilot CLI specifically** (sub-agents are launched via
`copilot -C ... --resume=... -p "/<skill>"`, detached with `nohup`). Standardizing this on
Claude Code is a planned follow-up, not yet done — installing via Claude Code will register the
skills, but `ticket-orchestrator` won't be able to launch sub-agent sessions until that migration
happens.

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
false` on Claude Code: they're launched by the orchestrator/worker as detached sub-agent sessions
against a specific ticket directory's own copied `SKILL.md` and would fail confusingly if invoked
directly, without that ticket directory's `.context.md` / `.session.state` / `review-context.md`
already in place. (Copilot CLI doesn't recognize `user-invocable` — until this plugin's sub-agent
launching is migrated off Copilot CLI, that flag has no effect there.)

## Prerequisites

- **GitHub Copilot CLI** (`copilot`) — the orchestrator and worker launch sub-agent sessions
  through it; this doesn't yet work under Claude Code.
- Python 3 with the `requests` package installed on the machine running `orchestrator.py` (not yet
  automated via a bootstrap hook the way `pde-mcp`'s venv is — install it yourself for now:
  `pip install requests`).
- `git` and `gh` (GitHub CLI, authenticated with push + PR access to the target repos) on `PATH`.
- `ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN` — prompted for on install via this plugin's
  `userConfig` under Claude Code; under Copilot CLI (no `userConfig` support) export them yourself
  in the environment the orchestrator and sub-agent sessions run in.
- A target project directory containing (or able to clone) the repos being worked on, plus a
  writable `tickets/` subdirectory — this is where the orchestrator is run *from* (see
  `ticket-orchestrator/SKILL.md`'s "Working directory" section).

## Installing

```bash
# Claude Code
claude plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
claude plugin install jexpress@provider-hub

# Copilot CLI
copilot plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
copilot plugin install jexpress@provider-hub
```

Then start a new session before using it, same as any plugin install.
