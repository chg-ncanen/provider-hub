# PDE JSM MCP Server

Model Context Protocol server for PDE JSM Ops alert management.

## Overview

This MCP exposes JSM (Jira Service Management) alert operations as tools for autonomous agents and Copilot. It provides:

- `list_alerts` — Query open/acknowledged alerts with filtering
- `get_alert` — Fetch detailed alert with notes history
- `acknowledge_alert` — Acknowledge an alert as being actively worked
- `add_alert_note` — Add operator notes to an alert
- `close_alert` — Resolve and close an alert
- `find_emails` — Search email archive for notification evidence

## Setup

### Prerequisites

- **Python 3.9+**
  - Verify: `python --version`

- **Atlassian API credentials**
  - Email + API token
  - Generate token at: https://id.atlassian.com/manage-profile/security/api-tokens

- **Optional: Email credentials** (for `find_emails`)
  - IMAP server credentials (usually your email + app password, not user password)

- **Optional: Salesforce CLI** (for use with `resolve-duplicate-contact-alerts` skill)
  - Install: `npm install -g @salesforce/cli`
  - Authenticate: `sf org authenticate org_name:prod`
  - Verify: `sf org list --all`

### Installation

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure environment:
   ```bash
   cp .env.example .env
   # Edit .env with your credentials:
   # - ATLASSIAN_EMAIL
   # - ATLASSIAN_API_TOKEN
   # - ATLASSIAN_CLOUD_ID (optional, auto-detected if omitted)
   # - EMAIL_USERNAME / EMAIL_PASSWORD (optional, for email checks)
   ```

3. Verify config:
   ```bash
   cat app_config.json  # Review alert filter query, timeout, retry settings
   ```

## Running the MCP Server

### As part of the `pde` Claude Code plugin (recommended)

Install via `/plugin install pde@provider-hub` — see `../../README.md` (the plugin root). Claude Code
handles the venv, `${CLAUDE_PLUGIN_ROOT}`-relative paths, and credential prompts for you.

### Locally (stdio transport)
```bash
python app.py
```

The server runs on stdin/stdout.

### Example Copilot CLI Config

Copilot CLI has no plugin system, so register this server directly in your project's
`.copilot-config.json`:
```json
{
  "mcpServers": {
    "pde-jsm": {
      "command": "python",
      "args": ["/path/to/provider-hub/plugins/pde/mcp-servers/pde-jsm/app.py"]
    }
  }
}
```

## Deployment (Docker)

Build and run as a containerized service:
```bash
docker build -t pde-jsm-mcp .
docker run -e ATLASSIAN_EMAIL=... -e ATLASSIAN_API_TOKEN=... pde-jsm-mcp
```

(Dockerfile coming soon)

## Tools Documentation

### list_alerts
Query JSM alerts with optional filtering by status, priority, service.

```python
# Example: list open P1 PDE alerts
list_alerts(status="open", priority="P1", service="PDE")
```

### get_alert
Fetch full alert details including notes history.

```python
get_alert(alert_id="abc123def456-1234567890")
```

### acknowledge_alert
Mark an alert as being actively investigated.

```python
acknowledge_alert(alert_id="abc123def456-1234567890", note="Working on this now")
```

### add_alert_note
Add an operator note to the alert's activity log.

```python
add_alert_note(alert_id="abc123def456-1234567890", note="Found root cause: ...")
```

### close_alert
Resolve and close an alert.

```python
close_alert(alert_id="abc123def456-1234567890", note="Verified resolved in prod.")
```

### find_emails
Search email archive for alert notifications.

```python
find_emails(subject="Possible provider merge needed", since="01-Jul-2026")
```

## Related Skills & Tools

- `../../skills/resolve-duplicate-contact-alerts` — Uses this MCP to process duplicate contact alerts
- `tools/team/pde/pde-ops-api` (repo root) — Shared API layer for JSM operations; a `requirements.txt`
  dependency of this server, not a bundled copy

## Troubleshooting

- **"Missing required credentials"** — Check `.env` file has ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN
- **"requests package not installed"** — Run `pip install -r requirements.txt`
- **"Connection timeout"** — Check Atlassian cloud ID in `app_config.json`, increase timeout_seconds if needed

## Support

See project README for general support and contribution guidelines.
