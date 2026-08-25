# Headless execution contract

Every skill in this plugin that actually does work — `ticket-worker`,
`ticket-discovery`, `ticket-implementation`, `ticket-review`, `ticket-merge` —
runs non-interactively, launched via `claude -p "/<skill> ..."` (the
orchestrator launches `ticket-worker` this way; `ticket-worker` launches each
specialist the same way). This file is the single source of truth for what
that means for any command you run that might take a while. If a skill's own
`SKILL.md` and this file ever disagree, this file is right.

(`ticket-orchestrator` is exempt: it never blocks on anything it launches —
it fires sessions with `nohup ... &` and exits immediately, so it never hits
what this file describes.)

## Why this matters

A `-p` invocation has no later turn. It runs until it stops calling tools and
produces a final text response, and then the process exits — for good. This
is different from an interactive session (a human's terminal, or an agent
kept alive across many turns), where a background task can raise a
notification minutes or hours later and something is still there to receive
it.

Several mechanisms your tools might offer look like they solve "wait for a
long-running command," but all assume that later turn exists:

- **Bash auto-backgrounding.** If a command outlives the tool's default
  timeout (commonly ~120s), it gets moved to a background task with a
  message like "you will be notified when it completes." In a `-p` session
  that notification has nowhere to land — you will have already exited by
  the time it would arrive.
- **`Monitor`.** Same problem: it streams events to future turns. There are
  no future turns here.
- **A `Skill` invocation that forks to a background task** (e.g. `code-review`
  when it runs as a forked execution rather than inline) — identical problem:
  it returns a task handle and reports back via the same kind of later-turn
  notification, which never arrives in a `-p` session.

Treat all of these as unavailable to you for actually waiting on completion.
If a Bash call — or a Skill invocation — you made gets auto-backgrounded,
that's not a failure and not a signal to wrap up and wait — it just means the
task is still running as something you now have to watch yourself, in-line,
before you do anything else.

## What to do instead

Poll the task explicitly, in a loop, with `TaskOutput(task_id, block=true,
timeout=<up to 600000>)`:

1. Take the `task_id` the auto-backgrounded Bash call — or forked Skill
   invocation — handed you.
2. Call `TaskOutput` with `block=true` and as large a `timeout` as the tool
   allows (up to 600000ms / 10 minutes).
3. If it comes back still running, call it again immediately on the same
   `task_id`. Repeat for as many rounds as it takes.
4. Only once `TaskOutput` reports the process has actually exited should you
   read its output and move on to whatever comes next.

**Do not "fix" this by raising the Bash timeout instead.** Some of what this
plugin waits on — a slow CI run, a merge that needs retries, a specialist
that needs a second pass, a large `npm ci` — can legitimately take longer
than any single fixed value you'd pick. The poll loop, not the timeout, is
what has to be unbounded: keep calling `TaskOutput` until the task is
actually done, however many rounds that takes.
