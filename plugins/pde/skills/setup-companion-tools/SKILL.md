---
name: setup-companion-tools
description: Interactively install optional companion MCPs/plugins for PDE work (Atlassian, Grafana, LaunchDarkly, LogRocket, Salesforce prod/UAT) that aren't bundled in the pde plugin. Use when the user asks to set up, connect, install, or configure additional PDE tools/MCPs, or asks what companion tools are available.
user-invocable: true
---

# Setup Companion Tools

A guided wizard for optionally installing MCP servers/plugins commonly used alongside PDE
tooling, that aren't bundled in the `pde` plugin itself: Atlassian (Jira/Confluence), Grafana
(`gcx`), LaunchDarkly, LogRocket, Salesforce prod, and Salesforce UAT. None of these are called
automatically by any code in the `pde` plugin — nothing here runs on its own, only when a
developer explicitly invokes this skill, and only for whichever service(s) they pick. Whether a
particular downstream skill needs one of these installed is that skill's own concern to check,
not this one's.

## Before you start

Figure out which CLI you're actually running under (a machine can have both installed) — check
for `CLAUDECODE`/`CLAUDE_CODE_SESSION_ID` in the environment for Claude Code, or otherwise confirm
with the user directly if genuinely ambiguous. Pass `--cli claude` or `--cli copilot` to every
`manage_companions.py` call below — the two CLIs use different commands and config locations.

## The wizard loop

Repeat this loop until the user says they're done. Each iteration runs `status` exactly once and
asks exactly one question, then **stops and waits for the user's actual reply** — never show
status or ask what to work on twice in a row without new input in between, and never re-run
`status` again until either the user has responded, or they've picked an action that changes state
(an install, or coming back after fixing a dependency). Every fresh iteration starts from a new
`status` call rather than trusting what a prior turn in this conversation said was true — that's
what makes "resume" work for free (see "Resuming" below): whether the user just ran a command in
another terminal, backgrounded this session and came back, or closed Claude entirely and returned
later in a brand new conversation, re-running `status` picks up the real current state either way.

### 1. Show a numbered status table, then ask which one

Run `python3 <this skill's own directory>/manage_companions.py status --cli <claude|copilot>`
**using its full path — do not `cd` into the skill's directory first.** The script shells out to
`claude plugin list --json`, which reports a project-scoped plugin's `enabled`/`mcpServers` fields
relative to the caller's cwd (`enabled: false` and no `mcpServers` when cwd doesn't match the
project it was installed for) — `cd`-ing away from the user's actual project before running this
would make `pde_mcp_status()` misreport `pde` as not found even when it's installed and enabled.
Render the result as **all six companion services, plus `pde_mcp` itself**, as one markdown
table — a plain numbered pick, not an interactive tool prompt. (An earlier version of this skill
used `AskUserQuestion`, but that tool caps at 4 options, which meant 2 of the 6 services always
had to be demoted to "type the name yourself" — worse than just listing all 6 up front.) A table
is also what actually gets read; a plain status paragraph followed by something else blends in
and gets skipped.

One row per service, alphabetical, numbered so the user can reply with just a digit. Name each
one with "MCP" in it (e.g. "Atlassian MCP") so it's clear these are MCP servers being installed.
**Add one more row above all of them, unnumbered, for `pde_mcp`** — the core MCP server bundled
with the PDE Ops Tools plugin (`pde`) itself, not something this wizard installs (that's the
`SessionStart` hook's job). It's still worth surfacing here since every companion tool sits
alongside it, and its
`ready`/`detail` fields come from `status` the same way: use `—` instead of a number in that row so
it's visibly not a pickable option, and if the user replies with that dash or its name anyway,
just explain it isn't installable here rather than trying to do anything with it.

| # | Service | Status | Description |
|---|---|---|---|
| — | pde-mcp | Ready | JSM alert management, email tools, skill discovery — bundled with the PDE Ops Tools plugin, not installed via this wizard |
| 1 | Atlassian MCP | Covered by org connector | Jira/Confluence search, issue creation, sprint management |
| 2 | Grafana (gcx) MCP | Not installed (will install dependencies) | Dashboards, alerts, SLOs, incident analysis |
| 3 | LaunchDarkly MCP | Not installed | Feature flag management |
| 4 | LogRocket MCP | Not installed | Session replay, metrics, issue search |
| 5 | Salesforce prod MCP | Not installed (will install dependencies) | SOQL queries against the prod org |
| 6 | Salesforce UAT MCP | Not installed | SOQL queries against the UAT org |

For the `pde_mcp` row, `ready: null` means the PDE Ops Tools plugin (`pde`) itself wasn't found
installed — genuinely unusual, since this skill is bundled inside it, so seeing this likely means
something's off with the CLI/marketplace lookup itself; show `detail` verbatim rather than a
made-up status word. `ready: false` means something's actually wrong with an install that *was*
found (venv missing, or a broken dependency) — put `detail` in the Description column so it's
visible without the user having to ask, since this row never leads to a follow-up step the way the
picker rows do.

`Status` for the 6 numbered rows comes from `installed`/`ready`/`dependencies`, mapped like this:
- **Installed and ready** (`installed: true`, `ready: true`, or no dependencies and `installed:
  true`): `"Installed"`.
- **Not installed, and its `dependencies` entry isn't ready yet** (`grafana`, `salesforce-prod`,
  `salesforce-uat` — check the dependency's own `ready`/`installed`, since `status` always
  includes `dependencies` even when the service itself isn't installed): `"Not installed (will
  install dependencies)"` — this tells the user up front that picking it walks through the
  dependency first, not just a plain install.
- **Not installed, and either it has no dependency or the dependency is already ready**: plain
  `"Not installed"` — nothing stands between picking it and it working.
- **Installed but `ready: false`** (the dependency regressed after install — e.g. someone logged
  out of `sf` later): `"Installed — check dependency"`, a distinct tag from the not-installed
  case since re-registering isn't what's needed here, just re-authenticating.
- **Atlassian, when `org_connector.connected` is `true`**: `"Covered by org connector"`, same as
  before.

Keep `Description` to what the service generally does — **don't fold dependency specifics in
there** (e.g. don't write "sf CLI not logged into 'prod'" in this column); `Status` already flags
that something needs doing, and once the user actually picks the row, step 2/3 explains exactly
what and how. For Atlassian, `Status` saying "Covered by org connector" is sufficient on its own —
don't also list the six bundled skill names anywhere in the table; mention those only if the user
asks what the plugin would add on top. **Atlassian stays in the table as a real, pickable row
even when covered** — it isn't actually installed via this plugin in that case, so installing it
anyway for the bundled skills is still a live option, not something to hide or grey out.

After the table, ask in plain text: "Which one would you like to work on? Reply with a number, or
let me know if you're done." — handle exactly one pick at a time (matches the loop below: after
handling one pick, re-run status and show a fresh table, rather than batching several installs
from one answer).

If every one of the 6 numbered companion services is already installed and ready, skip the
question — just show the table (`pde_mcp` row included) and say there's nothing left to do.
Atlassian being covered by an `org_connector` doesn't count toward this on its own, since
installing it anyway is still a standing option. `pde_mcp`'s own readiness never affects whether
there's "nothing to do" — it's informational only, not part of what this wizard can act on.

Once they answer with a number or a name, handle exactly that one pick (step 2/3), then loop back
to step 1 for a fresh status table and a fresh question — never show the table or ask again
before that reply comes back.

### 2. Handle an install pick

If `status` already shows it installed, say so and skip straight to dependency/readiness handling
below — nothing to install.

Otherwise run `python3 manage_companions.py install <service> --cli <claude|copilot>`.

**`install` will refuse to run for a service with an unmet blocking dependency** (currently: `sf`
for salesforce-prod/uat, `gcx` for grafana — both work the same way; see step 3). Neither the
Grafana plugin's MCP server nor salesforce-prod's is a hosted/HTTP server — both shell out to a
local CLI directly, so registering either before its CLI is installed and authenticated would
leave the same kind of broken-looking, half-working install sitting there. The result comes back
as `{"success": false, "blocked": true, "unmet_dependencies": [...], ...}`
instead of actually registering anything. When you see `blocked: true`, don't treat it as a
generic failure — go straight to step 3 for each entry in `unmet_dependencies`, and once the user
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
- For any service with unmet dependencies (see step 3), handle those too before considering the
  service done.

### 3. Handle a dependency/readiness gap

This is the part that most needs to be unmistakable, because it's where a human has to stop and
do something outside the conversation — a plain paragraph blends into everything around it and
gets missed. Wrap every one of these in a plain rule line (repeated `=`, a fixed length like 70
characters) above and below, with a `MANUAL STEP NEEDED` header — not box-drawing characters
(`┌─┐│`), which need every line's width to line up exactly and visibly break the moment the
content wraps differently across terminal widths; a plain rule doesn't have that problem:

```
======================================================================
MANUAL STEP NEEDED — Step 1 of 2: install the sf CLI
======================================================================
I can't do this myself — it needs root/admin access on this machine.

    sudo npm install -g @salesforce/cli

Run that yourself, then come back and tell me to continue — I'll re-check before moving on, not
just take your word for it.
======================================================================
```

Number sequential steps ("Step 1 of 2", "Step 2 of 2") whenever more than one manual step is
currently outstanding for the same service (e.g. install the CLI, then log in), and show every
outstanding one in the same message — don't drip-feed them one at a time when the user already
needs to do both.

- **A dependency isn't installed and might need root** (currently: `sf` for salesforce-prod/uat,
  `gcx` for grafana). Run `python3 manage_companions.py dep-guidance <dependency>` (e.g.
  `dep-guidance sf` or `dep-guidance gcx`) — this actually tests the machine, it doesn't guess.
  Then:
  - If `root_required` is `null`: relay the `prerequisite` field (e.g. install Node.js first for
    `sf`, or Go/git for `gcx` on Windows) — there's nothing to run yet, so this doesn't need the
    box treatment, just say plainly what to install first.
  - If `root_required` is `true`: this is a **hard stop for you** — you have no way to supply a
    root/admin password interactively even if you tried. Use the box format above.
  - If `root_required` is `false`: same box format, but phrase the content as an offer inside it,
    since you *could* run it: "This doesn't need root on your machine — want me to run
    `<command>` for you, or would you rather run it yourself?" Only run it after they say yes.
    `gcx`'s install script normally lands here (it installs to `~/.local/bin`, never root) — don't
    assume it needs the same root/no-root ambiguity `sf` does, `dep-guidance` already resolved
    that.
- **A dependency is installed but not authenticated** — `sf` CLI present but the relevant alias
  isn't logged in, or `gcx` CLI present but `gcx config check` fails: both are always something
  only the human can do (interactive browser login) — same box format, headed e.g. "MANUAL STEP
  NEEDED — Step 2 of 2: log into Salesforce" with the exact command (`sf org login web --alias
  prod`/`--alias uat`, or `gcx login` for Grafana).
- **OAuth-based services with no local dependency** (logrocket, atlassian, launch-darkly): after
  a restart, you can proactively call one of that service's tools right away (e.g. "list my
  feature flags") to trigger the login immediately instead of leaving the user to stumble into it
  later — ask first, since it'll pop an auth prompt.
- **Atlassian specifically**: check `org_connector` from `status` first. If it's present and
  `connected: true`, say so plainly before pushing OAuth on the plugin's own entry — a connected
  `claude.ai`-configured connector already provides the same Jira/Confluence tools, so
  authenticating the plugin's separate `plugin:atlassian:...` entry is only worth doing if the
  user wants this plugin's bundled skills (`capture-tasks-from-meeting-notes`,
  `generate-status-report`, `jira-sprint-dashboard-canvas`, `search-company-knowledge`,
  `spec-to-backlog`, `triage-issue`) — ask which they want rather than assuming.

Never bundle one of these action-needed moments into a paragraph of other text — always give it
the `MANUAL STEP NEEDED` rule-line treatment above so it can't be missed.

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
  none of this history: just re-invoke the skill from the top. The status readout reflects
  whatever changed while they were away; there's no prior state to recover because none of it
  lived in conversation memory in the first place.

## Available services

- **Atlassian** — Jira/Confluence search, issue creation, sprint management.
  - Claude Code: the full official plugin (6 skills), via the pre-registered
    `claude-plugins-official` marketplace. `status` also checks (Claude Code only) for a
    pre-existing `claude.ai`-configured Atlassian connector — often provisioned org-wide,
    entirely separate from this plugin — and surfaces it as `org_connector`; if it's already
    connected, the bundled skills work against it too, so authenticating this plugin's own entry
    is only needed if that connector isn't there or the user wants a clean separation.
  - Copilot CLI: that marketplace file fails to parse there (a real schema incompatibility on the
    `source` field of several entries, not a typo) — `install` falls back to registering the bare
    `chg-atlassian` MCP endpoint instead. Tools only, no bundled skills, until that gets fixed
    upstream. No `org_connector` check either — that's a Claude Code-only concept.
- **Grafana (`gcx`)** — 16+ skills, a `grafana-debugger` agent, dashboard/alert/SLO management.
  Same install mechanism on both CLIs. Its MCP server shells out to the local `gcx` CLI directly
  (not a hosted HTTP MCP), so it needs the `gcx` CLI installed *and* authenticated to a stack
  first — `install` refuses to register it otherwise, exactly like Salesforce below.
- **LaunchDarkly** — feature flag management. Remote MCP, authenticates via an interactive OAuth
  prompt the first time it connects — no static credentials to configure. Same install mechanism
  on both CLIs.
- **LogRocket** — session replay, metrics, issue search. Same install mechanism on both CLIs.
- **Salesforce prod** — SOQL queries against the prod org. Needs the `sf` CLI authenticated to the
  `prod` alias (see step 3 above). **`install` refuses to register this MCP until that's true** —
  there's no point registering an entry that can't work yet.
- **Salesforce UAT** — SOQL queries against the UAT org. Needs the `sf` CLI authenticated to the
  `uat` alias (see step 3 above); `install` is gated the same way.
