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

## Installing

```bash
# Claude Code
/plugin marketplace add https://github.com/chghealthcare/provider-hub.git
/plugin install pde@provider-hub

# Copilot CLI
copilot plugin marketplace add https://github.com/chghealthcare/provider-hub.git
copilot plugin install pde@provider-hub
```

Either way, a `SessionStart` hook (`scripts/bootstrap-venv.sh`) creates a venv under
`${CLAUDE_PLUGIN_ROOT}/.venv` and installs `mcp-servers/pde-jsm/requirements.txt` into it, only
reinstalling when that file changes.

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
