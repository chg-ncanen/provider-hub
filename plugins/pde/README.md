# pde plugin

Claude Code plugin bundling the PDE team's JSM tooling, installable from any project.

## Contents

- **`mcp-servers/pde-jsm/`** — MCP server exposing JSM alert operations (`list_alerts`, `get_alert`,
  `acknowledge_alert`, `add_alert_note`, `close_alert`, `find_emails`, `send_email`) plus project
  skill discovery. Depends on the `pde-ops-api` library (`tools/team/pde/pde-ops-api` in this repo)
  via `requirements.txt` rather than bundling a copy — see that library's README.
- **`skills/resolve-duplicate-contact-alerts/`** — Skill that resolves duplicate-contact JSM alerts
  using the MCP server above plus the `salesforce-prod` MCP (a separate, Copilot-CLI-only extension;
  no equivalent is bundled here).

## Installing

```
/plugin marketplace add <path-or-url-to-provider-hub>
/plugin install pde@provider-hub
```

On first enable, Claude Code prompts for `ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN` (required) and,
optionally, `EMAIL_USERNAME` / `EMAIL_PASSWORD` for `find_emails` (see `.claude-plugin/plugin.json`'s
`userConfig`). A `SessionStart` hook (`scripts/bootstrap-venv.sh`) creates a venv under
`${CLAUDE_PLUGIN_DATA}` and installs `mcp-servers/pde-jsm/requirements.txt` into it, only reinstalling
when that file changes.

## Prerequisites

- Python 3.9+ on the machine running Claude Code.
- For `resolve-duplicate-contact-alerts`: the `sf` CLI authenticated to the `prod` org alias
  (`npm install -g @salesforce/cli && sf org authenticate org_name:prod`), and the `salesforce-prod`
  Copilot CLI extension if you're driving the skill from Copilot CLI instead of Claude Code.
