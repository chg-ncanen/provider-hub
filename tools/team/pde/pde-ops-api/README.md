# PDE Ops API

Shared Python library for JSM Ops and Email operations. Used by the `pde-mcp` MCP server and `resolve-duplicate-contact-alerts` skill.

## Contents

- **`jsm/`** — Jira Service Management operations
  - `JSMOpsAPI` — Interface to JSM alerts, rules, projects
  - `JiraServiceManagement` — JSM project queries and alert management
  - Query, create, update, close JSM alerts
  
- **`mail/`** — Email utilities
  - `EmailTool` — IMAP-based email search and operations
  - Search email archives by subject, sender, date
  - (named `mail/`, not `email/` — a directory literally named `email` shadows Python's stdlib `email` package for anything that lands on `sys.path` near it)

## Installation

This is a real pip package (`pde-ops-api`), importable as `api` (matching its existing internal `api.jsm.*` / `api.mail.*` imports — see `pyproject.toml`'s `package-dir` mapping):

```bash
cd provider-hub/tools/team/pde/pde-ops-api
pip install -e .
```

The `pde-mcp` MCP server (`provider-hub/plugins/pde/mcp-servers/pde-mcp/`) depends on this package via its `requirements.txt` rather than bundling a copy — install that instead to get both:

```bash
cd provider-hub/plugins/pde/mcp-servers/pde-mcp
pip install -r requirements.txt
```

## Configuration

Set environment variables for API access:

```bash
export ATLASSIAN_EMAIL=your-email@example.com
export ATLASSIAN_API_TOKEN=your-token-here

# For email features:
export EMAIL_USERNAME=your-email@example.com
export EMAIL_PASSWORD=your-app-password      # Not your user password
```

Or set in `.env`:
```bash
ATLASSIAN_EMAIL=...
ATLASSIAN_API_TOKEN=...
EMAIL_USERNAME=...
EMAIL_PASSWORD=...
```

`ATLASSIAN_CLOUD_ID` is not an env var — `AppConfig` only reads it from `app_config.json` (see
`jsm/config.py`), where it already has a working default.

## Usage

### JSM Operations

```python
from api.jsm.client import JSMOpsAPI

# Initialize (reads app_config.json + ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN from the environment)
api = JSMOpsAPI.from_config_file()

# List alerts
alerts = api.list_alerts(status="open", priority="P1")

# Get alert details
alert = api.get_alert("alert-id-here")

# Acknowledge alert
api.acknowledge("alert-id-here", note="Working on it")

# Close alert
api.close("alert-id-here", note="Resolved")
```

### Email Search

```python
from api.mail.email_tool import EmailTool

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

### CLI

```bash
python -m cli list --profile pde --status open
python -m cli list-closed --profile pde --since-days 2
python -m cli export-csv --output ./out/alerts.csv --start 2026-06-08T09:00 --end 2026-07-02T09:00
```

`.env` is discovered by searching upward from wherever you invoke `python -m cli`, not from
this package's own location — so run it from `plugins/pde/mcp-servers/pde-mcp/` (where this
plugin's `.env` actually lives) or export the credentials into your shell first. Running it
from `pde-ops-api/` itself won't find that `.env`, since it isn't an ancestor directory.

## Running tests

```bash
cd provider-hub/tools/team/pde/pde-ops-api
pip install -e .
python -m unittest discover -s tests -t . -q
```

(Not `pytest`: the `package-dir = {"api" = "."}` remap above means this directory's own
`__init__.py` *is* `api/__init__.py`, which trips pytest's rootdir-package detection into
trying to import it directly and failing with a relative-import error. `unittest discover`
doesn't have that failure mode.)

## Related

- `plugins/pde/mcp-servers/pde-mcp/` — MCP server exposing these APIs as tools
- `plugins/pde/skills/resolve-duplicate-contact-alerts/` — Skill using JSM operations

## Dependencies

See `requirements.txt` in `plugins/pde/mcp-servers/pde-mcp/`.

Key packages:
- `requests` — HTTP client
- `python-dotenv` — Env file loading

## Contributing

When adding new API methods:
1. Add method to the appropriate class (`JSMOpsAPI`, `EmailTool`, etc.)
2. Document usage in this README
3. Update MCP server tools if exposing as a new capability
4. Add tests if applicable
