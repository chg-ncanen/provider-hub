# pde plugin

Plugin bundling the PDE team's JSM tooling, installable from any project — works with both
Claude Code and GitHub Copilot CLI, which both install from the same `.claude-plugin/` manifest.

## Contents

- **`mcp-servers/pde-jsm/`** — MCP server exposing JSM alert operations (`list_alerts`, `get_alert`,
  `acknowledge_alert`, `add_alert_note`, `close_alert`, `find_emails`, `send_email`) plus project
  skill discovery. Depends on the `pde-ops-api` library (`tools/team/pde/pde-ops-api` in this repo)
  via `requirements.txt` rather than bundling a copy — see that library's README.
- **`skills/resolve-duplicate-contact-alerts/`** — Skill that resolves duplicate-contact JSM alerts
  using the MCP server above plus `salesforce-prod`, a separate MCP server (the `@salesforce/mcp`
  npm package) not bundled here — see that skill's README for how to register it.
- **`launch-darkly`** — LaunchDarkly's own remote MCP server (`mcp.launchdarkly.com`), bundled
  directly in `.mcp.json` since it's a plain PDE dependency with no official Claude/Copilot plugin of
  its own. No static credentials to configure — it authenticates via an interactive OAuth prompt the
  first time it connects.
- **`skills/setup-companion-tools/`** — an opt-in skill (invoke it by asking to set up/connect
  companion tools) for installing Grafana, LogRocket, Atlassian, and `salesforce-prod` one at a
  time. Deliberately *not* automatic (no `SessionStart` hook does this) — those aren't dependencies
  of anything in this plugin, and installing `pde` shouldn't silently pull in other teams'/vendors'
  plugins without you choosing to.

## Installing

```bash
# Claude Code
/plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
/plugin install pde@provider-hub

# Copilot CLI
copilot plugin marketplace add https://github.com/chg-ncanen/provider-hub.git
copilot plugin install pde@provider-hub
```

**Then start a new session** (close and reopen) — installing alone isn't enough. The dependency
setup below runs via a `SessionStart` hook, which only fires at an actual session boundary (new
session, `--resume`/`--continue`, `/clear`, or compaction); `/plugin install` and `/reload-plugins`
do *not* trigger it. Until that hook runs once, `pde-jsm` can't launch — its command points at a
venv that doesn't exist yet.

That hook (`scripts/bootstrap-deps.sh`) creates a venv under `${CLAUDE_PLUGIN_ROOT}/.venv` and
installs `mcp-servers/pde-jsm/requirements.txt` into it, only reinstalling when that file changes.
It also best-effort installs the `sf` CLI (via `npm install -g @salesforce/cli`) if
`resolve-duplicate-contact-alerts` needs it and it's missing — but it can't authenticate `sf` for you
(that's an interactive browser login); it just tells you to run `sf org login web --alias prod` if
that alias isn't set up yet. It also never uses `sudo`: if the global npm prefix isn't writable, the
install just fails with a clear message pointing at the standard sudo-free fix (a user-owned npm
prefix), rather than hanging or silently doing nothing.

**Credentials** (`ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN`, required; `EMAIL_USERNAME` /
`EMAIL_PASSWORD`, optional for `find_emails`):
- Claude Code prompts for these on first enable (`.claude-plugin/plugin.json`'s `userConfig`) and the
  hook writes them to `mcp-servers/pde-jsm/.env`.
- Copilot CLI has no equivalent prompt — create that `.env` yourself (copy
  `mcp-servers/pde-jsm/.env.example`) after installing.

## Prerequisites

- Python 3.9+ on the machine running the CLI.
- For `resolve-duplicate-contact-alerts`: the `sf` CLI authenticated to the `prod` org alias
  (`npm install -g @salesforce/cli && sf org authenticate org_name:prod`), and the `salesforce-prod`
  MCP server registered separately (see that skill's README).
