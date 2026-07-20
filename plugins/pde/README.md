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
- **`skills/setup-companion-tools/`** — an opt-in skill (invoke it by asking to set up/connect
  companion tools) for installing Grafana, LogRocket, Atlassian, `salesforce-prod`, and LaunchDarkly
  one at a time. Deliberately *not* automatic (no `SessionStart` hook does this, and none of these —
  including LaunchDarkly, previously bundled directly — are actually called by any code in this
  plugin; only `salesforce-prod` is a genuine dependency, of `resolve-duplicate-contact-alerts`).
  Installing `pde` shouldn't silently pull in other teams'/vendors' plugins without you choosing to.

## Installing

```bash
# Claude Code
/plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
/plugin install pde@provider-hub

# Copilot CLI
copilot plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
copilot plugin install pde@provider-hub
```

**Then start a new session** (close and reopen) — installing alone isn't enough. The venv setup
below runs via a `SessionStart` hook, which only fires at an actual session boundary (new session,
`--resume`/`--continue`, `/clear`, or compaction); `/plugin install` and `/reload-plugins` do *not*
trigger it. Until that hook runs once, `pde-mcp` can't launch — its command points at a venv that
doesn't exist yet.

That hook (`scripts/bootstrap-deps.sh`) does exactly one thing: creates a venv under
`${CLAUDE_PLUGIN_ROOT}/.venv` and installs `mcp-servers/pde-mcp/requirements.txt` into it, only
reinstalling when that file changes. It's deliberately minimal — only what's required for `pde-mcp`
to start at all. Credentials and companion tooling (below) are handled elsewhere, not by this hook.

**Credentials** (`ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN`, required; `EMAIL_USERNAME` /
`EMAIL_PASSWORD`, optional for `find_emails`):
- **Claude Code**: prompts for these on first enable (`.claude-plugin/plugin.json`'s `userConfig`)
  and passes them straight into the `pde-mcp` process's environment via `.mcp.json`'s
  `${user_config.*}` substitution — verified this actually reaches the subprocess env, including for
  the `sensitive: true` token/password fields, and that unset optional fields substitute to an empty
  string rather than a literal placeholder. No `.env` file is written; nothing to rotate by hand.
- **Rotating a credential later**: run `/plugin configure pde@provider-hub`, then restart the
  session. Since Claude Code re-supplies the current `userConfig` values on every session start
  through the same substitution, the new value takes effect immediately — no stale `.env` to delete.
- **Copilot CLI** has no `userConfig` equivalent — copy `mcp-servers/pde-mcp/.env.example` to
  `mcp-servers/pde-mcp/.env` yourself and fill it in; `app.py`'s `load_dotenv()` picks it up. If you
  already export `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN` etc. yourself (either CLI), those take
  precedence over `.env` regardless (see `app.py`'s `load_dotenv()`, default `override=False`).
- **`resolve-duplicate-contact-alerts`'s `run.py` is the one exception**, on *either* CLI: it's
  invoked directly rather than spawned via `.mcp.json`, so it never sees `userConfig`-substituted
  credentials — it needs the same hand-created `mcp-servers/pde-mcp/.env` that Copilot CLI always
  needs. Verified: with no `.env` and a clean environment, `run.py` fails with a clear message telling
  you to create that file or fall back to manual `pde-mcp` MCP tool calls (which do work via
  `userConfig` on Claude Code). See that skill's own README/SKILL.md.

## Prerequisites

- Python 3.9+ on the machine running the CLI.
- For `resolve-duplicate-contact-alerts`: the `sf` CLI authenticated to the `prod` org alias
  (`npm install -g @salesforce/cli && sf org login web --alias prod`), and the `salesforce-prod`
  MCP server registered separately (see that skill's README). `setup-companion-tools` can install
  and guide you through both — ask to set up `salesforce-prod`, or run its `sf-cli-guidance`
  subcommand directly for OS-specific, sudo-safe install instructions.
