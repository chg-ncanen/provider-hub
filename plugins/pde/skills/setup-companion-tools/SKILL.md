---
name: setup-companion-tools
description: Interactively install optional companion MCPs/plugins for PDE work (Grafana, LogRocket, Atlassian, Salesforce prod/UAT, LaunchDarkly) that aren't bundled in the pde plugin. Use when the user asks to set up, connect, install, or configure additional PDE tools/MCPs, or asks what companion tools are available.
user-invocable: true
---

# Setup Companion Tools

A guided wizard for optionally installing MCP servers/plugins commonly used alongside PDE
tooling, that aren't bundled in the `pde` plugin itself: Grafana (`gcx`), LogRocket, Atlassian
(Jira/Confluence), Salesforce prod, Salesforce UAT, and LaunchDarkly. None of these are called
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

### 1. Show status as plain text

Run `python3 manage_companions.py status --cli <claude|copilot>` (from this skill's own
directory) and summarize it as short, plain sentences — **no `[x]`/`[ ]` checkbox glyphs, no
ASCII tree/indentation**. Those look like a real interactive checklist but are just text in a
chat message; they can't actually be clicked, which is confusing. One line per service is enough;
fold any dependency detail into that same line rather than a separate indented row:

- `Grafana (gcx): not installed — also needs the gcx CLI, not currently found on PATH.`
- `LogRocket: not installed.`
- `Salesforce prod: installed, but not ready — the sf CLI is installed but not logged into 'prod'.`
- `LaunchDarkly: not installed.`

**Atlassian is a special case when `org_connector` is present and `connected: true`**: don't
present it as "not installed" with a long explanation. Say, in one short line, that it's already
covered — e.g. `Atlassian: not installed via this plugin, but your existing 'claude.ai Atlassian'
connector is already connected and covers the same Jira/Confluence tools — only worth installing
this plugin if you want its bundled skills.` One sentence, not a bulleted list of the six skill
names — mention those only if the user actually asks what the bundled skills are.

If every service is already installed and ready (or, for Atlassian, already covered by the org
connector) and the user didn't ask to fix anything specific, just say so plainly — don't force the
question in step 2 when there's nothing left to offer.

### 2. Ask what to work on — with a real interactive prompt

Use the `AskUserQuestion` tool to ask **which one thing** to work on next — this is an actual
clickable prompt, unlike the plain status text above, which is what makes this feel like a real
choice instead of something to type free-form. Set `multiSelect: false`: handle exactly one
service at a time, by design (matches the loop below — after handling one pick, you re-render
status and ask again, rather than trying to batch several installs from a single answer).

`AskUserQuestion` allows at most 4 options per question, which is fewer than the 6 possible
services, so build the option list like this:
1. Walk the services in a fixed order — Grafana, LogRocket, Atlassian, Salesforce prod,
   Salesforce UAT, LaunchDarkly — and skip any that are already installed and ready (or, for
   Atlassian, already covered by a connected `org_connector`).
2. Take the first 3 remaining as concrete options, each labeled with the service name and a
   one-line description of its current state.
3. The 4th option is either "Nothing right now" (if 3 or fewer remained in step 1) or, when more
   than 3 remain, an option like "Something else" whose description names the rest by name — the
   user can still reach them by typing the name, since `AskUserQuestion` always offers a free-text
   "Other" alongside the listed options.

Once they answer (whether a listed option or free text), handle exactly that one pick (step 3/4),
then loop back to step 1 for a fresh status readout and a fresh single-pick question — never
render the status or ask again before that reply comes back.

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
- **Atlassian specifically**: check `org_connector` from `status` first. If it's present and
  `connected: true`, say so plainly before pushing OAuth on the plugin's own entry — a connected
  `claude.ai`-configured connector already provides the same Jira/Confluence tools, so
  authenticating the plugin's separate `plugin:atlassian:...` entry is only worth doing if the
  user wants this plugin's bundled skills (`capture-tasks-from-meeting-notes`,
  `generate-status-report`, `jira-sprint-dashboard-canvas`, `search-company-knowledge`,
  `spec-to-backlog`, `triage-issue`) — ask which they want rather than assuming.

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
  none of this history: just re-invoke the skill from the top. The status readout reflects
  whatever changed while they were away; there's no prior state to recover because none of it
  lived in conversation memory in the first place.

## Available services

- **Grafana (`gcx`)** — 16+ skills, a `grafana-debugger` agent, dashboard/alert/SLO management.
  Same install mechanism on both CLIs. Its MCP server shells out to the local `gcx` CLI directly
  (not a hosted HTTP MCP), so it needs the `gcx` CLI installed *and* authenticated to a stack
  first — `install` refuses to register it otherwise, exactly like Salesforce below.
- **LogRocket** — session replay, metrics, issue search. Same install mechanism on both CLIs.
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
- **Salesforce prod** — SOQL queries against the prod org. Needs the `sf` CLI authenticated to the
  `prod` alias (see step 4 above). **`install` refuses to register this MCP until that's true** —
  there's no point registering an entry that can't work yet.
- **Salesforce UAT** — SOQL queries against the UAT org. Needs the `sf` CLI authenticated to the
  `uat` alias (see step 4 above); `install` is gated the same way.
- **LaunchDarkly** — feature flag management. Remote MCP, authenticates via an interactive OAuth
  prompt the first time it connects — no static credentials to configure. Same install mechanism
  on both CLIs.
