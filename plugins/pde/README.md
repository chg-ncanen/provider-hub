# pde plugin

Plugin bundling the PDE team's JSM tooling, installable from any project — works with both
Claude Code and GitHub Copilot CLI, which both install from the same `.claude-plugin/` manifest.

## Contents

- **`mcp-servers/pde-mcp/`** — MCP server exposing JSM alert operations (`list_alerts`, `get_alert`,
  `acknowledge_alert`, `add_alert_note`, `close_alert`, `find_emails`, `send_email`) plus project
  skill discovery. Depends on the `pde-ops-api` library (`tools/team/pde/pde-ops-api` in this repo)
  via `requirements.txt` rather than bundling a copy — see that library's README.
- **`skills/resolve-duplicate-contact-alerts/`** — Skill that resolves duplicate-contact JSM alerts
  using the MCP server above plus `salesforce-prod`, a separate MCP server (the `@salesforce/mcp`
  npm package) not bundled here — see that skill's README for how to register it.
- **`skills/setup-companion-tools/`** — an opt-in wizard (invoke it by asking to set up/connect
  companion tools) for installing Grafana, LogRocket, Atlassian, `salesforce-prod`, `salesforce-uat`,
  and LaunchDarkly one at a time. It summarizes install/readiness state (including nested
  dependency state like whether the `sf` CLI is installed/logged in) as plain text before every
  pick, then asks which one thing to work on next via an actual interactive prompt, and gives
  OS/root-aware guidance — actually testing this machine rather than guessing — for any step only
  the human can do. Deliberately *not* automatic (no `SessionStart` hook does this, and none of
  these — including LaunchDarkly, previously bundled directly — run automatically just because
  `pde` is installed). Installing `pde` shouldn't silently pull in other teams'/vendors' plugins
  without you choosing to; whether a particular skill in this plugin needs one of these installed
  is that skill's own concern to check.

## Installing

Add the marketplace, then install — either directly, or by browsing it first:

```bash
# Claude Code — add marketplace, then install directly
/plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
/plugin install pde@provider-hub

# Claude Code — or browse instead: run /plugin with no arguments, open the "Discover" tab,
# and select pde from the provider-hub marketplace listed there

# Copilot CLI — add marketplace, then install directly
copilot plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
copilot plugin install pde@provider-hub

# Copilot CLI — or browse instead:
copilot plugin marketplace browse provider-hub
```

**Then start a new session** (close and reopen) — installing alone isn't enough. The venv setup
below runs via a `SessionStart` hook, which only fires at an actual session boundary (new session,
`--resume`/`--continue`, `/clear`, or compaction); `/plugin install` and `/reload-plugins` do *not*
trigger it. Until that hook runs once, `pde-mcp` can't launch — its command points at a venv that
doesn't exist yet.

That hook (`scripts/bootstrap-deps.sh`) does two things: creates a venv under
`${CLAUDE_PLUGIN_ROOT}/.venv` and installs `mcp-servers/pde-mcp/requirements.txt` into it (only
reinstalling when that file changes), and mirrors Claude Code's `userConfig` credentials into
`mcp-servers/pde-mcp/.env`. It's deliberately scoped to just those two things — companion tooling
(below) is handled elsewhere, since none of it is required to start `pde-mcp`.

**Credentials** (`ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN`, required; `EMAIL_USERNAME` /
`EMAIL_PASSWORD`, optional for `find_emails`):
- **Claude Code**: prompts for these on first enable (`.claude-plugin/plugin.json`'s `userConfig`).
  They reach `pde-mcp` two ways: directly into its process environment via `.mcp.json`'s
  `${user_config.*}` substitution (verified this reaches the subprocess env, including for
  `sensitive: true` fields), and mirrored into `mcp-servers/pde-mcp/.env` by the hook. The `.env` copy
  exists specifically for `resolve-duplicate-contact-alerts/run.py`, which is invoked directly rather
  than spawned via `.mcp.json` and so never sees the `${user_config.*}` substitution — without this,
  it would have no credential source at all on Claude Code. The hook only writes `.env` when
  `userConfig` actually supplied a value, so this is a no-op on Copilot CLI (no `userConfig`, so
  nothing to mirror) and won't overwrite a `.env` you created by hand there.
- **Rotating a credential later**: run `/plugin configure pde@provider-hub`, then restart the
  session — the hook re-mirrors the updated value into `.env` on that restart, and `.mcp.json`'s
  substitution picks it up for `pde-mcp` directly too.
- **Copilot CLI** has no `userConfig` equivalent — copy `mcp-servers/pde-mcp/.env.example` to
  `mcp-servers/pde-mcp/.env` yourself and fill it in; `app.py`'s `load_dotenv()` picks it up. If you
  already export `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN` etc. yourself (either CLI), those take
  precedence over `.env` regardless (see `app.py`'s `load_dotenv()`, default `override=False`).

## Upgrading

Pull the marketplace's latest commit, then update the plugin to it:

```bash
# Claude Code
/plugin marketplace update provider-hub
/plugin update pde@provider-hub

# Copilot CLI
copilot plugin marketplace update provider-hub
copilot plugin update pde@provider-hub
```

**Then start a new session** — same requirement as a fresh install (see above); `plugin update`
doesn't take effect in the session you ran it from. If `pde` isn't installed yet at all, use the
`marketplace add` / `plugin install` commands under Installing instead — there's nothing to
update.

## Prerequisites

- Python 3.9+ on the machine running the CLI.
- For `resolve-duplicate-contact-alerts`: the `sf` CLI authenticated to the `prod` org alias
  (`npm install -g @salesforce/cli && sf org login web --alias prod`), and the `salesforce-prod`
  MCP server registered separately (see that skill's README). `setup-companion-tools` can install
  and guide you through both — ask to set up `salesforce-prod`, or run its `dep-guidance sf`
  subcommand directly, which tests this machine and returns one decisive, sudo-safe install
  command rather than a menu of options.

## After installing: what to actually do

Both skills are `user-invocable`, so you can either ask for them in natural language or invoke them
directly by name:

- **`/pde:setup-companion-tools`** (or just ask: "set up companion tools" / "what companion tools
  are available?") — a guided wizard for installing Grafana, LogRocket, Atlassian,
  `salesforce-prod`, `salesforce-uat`, and LaunchDarkly, one at a time. Before every pick it
  summarizes install/ready state as plain text (plus nested dependency state — e.g. whether the
  `sf` CLI is installed and logged in for Salesforce, or `gcx` for Grafana), then asks which one
  thing to work on next through an actual interactive prompt rather than a plain-text checklist.
  This is how you get `salesforce-prod` registered for `resolve-duplicate-contact-alerts` above,
  and is the natural first thing to run after installing `pde` if you plan to use that skill.
  Grafana and Salesforce's MCP servers both shell out to a local CLI directly, so `install`
  refuses to register either one until its CLI is installed *and* authenticated —
  LogRocket/Atlassian/LaunchDarkly, which have no local CLI, authenticate via OAuth automatically
  on first real use instead (Atlassian specifically also checks for a pre-existing, often
  org-provisioned `claude.ai` connector that may already cover the same tools); steps only a human
  can do (root-required local installs, interactive logins) are called out explicitly rather than
  left in a wall of prose, and re-invoking
  the skill later (even in a new conversation) always re-checks real state instead of assuming.
  - Direct/scriptable equivalent, if you'd rather not go through the agent:
    `python skills/setup-companion-tools/manage_companions.py status --cli claude` (or `copilot`),
    `... install <service> --cli claude`, or `... dep-guidance sf`.
- **`/pde:resolve-duplicate-contact-alerts`** (or ask: "resolve duplicate contact alerts" — dry run
  by default) — runs the alert-resolution workflow. The agent runs
  `skills/resolve-duplicate-contact-alerts/run.py` for you; running it yourself directly
  (`python run.py` from that directory) works the same way and is useful for debugging — it checks
  its own dependencies up front and reports exactly what's missing.
- Anything else — `list_alerts`, `get_alert`, `find_emails`, etc. — is available as soon as
  `pde-mcp` is connected; just ask for what you want (e.g. "show me open P1 alerts").
