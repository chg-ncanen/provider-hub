---
name: setup-companion-tools
description: Interactively install optional companion MCPs/plugins for PDE work (Atlassian, Figma, Grafana, LaunchDarkly, LogRocket, Playwright, Salesforce prod/UAT) that aren't bundled in the pde plugin. Use when the user asks to set up, connect, install, or configure additional PDE tools/MCPs, or asks what companion tools are available.
user-invocable: true
---

# Setup Companion Tools

A guided wizard for optionally installing MCP servers/plugins commonly used alongside PDE
tooling, that aren't bundled in the `pde` plugin itself: Atlassian (Jira/Confluence), Figma,
Grafana (`gcx`), LaunchDarkly, LogRocket, Playwright, Salesforce prod, and Salesforce UAT. None
of these are called
automatically by any code in the `pde` plugin — nothing here runs on its own, only when a
developer explicitly invokes this skill, and only for whichever service(s) they pick. Whether a
particular downstream skill needs one of these installed is that skill's own concern to check,
not this one's.

The wizard loop below assumes a human is present. For an async/headless agent run, `grafana` and
`salesforce-prod`/`salesforce-uat` support a non-interactive auth path via pre-supplied env vars —
see "Non-interactive auth" further down.

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
Render the result as **all eight companion services, plus `pde_mcp` itself**, as one markdown
table — a plain numbered pick, not an interactive tool prompt. (An earlier version of this skill
used `AskUserQuestion`, but that tool caps at 4 options, which meant several of the 8 services
always had to be demoted to "type the name yourself" — worse than just listing all 8 up front.) A table
is also what actually gets read; a plain status paragraph followed by something else blends in
and gets skipped.

One row per service, alphabetical, numbered so the user can reply with just a digit. Name each
one with "MCP" in it (e.g. "Atlassian MCP") so it's clear these are MCP servers being installed —
**except Grafana, which isn't one** (see below); call that row "Grafana (gcx CLI plugin)" instead,
so it doesn't imply an MCP server exists where there isn't one.
**Add one more row above all of them, unnumbered, for `pde_mcp`** — the core MCP server bundled
with the PDE Ops Tools plugin (`pde`) itself, not something this wizard installs (that's the
`SessionStart` hook's job). It's still worth surfacing here since every companion tool sits
alongside it, and its
`ready`/`detail` fields come from `status` the same way: use `—` instead of a number in that row so
it's visibly not a pickable option, and if the user replies with that dash or its name anyway,
just explain it isn't installable here — this only means it's not a numbered choice to *install*;
missing required config on this row still gets proactively flagged regardless of what the user
picks (see below), that's a different thing from "picking" it.

| # | Service | Deps | Installed | Configured | Connected | Description |
|---|---|---|---|---|---|---|
| — | pde-mcp | — | ✅ | ✅ | — | JSM alert management, email tools, skill discovery — bundled with the PDE Ops Tools plugin, not installed via this wizard |
| 1 | Atlassian MCP | — | ✅ | — | ✅ (org connector) | Jira/Confluence search, issue creation, sprint management |
| 2 | Figma MCP | — | ❌ | — | — | Design files, styles, components, layout for design-to-code |
| 3 | Grafana (gcx CLI plugin) | ❌ gcx CLI not found | ❌ | ❌ | — | Dashboards, alerts, SLOs, incident analysis |
| 4 | LaunchDarkly MCP | — | ✅ | — | ❌ not authenticated yet | Feature flag management |
| 5 | LogRocket MCP | — | ❌ | — | — | Session replay, metrics, issue search |
| 6 | Playwright MCP | — | ✅ | — | — | Browser automation: navigate, fill forms, screenshots, run JS |
| 7 | Salesforce prod MCP | ✅ | ✅ | ✅ | ✅ | SOQL queries against the prod org |
| 8 | Salesforce UAT MCP | ❌ sf CLI too old (v1.5.0, need v2.0.0+) | ❌ | ❌ | — | SOQL queries against the UAT org |

Four checkmark-style columns instead of one prose "Status" column — each answers one yes/no
question on its own, so a row's overall state is scannable at a glance instead of buried in a
sentence:

- **`Deps`** — is there a local CLI dependency, and is it clear of anything that would make
  `install` refuse? Only `grafana` (`gcx`) and `salesforce-prod`/`salesforce-uat` (`sf`) have one;
  the other three (`atlassian`, `launch-darkly`, `logrocket`) are lazily-OAuth'd MCP servers with no
  local dependency at all.
  - **No `dependencies` entry on this service at all**: `"—"` — not applicable, nothing to check.
  - **Has a `dependencies` entry, and none of them have `blocking: true` with `ready: false`**:
    `"✅"` — clear to install, regardless of whether it's actually configured/authenticated yet
    (that's what `Configured`/`Connected` are for). This includes the CLI being installed but just
    not logged in yet — not-authenticated is never blocking (see step 3).
  - **Has a `dependencies` entry with `blocking: true` and `ready: false`**: `"❌"` + the shortest
    accurate paraphrase of that entry's `detail` (e.g. "gcx CLI not found", "sf CLI too old (v1.5.0,
    need v2.0.0+)") — covers both "CLI missing entirely" and "CLI installed but below its version
    floor" the same way, since `install` refuses for either reason identically (see step 2).
- **`Installed`** — the service's own `installed` field, nothing else: `"✅"` if `true`, `"❌"` if
  `false`. This is pure registration state — whether it actually works belongs in `Configured`/
  `Connected`, not folded in here. A `"❌"` here with `Deps` also `"❌"` tells the user in one glance
  that picking this row walks through a dependency install first, not just a plain install.
- **`Configured`** — for `grafana`/`salesforce-prod`/`salesforce-uat` (`"—"` for `atlassian`,
  `launch-darkly`, `logrocket` — same as `Deps`, there's no local CLI to point at anything for those
  three): has the CLI actually been pointed at the right target at some point, *regardless of
  whether that session is still live*? `sf` — the alias exists in `sf org list --json` at all, any
  `connectedStatus`. `gcx` — the current context (`gcx config list-contexts --output json`, the
  entry with `current: true`) has a `server` set. This is the field `dependencies[].configured`
  feeds — `"✅"` if `true`, `"❌"` if `false`, same bare-checkmark style as `Installed` (the reason,
  if any, already showed up in `Deps` or will show up in `Connected`). **This is what actually
  distinguishes "never logged in" from "logged in once, but the session died"** — the case
  `Connected` alone would otherwise flatten into an identical-looking `"❌"`: a revoked/expired `sf`
  refresh token, or a `gcx` context whose OAuth session expired, both read as `Configured: ✅,
  Connected: ❌` instead of looking like nothing was ever set up (`dependencies[].detail` says
  exactly this — "configured for '<alias>' but the session isn't live", "current context is set but
  the session isn't live" — reuse that text rather than re-deriving it). `pde_mcp` also gets a real
  `Configured` value (see below) — it's the one row where this column isn't `"—"` despite having no
  `dependencies` entry, since its userConfig check works differently (see `pde_mcp_configured()`).
- **`Connected`** — comes from `ready`/`dependencies`/`org_connector` — **always resolve it to the
  actual concrete reason, never a vague placeholder**: if it isn't connected, say why, in the same
  terms the dependency's own `detail` field already gives you (e.g. "not logged into 'prod'", "gcx
  CLI not authenticated", "not authenticated yet" for a lazily-OAuth'd entry) rather than a generic
  tag like "check dependency" that just tells the user to go look — you already ran the check, so
  the table should say what it found:
  - **Not installed** (`installed: false`): `"—"` — nothing to be connected yet.
  - **Installed, `ready: true`**: `"✅"`.
  - **Installed, `ready: false`**: `"❌ "` + the shortest accurate paraphrase of the relevant
    `dependencies[].detail` (there's always exactly one dependency entry driving `ready` for each of
    these services — the blocking CLI dependency for `grafana`/`salesforce-prod`/`salesforce-uat`,
    or the synthetic `"OAuth session"` entry for `atlassian`/`figma`/`launch-darkly`/`logrocket`).
  - **Installed, `ready: null` and it has no `dependencies` entry at all** (currently: `playwright`
    only): `"—"` — not a vague "unknown", a genuine "there's no live-connection signal to check for
    this service at all": no CLI to authenticate, no OAuth session, nothing that can be
    connected/disconnected. Don't render this as `"❌"` (that would imply something's actually
    wrong) or invent a status word — once `Installed` is `"✅"` for a service with no dependency and
    no OAuth session match, it just works.
  - **Atlassian specifically, when `org_connector.connected` is `true`**: `"✅ (org connector)"`,
    regardless of the plugin's own `ready` — a connected org-wide connector already covers the
    bundled skills, so the plugin's separate OAuth session not being done yet doesn't make this "not
    connected" from the user's point of view.

For the `pde_mcp` row, `Deps` and `Connected` are always `"—"` (it isn't something this wizard
installs, and isn't a service you connect to a login/OAuth session — it's the plugin's own bundled
server). `Configured`, though, is a real check here (see above) — `pde_mcp_configured()` reads
whether the plugin's *required* userConfig (`ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`) is actually
set, straight from Claude Code's own on-disk plugin config (`~/.claude/settings.json`'s
`pluginConfigs` for the non-sensitive fields, `~/.claude/.credentials.json`'s `pluginSecrets` for
the ones marked `"sensitive": true` in `plugin.json` — checked for presence only, never for the
actual value, which this skill should never print, log, or ask the user to paste).
`pde_mcp_config_state()` behind it also breaks out *which* fields are missing
(`missing_required_config`/`missing_optional_config` on the `pde_mcp` status entry — `ATLASSIAN_EMAIL`/
`ATLASSIAN_API_TOKEN` required, `EMAIL_USERNAME`/`EMAIL_PASSWORD` optional, only needed for email
features) rather than just a pass/fail bit, so the guidance below can say exactly what's missing
instead of a vague "something's unconfigured." Claude Code-only and best-effort: both return
`None`/empty (render as `"—"`, not a false `"❌"`) on Copilot CLI, or whenever either file can't be
read at all. `ready: null` means the PDE Ops Tools plugin (`pde`) itself wasn't found installed —
genuinely unusual, since this skill is bundled inside it, so seeing this likely means something's
off with the CLI/marketplace lookup itself; show `detail` verbatim as the `Installed` cell rather
than a checkmark in that case. `ready: false` means something's actually wrong with an install that
*was* found (venv missing, or a broken dependency) — `Installed` is still `"❌"`, but put `detail` in
the Description column too so it's visible without the user having to ask.

**Unlike the rest of this row, missing `pde_mcp` config *does* lead to a follow-up step** — even
though `pde_mcp` itself stays unnumbered/non-pickable (this wizard still doesn't install the
plugin), give `missing_required_config` the same unmistakable `MANUAL STEP NEEDED` box treatment as
step 3's other manual steps whenever it's non-empty, right alongside the status table rather than
waiting for the user to ask about it:

```
======================================================================
MANUAL STEP NEEDED: fill in pde-mcp's missing config — ATLASSIAN_API_TOKEN
======================================================================
JSM alert tools will fail until this is set. Run this in your own terminal (not something I can
do for you — this is a credential):

    /plugin configure pde

That opens Claude Code's interactive config form for every userConfig field the plugin declares,
including this one, and stores it the same secure way as a normal plugin install prompt.
======================================================================
```

- **Always point at `/plugin configure pde`** (or `/plugin` → choose "Configure" for the `pde`
  plugin, if the exact subcommand form isn't accepted) as the primary path — it's the one
  interactive flow that handles every declared field, sensitive or not, through Claude Code's own
  secure storage.
- **Never construct or run a `claude plugin install ... --config ATLASSIAN_API_TOKEN=...` /
  `--config EMAIL_PASSWORD=...` command yourself, and never ask the user to paste either value into
  the chat** — both are marked `"sensitive": true` in the plugin's schema for a reason; typing them
  into a shell command or a chat message puts them in this conversation's transcript/history, which
  defeats the point of the secure storage `/plugin configure` uses. Route both to the human via the
  box above, exactly like every other credential-entry step in this skill.
- **`ATLASSIAN_EMAIL`/`EMAIL_USERNAME` are not sensitive** — if the user would rather just tell you
  the value in chat than run the interactive flow, it's fine to relay it via `claude plugin install
  pde@provider-hub --config ATLASSIAN_EMAIL=<value>` yourself (repeatable per field; validated
  against the plugin's schema and stored via the same path `/plugin configure` uses) — same
  "just install when it can" philosophy as the rest of this skill for anything that isn't a secret.
- `missing_optional_config` (just `EMAIL_PASSWORD`/`EMAIL_USERNAME`) only matters if the user
  actually wants email features — mention it, but don't give it the same urgency as
  `missing_required_config`; JSM/alert tools work fine without it.

Keep `Description` to what the service generally does — the concrete not-connected/not-configured
reason belongs in `Connected`/`Configured`/`Deps`, not here. Don't also list the six bundled
Atlassian skill names anywhere in the table; mention those only if the user asks what the plugin
would add on top. **Atlassian stays in the table as a real, pickable row even when covered** — it
isn't actually installed via this plugin in that case, so installing it anyway for the bundled
skills is still a live option, not something to hide or grey out.

**If `Configured` is `"❌"` and it's worth showing the user *why*** — e.g. `sf` knows aliases other
than `prod`/`uat` and one might be a typo/leftover, or `gcx` has more than one context and the
wrong one is current — pull the raw list (`sf org list --json`, or `gcx config list-contexts
--output json`) and show it as **a table with defined columns** (e.g. `Alias | Org URL | Connected
Status` for `sf`, `Context | Server | Current` for `gcx`), not a prose paragraph — same reasoning as
the main status table: a list of items is something the user scans and compares row-by-row, and a
table is what actually gets read for that. Only do this when it's genuinely useful for
troubleshooting the specific gap in front of you, not as a routine part of every status check.

After the table, ask in plain text: "Which one would you like to work on? Reply with a number, or
let me know if you're done." — handle exactly one pick at a time (matches the loop below: after
handling one pick, re-run status and show a fresh table, rather than batching several installs
from one answer).

If every one of the 8 numbered companion services is already installed and ready — or, for a
service with no live-connection signal at all (currently: `playwright`), simply installed — skip
the question — just show the table (`pde_mcp` row included) and say there's nothing left to do.
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

**`install` only refuses to run when the underlying CLI dependency isn't usable yet** — for both
`sf` (salesforce-prod/uat) and `gcx` (grafana) that means either not installed at all, *or*
installed but below the minimum version that MCP server/plugin needs (see step 3). It
does **not** wait for that CLI to be authenticated first: verified directly that the Salesforce MCP
server starts up and registers all its tools cleanly even against a nonexistent/never-authenticated
alias (no crash, no broken half-registered state), and the `gcx` plugin has no MCP server of its
own to begin with (just skills/agents that shell out to `gcx` when actually invoked) — so in both
cases, installing ahead of authentication is safe, same lazy-auth pattern as the OAuth-based
services. When `install` *does* refuse (dependency missing or too old), the result comes back as
`{"success": false, "blocked": true, "unmet_dependencies": [...], ...}` instead of actually
registering anything — go straight to step 3 for each entry in `unmet_dependencies`, and once
that's resolved (whether you just fixed it yourself via `dep-install`, or the user told you they
fixed it manually), **retry the same `install` call** (don't just re-check status and stop) —
that's what actually registers the MCP/plugin once the dependency clears.

Otherwise relay the result (`success`, what got installed, or the `error` if it failed for some
other reason).

**Installed is not the same as ready to use.** For every successful install, also do whatever's
needed to actually finish setup:

- If the result has a non-null `post_install` field (all eight services currently have one):
  relay it verbatim, and be explicit that a
  **session restart** is required first — the newly installed server/plugin isn't connected in the
  *current* session. Tell the user plainly: "restart your session now; when you're back, just ask
  me to check companion tools status again (or re-run this skill) and I'll pick up exactly where
  we left off."
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
MANUAL STEP NEEDED — Step 1 of 2: install Node.js
======================================================================
I can't do this myself — the sf CLI installs via npm, and Node.js/npm isn't on this machine yet.

    Install Node.js first (nodejs.org, or your OS package manager/nvm), then tell me to continue.

I'll re-check before moving on, not just take your word for it.
======================================================================
```

Number sequential steps ("Step 1 of 2", "Step 2 of 2") whenever more than one manual step is
currently outstanding for the same service (e.g. install a missing prerequisite, then log in), and
show every outstanding one in the same message — don't drip-feed them one at a time when the user
already needs to do both.

- **A dependency is missing, or installed but below the minimum version this MCP server/plugin
  needs** (currently: `sf` for salesforce-prod/uat — including machines with a leftover
  pre-unification `sf`/legacy `sfdx-cli` from a while back, which looks "installed" but flat-out
  lacks the `--orgs`/`--toolsets` flags this MCP server needs; and `gcx` for grafana — pinned to
  the actual confirmed-working version, since gcx has no vendor-declared minimum). **Default to just
  installing (or upgrading) it yourself, without asking permission first** — run
  `python3 manage_companions.py dep-install <dependency>` (e.g. `dep-install sf` or
  `dep-install gcx`) directly. **Root is never actually required for either of these, so don't
  present it as a permission question or a blocker** — confirmed by actually running the fix
  end-to-end on a machine whose default npm global prefix was root-owned: `gcx` always installs to
  `~/.local/bin` or the user's Go bin dir (no root, ever); `sf` via npm either uses the default
  prefix directly when it's already user-writable, or — when it isn't (common when Node.js came
  from apt/a system package manager) — points npm's global prefix at a user-owned directory
  instead (`npm config set prefix ~/.npm-global`, npm's own documented fix, not sudo) and installs
  there. `dep-install` tries the Homebrew cask first on macOS when `brew` is present (`brew install
  --cask salesforce-cli` for a fresh install, `brew upgrade --cask salesforce-cli` when the existing
  install already came from that cask) as an even simpler no-root option, before falling back to
  the npm path. State what you're about to run in one short sentence first (transparency, not a
  yes/no question — e.g. "Installing the sf CLI now — no root needed."), then relay the actual
  result from `dep-install`'s JSON (`success`, `method`, `command`, `note`, or `error`) — verify
  from that, don't assume it worked just because you ran it. **If `dep-install` reports a `note`
  about PATH** (the npm-user-prefix and both gcx paths install outside the usual system
  directories): this is two sequential steps, not two alternatives — update the shell profile
  *first* (the `note` gives the exact `export PATH=...` line), *then* a genuinely new terminal
  window/tab/SSH session (one that hasn't started yet) picks it up. **Restarting the Claude Code
  session by itself never fixes this**, even after the profile is updated — that just spawns a new
  `claude` process inside the *same already-running terminal shell*, which already read its
  profile once at its own startup and won't re-read it just because a child process restarts.
  Verified directly: `sf` stayed unresolvable via plain `which sf` even after the profile was
  updated, until a genuinely new shell was started. Tell the user this plainly rather than letting
  "restart your session" imply the same fix as the MCP/plugin case above — they're different
  restarts. Once a dependency install/upgrade succeeds (and PATH is sorted if needed), retry the
  service's own `install` call (step 2) — that's what actually registers the MCP/plugin now that
  the dependency is clear.
  - **Only fall back to a manual step when `dep-install` itself reports `{"success": false,
    "blocked": true, ...}` with `command: null`** — in practice this means Node.js is missing for
    `sf`, or Go/git is missing for `gcx` on Windows (see `prerequisite`): there's a real prerequisite
    to install first, with nothing to run yet, so use the box format above but don't frame it as a
    root/permission issue — it isn't one.
  - To preview what `dep-install` would do without actually running anything (e.g. the user asks
    "what would that involve" before deciding), `dep-guidance <dependency>` resolves and returns
    the exact same fields, read-only.
- **A dependency is installed but not authenticated** — `sf` CLI present but the relevant alias
  isn't logged in, or `gcx` CLI present but `gcx config check` fails. **Before falling back to an
  interactive login for `sf`/`gcx` specifically**, check whether the matching env var is already
  set in this process's environment: `SFDX_AUTH_URL_PROD`/`SFDX_AUTH_URL_UAT` for the two
  Salesforce aliases, `GRAFANA_TOKEN` (optional `GRAFANA_SERVER` override) for gcx. If it is, this
  is an async/headless run where the credential was supplied ahead of time — run
  `python3 manage_companions.py auth <service>` instead of asking a human to log in, and relay its
  JSON result the same way as `install`/`dep-install` (see "Non-interactive auth" below for what
  each does). Most interactive runs won't have these set, and that's expected — fall straight
  through to the manual box below in that case, don't ask the user whether they want to set one.
  - **Neither env var set (the normal interactive case)**: both are always something only the
    human can do (interactive browser login) — same box format, headed e.g. "MANUAL STEP NEEDED —
    Step 2 of 2: log into Salesforce". **For `sf`, use the exact command from `status`'s own
    `ready_hint` for that service** rather than retyping or reconstructing it here — prod and uat
    both need an explicit `--instance-url` (uat because Salesforce sandboxes are never reachable
    via the generic login page regardless of org config; prod because this org's own custom domain
    doesn't accept the generic login page either), and `manage_companions.py`'s
    `sf_login_command()` / `SF_LOGIN_INSTANCE_URLS` is the one place that knows the current URL for
    each — if either domain ever changes, that's the only place that needs updating, so don't
    duplicate the URLs into this file too. For Grafana: `gcx login --server
    https://chg.grafana.net` — that's this org's Grafana Cloud stack; don't let the user log into a
    different one.
- **OAuth-based services with no local dependency** (logrocket, atlassian, launch-darkly, figma):
  `status` shows `Connected: "❌ ..."` in the table (not just `"—"`) until the entry's own live
  connection state — read from `claude mcp list`, not just "the plugin is registered" — comes back
  connected. While it's not: after a restart, you can proactively call one of that service's tools
  right away (e.g. "list my feature flags", "get the layout for this Figma frame") to trigger the
  login immediately instead of leaving the user to stumble into it later — ask first, since it'll
  pop an auth prompt.
- **Playwright specifically**: has no OAuth session and no CLI dependency either — nothing to
  authenticate, ever. Its `post_install` covers the one real first-use gap (browser binaries not
  yet downloaded) — relay that if a browser tool call actually fails with a missing-browser error,
  don't treat it as a readiness gap to check for proactively the way OAuth services get one.
- **Atlassian specifically**: check `org_connector` from `status` first. If it's present and
  `connected: true`, say so plainly before pushing OAuth on the plugin's own entry — a connected
  `claude.ai`-configured connector already provides the same Jira/Confluence tools, so
  authenticating the plugin's separate `plugin:atlassian:...` entry is only worth doing if the
  user wants this plugin's bundled skills (`capture-tasks-from-meeting-notes`,
  `generate-status-report`, `jira-sprint-dashboard-canvas`, `search-company-knowledge`,
  `spec-to-backlog`, `triage-issue`) — ask which they want rather than assuming.

Never bundle one of these action-needed moments into a paragraph of other text — always give it
the `MANUAL STEP NEEDED` rule-line treatment above so it can't be missed.

## Non-interactive auth

This wizard is normally interactive — the loop above assumes a human is present to answer the
numbered question and complete any browser login. For an async/headless agent run where the
relevant credential was supplied ahead of time (e.g. as an environment variable on the process),
`salesforce-prod`, `salesforce-uat`, and `grafana` each have a non-interactive alternative to the
browser login — see the env-var check in step 3's "installed but not authenticated" case above for
when to use it instead of the manual box.

- **`salesforce-prod` / `salesforce-uat`**: needs `SFDX_AUTH_URL_PROD` / `SFDX_AUTH_URL_UAT` set to
  an sfdx auth URL, generated ahead of time on any machine already logged into that alias with `sf
  org auth show-sfdx-auth-url --target-org <prod|uat> --json` (the `sfdxAuthUrl` field of the
  result). `python3 manage_companions.py auth salesforce-prod` (or `salesforce-uat`) pipes that URL
  to `sf org login sfdx-url --sfdx-url-stdin` — never as an argv value, never printed. This is a
  bearer refresh token for whoever generated it, not a scoped service-account credential — the
  agent inherits that person's Salesforce permissions, and revoking access means revoking their
  connected-app session and regenerating the URL.
- **`grafana`**: needs `GRAFANA_TOKEN` set to a Grafana service-account token (Administration >
  Service accounts, in the target stack), and optionally `GRAFANA_SERVER` to override the default
  (`https://chg.grafana.net`, this org's stack). `python3 manage_companions.py auth grafana` writes
  it straight into gcx's own config store (`gcx config set stacks.chg.grafana.token ...` and
  switches to that context) instead of running `gcx login`'s browser OAuth — the same persistence
  mechanism `gcx login` itself would use, just supplied instead of typed into a browser flow. If
  the result has a `warning` field, it's gcx's own notice that no OS credential store (Keychain/
  Credential Manager/Secret Service) was available, so the token now sits in plaintext in gcx's
  config file on disk — relay this to whoever's running the headless agent rather than treating
  `success: true` as the whole story; it's expected on a bare CI/headless box but worth knowing.
- **Everything else stays interactive-only for now.** Atlassian, LogRocket, and LaunchDarkly each
  have a non-OAuth alternative on the vendor side (Atlassian's Rovo MCP server supports API-token
  auth once an org admin enables it; LogRocket's hosted MCP accepts a Bearer API key; LaunchDarkly
  only via its separate local npx/Docker MCP server, not the hosted one this skill installs) — none
  of that is wired up here yet, and each comes with a real tradeoff (an org-admin prerequisite, or
  installing a different server than the interactive path does) rather than being a simple flag
  addition. Figma's remote MCP is confirmed OAuth-only with no alternative at all. If one of these
  needs a non-interactive path later, treat it as new scope, not an extension of the `auth`
  subcommand as currently built.

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
- **Figma** — read design files, styles, components, layout for design-to-code work. Three real
  ways to reach Figma exist; this installs the one that fits every plan/seat with no extra
  credential to manage:
  - **This wizard installs Figma's official *remote* MCP server** (`https://mcp.figma.com/mcp`) —
    remote HTTP, authenticates via an interactive OAuth prompt the first time it connects, works on
    any Figma plan/seat. Same install mechanism and lazy-auth pattern as `launch-darkly`.
  - **Figma's official *desktop* Dev Mode MCP server** (`localhost:3845/mcp`) is a different,
    higher-fidelity option (live variables, Code Connect mappings) that this wizard does *not*
    install — it needs a paid Dev/Full seat (Starter/View/Collab seats get capped at 6 calls/month)
    *and* the Figma desktop app actually running locally with Dev Mode on, neither of which this
    script can check or start. If the user specifically wants this one, point them at Figma's own
    setup docs rather than trying to wire it up here.
  - **Framelink (`figma-developer-mcp`)**, a popular community alternative that works on any
    account/plan via a personal Figma access token instead of OAuth, isn't installed by this wizard
    either — the remote MCP server was chosen instead since it needs no per-user token to manage
    and matches this skill's existing OAuth-based services. Don't run both Figma MCP servers at
    once if a user already has
    Framelink configured some other way — two servers touching the same Figma data confuses tool
    selection more than it helps.
- **Grafana** — there are two genuinely different ways to reach it, and only one is actually usable
  here:
  - **A native Grafana MCP server** — this org hasn't enabled it, so it isn't a real option right
    now. Don't suggest connecting to it or troubleshoot it as if it should exist; if the user asks
    for "the Grafana MCP" specifically, tell them plainly that it isn't enabled for this org rather
    than trying to install or configure anything.
  - **The `gcx` CLI plugin (what this wizard actually installs)** — 16+ skills, a
    `grafana-debugger` agent, dashboard/alert/SLO management. No MCP server of its own — the
    skills/agent shell out to the local `gcx` CLI directly when actually invoked. Same install
    mechanism on both CLIs.
  - Needs the `gcx` CLI *installed and at least GCX_MIN_VERSION* first (`manage_companions.py`'s
    constant, currently `1.0.0` — gcx has no vendor-declared minimum CLI version for the plugin, so
    this is pinned to the actual confirmed-working version rather than inferred from anything;
    bump it deliberately when moving to a newer gcx release) — `install` refuses otherwise, whether
    `gcx` is missing entirely or just too old. Confirmed root is never required either way:
    `dep-install gcx` installs to
    `~/.local/bin` via the official install script (or the user's Go bin dir via `go install` on
    Windows) — no root, no sudo, ever — and this same command re-run also handles the upgrade case,
    since `gcx` has no separate self-update subcommand. Not necessarily authenticated yet after
    installing; installing the plugin has zero runtime footprint (no server to leave in a broken
    state), so it's fine to install ahead of `gcx login`. This org's stack is
    `https://chg.grafana.net` — that's the `--server` value to use for `gcx login`.
  - For a non-interactive/headless run, `GRAFANA_TOKEN` (optional `GRAFANA_SERVER` override) replaces
    `gcx login` — see "Non-interactive auth" above.
- **LaunchDarkly** — feature flag management. Remote MCP, authenticates via an interactive OAuth
  prompt the first time it connects — no static credentials to configure. Same install mechanism
  on both CLIs.
- **LogRocket** — session replay, metrics, issue search. Same install mechanism on both CLIs.
- **Playwright** — browser automation: navigate, fill forms, run JS, capture screenshots and
  structured page data. Microsoft's official `@playwright/mcp`, via `npx @playwright/mcp@latest` —
  local stdio, same install mechanism on both CLIs. No local CLI dependency to check, no OAuth
  session either, so `status` never has anything to say about this one beyond `installed` — it
  just works once registered. The one real first-use gap: browser binaries (and, on Linux, system
  libs) download lazily on first real use, not at install time — if a browser tool call fails with
  a missing-browser error, `npx -y playwright install --with-deps` fixes it. This can't be checked
  or fixed ahead of time the way `sf`/`gcx`'s dependencies can (there's no way to know it's missing
  until a tool actually tries to launch a browser), so don't try to proactively verify it during
  install — only mention it if a real failure with that shape actually happens.
- **Salesforce prod and UAT** — SOQL queries against the prod/UAT orgs, via `npx @salesforce/mcp`,
  scoped to the `prod`/`uat` alias respectively. Identical mechanics for both — one shared `sf` CLI
  dependency, checked and installed/upgraded the same way regardless of which alias you're setting
  up (see step 3): needs `sf` *installed and at least SF_MIN_VERSION* (`manage_companions.py`'s
  constant, currently the unified-CLI floor `2.0.0`) — `install` refuses otherwise, whether `sf` is
  missing entirely or just too old (a leftover pre-unification `sf`/legacy `sfdx-cli` seen on some
  machines looks "installed" but lacks the `--orgs`/`--toolsets` flags this MCP server needs) — but
  not necessarily logged into that alias yet: verified directly that the MCP server starts up and
  registers its tools cleanly even against a never-authenticated alias, only erroring if a tool is
  actually called before logging in. **The only real difference between the two is the login URL**
  — both need an explicit `--instance-url` (uat because it's a sandbox, unreachable via the generic
  login page regardless of org config; prod because this org's own custom domain doesn't accept the
  generic login page either), and the current URL for each lives in exactly one place,
  `manage_companions.py`'s `SF_LOGIN_INSTANCE_URLS`/`sf_login_command()` — not restated here, so a
  future domain change only needs to happen in that one file.
  For a non-interactive/headless run, `SFDX_AUTH_URL_PROD`/`SFDX_AUTH_URL_UAT` replaces `sf org
  login web` — see "Non-interactive auth" above.
