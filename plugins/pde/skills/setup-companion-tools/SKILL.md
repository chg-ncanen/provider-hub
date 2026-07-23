---
name: setup-companion-tools
description: Interactively install optional companion MCPs/plugins for PDE work (Grafana, LogRocket, Atlassian, Salesforce prod/UAT, LaunchDarkly) that aren't bundled in the pde plugin. Use when the user asks to set up, connect, install, or configure additional PDE tools/MCPs, or asks what companion tools are available.
user-invocable: true
---

# Setup Companion Tools

A guided wizard for optionally installing MCP servers/plugins commonly used alongside PDE
tooling, that aren't bundled in the `pde` plugin itself: Grafana (`gcx`), LogRocket, Atlassian
(Jira/Confluence), Salesforce prod and UAT (prod needed by `resolve-duplicate-contact-alerts`;
UAT is just commonly useful alongside it), and LaunchDarkly. None of these are actually called by
any code in the `pde` plugin (verified — only `salesforce-prod` is a genuine dependency, of
`resolve-duplicate-contact-alerts` specifically); the rest are just commonly useful alongside it.
Nothing here runs automatically — only when a developer explicitly invokes this skill, and only
for whichever service(s) they pick.

## Before you start

Figure out which CLI you're actually running under (a machine can have both installed) — check
for `CLAUDECODE`/`CLAUDE_CODE_SESSION_ID` in the environment for Claude Code, or otherwise confirm
with the user directly if genuinely ambiguous. Pass `--cli claude` or `--cli copilot` to every
`manage_companions.py` call below — the two CLIs use different commands and config locations.

## The wizard loop

Repeat this loop until the user says they're done. Every pass through it starts from a fresh
`status` call — **never** rely on what a prior turn in this conversation said was true. That's
what makes "resume" work for free (see "Resuming" below): whether the user just ran a command in
another terminal, backgrounded this session and came back, or closed Claude entirely and returned
later in a brand new conversation, re-running `status` picks up the real current state either way.

### 1. Render the status tree

Run `python3 manage_companions.py status --cli <claude|copilot>` (from this skill's own
directory) and render it as an indented tree, one line per service, dependencies indented
underneath — this should read like Claude Code's own `/mcp` list, not a wall of prose. For each
service line: `[x]`/`[ ]` for `installed`, then `installed`/`not installed`, then if installed and
`ready` is `false` append `— not ready`, or if `note` is present append `— <note>`. For each
dependency line, same `[x]`/`[ ]` shape keyed off that dependency's own `installed`, followed by
its `detail` string verbatim. Example rendering for a mixed-state machine:

```
[ ] Grafana (gcx)             not installed
      gcx CLI                 [ ] not found on PATH
[ ] LogRocket                 not installed
[x] Atlassian                 installed — connects via OAuth on first use
[x] Salesforce prod           installed — not ready
      sf CLI                  [x] installed, not logged into 'prod'
[ ] Salesforce UAT            not installed
[ ] LaunchDarkly              not installed
```

### 2. Ask what to work on

Offer, as a multiple-choice prompt: install one or more not-yet-installed services, fix an
outstanding dependency/readiness gap for something already installed, re-check status (in case
they just finished something out-of-band), or stop. Let them pick one or more at a time; handle
the pick(s), then loop back to step 1 so the tree reflects whatever just changed.

### 3. Handle an install pick

If `status` already shows it installed, say so and skip straight to dependency/readiness handling
below — nothing to install.

Otherwise run `python3 manage_companions.py install <service> --cli <claude|copilot>`.

**`install` will refuse to run for a service with an unmet blocking dependency** (currently: `sf`
for salesforce-prod/uat, `gcx` for grafana — both work the same way; see step 4). Neither the
Grafana plugin's MCP server nor salesforce-prod's is a hosted/HTTP server — both shell out to a
local CLI directly, so registering either before its CLI is installed and authenticated would
leave the same kind of broken-looking, half-working install sitting there. The result comes back
as `{"success": false, "blocked": true, "unmet_dependencies": [...], ...}`
instead of actually registering anything. When you see `blocked: true`, don't treat it as a
generic failure — go straight to step 4 for each entry in `unmet_dependencies`, and once the user
tells you they've fixed it, **retry the same `install` call** (don't just re-check status and
stop) — that's what actually registers the MCP/plugin once the dependency clears.

Otherwise relay the result (`success`, what got installed, or the `error` if it failed for some
other reason).

**Installed is not the same as ready to use.** For every successful install, also do whatever's
needed to actually finish setup:

- If the result has a non-null `post_install` field (grafana, logrocket, atlassian,
  launch-darkly): relay it verbatim, and be explicit that a **session restart** is required first
  — the newly installed server/plugin isn't connected in the *current* session. Tell the user
  plainly: "restart your session now; when you're back, just ask me to check companion tools
  status again (or re-run this skill) and I'll pick up exactly where we left off."
- For any service with unmet dependencies (see step 4), handle those too before considering the
  service done.

### 4. Handle a dependency/readiness gap

This is the part that most needs to be unmistakable, because it's where a human has to stop and
do something outside the conversation.

- **A dependency isn't installed and might need root** (currently: `sf` for salesforce-prod/uat,
  `gcx` for grafana). Run `python3 manage_companions.py dep-guidance <dependency>` (e.g.
  `dep-guidance sf` or `dep-guidance gcx`) — this actually tests the machine, it doesn't guess.
  Then:
  - If `root_required` is `null`: relay the `prerequisite` field (e.g. install Node.js first for
    `sf`, or Go/git for `gcx` on Windows) — there's nothing to run yet.
  - If `root_required` is `true`: this is a **hard stop for you** — you have no way to supply a
    root/admin password interactively even if you tried. Render it as a numbered, impossible-to-
    skim-past callout, e.g.:
    ```
    ACTION NEEDED — Step 1: install the sf CLI
    I can't do this myself — it needs root/admin access on this machine.

        sudo npm install -g @salesforce/cli

    Run that yourself, then come back and tell me to continue — I'll re-check before moving on,
    not just take your word for it.
    ```
  - If `root_required` is `false`: same callout shape, but phrased as an offer, since you *could*
    run it: "This doesn't need root on your machine — want me to run `<command>` for you, or would
    you rather run it yourself?" Only run it after they say yes. `gcx`'s install script normally
    lands here (it installs to `~/.local/bin`, never root) — don't assume it needs the same
    root/no-root ambiguity `sf` does, `dep-guidance` already resolved that.
- **A dependency is installed but not authenticated** — `sf` CLI present but the relevant alias
  isn't logged in, or `gcx` CLI present but `gcx config check` fails: both are always something
  only the human can do (interactive browser login) — render it the same numbered-callout way,
  e.g. "ACTION NEEDED — Step 2: log into Salesforce" with the exact command (`sf org login web
  --alias prod`/`--alias uat`, or `gcx login` for Grafana), and the same "come back and tell me to
  continue, I'll verify" close.
- **OAuth-based services with no local dependency** (logrocket, atlassian, launch-darkly): after
  a restart, you can proactively call one of that service's tools right away (e.g. "list my
  feature flags") to trigger the login immediately instead of leaving the user to stumble into it
  later — ask first, since it'll pop an auth prompt.

Never bundle one of these action-needed moments into a paragraph of other text — always give it
its own callout so it can't be missed.

## Resuming

Because every pass through the wizard loop starts from a fresh `status`/`dep-guidance` call
against real machine/CLI state (not from what this conversation remembers), resuming after any
kind of break works the same way — you don't need to ask the user what they did or track it
yourself:

- **They ran the command in another terminal while this session stayed open** (or backgrounded
  this session and came back): when they say "done" or "continue", don't take it at face value —
  re-run `status` (or the specific `dep-guidance`/alias check) and only report success once the
  check actually confirms it. If it's still not ready, say so plainly and suggest the concrete
  next diagnostic step (e.g. re-run the command and check its output, confirm they're on the
  right terminal/shell where the install landed).
- **They closed Claude entirely and came back later** — possibly in a brand new conversation with
  none of this history: just re-invoke the skill from the top. The status tree reflects whatever
  changed while they were away; there's no prior state to recover because none of it lived in
  conversation memory in the first place.

## Available services

- **Grafana (`gcx`)** — 16+ skills, a `grafana-debugger` agent, dashboard/alert/SLO management.
  Same install mechanism on both CLIs. Its MCP server shells out to the local `gcx` CLI directly
  (not a hosted HTTP MCP), so it needs the `gcx` CLI installed *and* authenticated to a stack
  first — `install` refuses to register it otherwise, exactly like Salesforce below.
- **LogRocket** — session replay, metrics, issue search. Same install mechanism on both CLIs.
- **Atlassian** — Jira/Confluence search, issue creation, sprint management.
  - Claude Code: the full official plugin (6 skills), via the pre-registered
    `claude-plugins-official` marketplace.
  - Copilot CLI: that marketplace file fails to parse there (a real schema incompatibility on the
    `source` field of several entries, not a typo) — `install` falls back to registering the bare
    `chg-atlassian` MCP endpoint instead. Tools only, no bundled skills, until that gets fixed
    upstream.
- **Salesforce prod** — SOQL queries against the prod org. Needs the `sf` CLI authenticated to the
  `prod` alias (see step 4 above) — the skill that actually uses this is
  `resolve-duplicate-contact-alerts`. **`install` refuses to register this MCP until that's
  true** — there's no point registering an entry that can't work yet.
- **Salesforce UAT** — SOQL queries against the UAT org. Needs the `sf` CLI authenticated to the
  `uat` alias (see step 4 above); `install` is gated the same way. Not used by any skill in this
  plugin — just handy alongside it.
- **LaunchDarkly** — feature flag management. Remote MCP, authenticates via an interactive OAuth
  prompt the first time it connects — no static credentials to configure. Same install mechanism
  on both CLIs.
