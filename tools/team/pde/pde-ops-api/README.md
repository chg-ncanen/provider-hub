# PDE Ops API

Shared Python library for JSM Ops and Email operations. Used by the `pde-jsm` MCP server and `resolve-duplicate-contact-alerts` skill.

## Contents

- **`jsm/`** — Jira Service Management operations
  - `JSMOpsAPI` — Interface to JSM alerts, rules, projects
  - `JiraServiceManagement` — JSM project queries and alert management
  - Query, create, update, close JSM alerts
  
- **`email/`** — Email utilities
  - `EmailTool` — IMAP-based email search and operations
  - Search email archives by subject, sender, date
  
- **`jsmreport/`** — Alert reporting and metrics
  - Stats generation and alert summaries
  - CSV export utilities

## Installation

This is a shared library — add to your PYTHONPATH or install via pip in development mode:

```bash
cd provider-hub/tools/team/pde/pde-ops-api
pip install -e .
```

Or install the parent pde-jsm MCP/skill first (it depends on this):

```bash
cd provider-hub/mcp/team/pde/pde-jsm
pip install -r requirements.txt
```

## Configuration

Set environment variables for API access:

```bash
export ATLASSIAN_EMAIL=your-email@example.com
export ATLASSIAN_API_TOKEN=your-token-here
export ATLASSIAN_CLOUD_ID=cloud-instance-id  # Optional: auto-detected if missing

# For email features:
export EMAIL_USERNAME=your-email@example.com
export EMAIL_PASSWORD=your-app-password      # Not your user password
```

Or set in `.env`:
```bash
ATLASSIAN_EMAIL=...
ATLASSIAN_API_TOKEN=...
ATLASSIAN_CLOUD_ID=...
EMAIL_USERNAME=...
EMAIL_PASSWORD=...
```

## Usage

### JSM Operations

```python
from api.jsm.client import JSMOpsAPI

# Initialize
api = JSMOpsAPI.from_env()

# List alerts
alerts = api.list_alerts(status="open", priority="P1")

# Get alert details
alert = api.get_alert("alert-id-here")

# Acknowledge alert
api.acknowledge_alert("alert-id-here", note="Working on it")

# Close alert
api.close_alert("alert-id-here", note="Resolved")
```

### Email Search

```python
from api.email.email_tool import EmailTool

# Initialize
email = EmailTool()

# Search
messages = email.find_emails(
    subject="alert topic",
    sender="notifications@example.com",
    since="01-Jul-2026"
)
```

### Configuration

The library reads `app_config.json` for JSM settings:
- `cloudId` — Atlassian cloud instance
- `timeout_seconds` — Request timeout
- `max_retries` — Retry count for failed requests
- `jql_filters` — Predefined JQL query templates

## Related

- `mcp/team/pde/pde-jsm/` — MCP server exposing these APIs as tools
- `ai-skills/team/pde/resolve-duplicate-contact-alerts/` — Skill using JSM operations

## Dependencies

See `requirements.txt` in the parent mcp directory.

Key packages:
- `requests` — HTTP client
- `python-dotenv` — Env file loading
- `mcp` — Model Context Protocol (for MCP server integration)

## Contributing

When adding new API methods:
1. Add method to the appropriate class (`JSMOpsAPI`, `EmailTool`, etc.)
2. Document usage in this README
3. Update MCP server tools if exposing as a new capability
4. Add tests if applicable
