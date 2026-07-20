# Resolve Duplicate Contact Alerts

AI skill for resolving JSM duplicate contact alerts by verifying Salesforce contact existence.

## Overview

This skill processes open JSM "More than one contact found..." alerts and automatically resolves those where the duplicate issue no longer exists in Salesforce prod.

## Dependencies

### Required MCPs

1. **`pde-jsm`** (bundled in this same plugin)
   - Available at: `../../mcp-servers/pde-jsm/`
   - Provides: Alert listing, details, notes, closing, and email search
   - Setup: See `../../mcp-servers/pde-jsm/README.md`

2. **`salesforce-prod`** (a regular MCP server, not bundled with this plugin)
   - Provides: SOQL queries against Salesforce prod
   - Prerequisite: `sf` CLI authenticated to the `prod` org alias
   - **Easiest path**: run the `setup-companion-tools` skill (in this same plugin) and pick
     `salesforce-prod` — it registers the MCP server, checks whether `sf` is installed/authenticated,
     and gives OS-specific guidance for whichever step is still missing.
   - Or install manually (either CLI — this is the `@salesforce/mcp` npm package, not a
     Copilot-specific extension):
     ```bash
     # Check if installed
     copilot mcp list | grep salesforce-prod   # or: claude mcp list

     # If not found:
     copilot mcp add salesforce-prod -- npx -y @salesforce/mcp --orgs prod --toolsets orgs,data
     ```

### Environment & Tools

- `ATLASSIAN_EMAIL` and `ATLASSIAN_API_TOKEN` in `.env`
- Optional: `EMAIL_USERNAME` / `EMAIL_PASSWORD` for email validation
- **`sf` CLI** — Salesforce CLI (required)
  - Install: `npm install -g @salesforce/cli`
  - Authenticate: `sf org login web --alias prod`
  - Verify: `sf org list --all` should show `prod` as available

## Running

### Quick Start

Fastest approach—run the Python script:

```bash
# From this skill's own directory (or the equivalent path once installed as a plugin):
python run.py

# Dry run (default — no changes):
python run.py --dry-run

# Live mode (add notes and close resolved alerts):
python run.py --live
```

### Manual Workflow

Use this only if the script fails or needs debugging. See `SKILL.md` for the full step-by-step workflow using MCP tools directly.

## How It Works

1. **Fetch** open duplicate contact alerts from JSM
2. **Query** Salesforce prod to check which contacts still exist
3. **Apply** brand grouping rules to determine if a true duplicate remains
4. **Decide:** Close if resolved, skip if duplicate exists, flag anomalies
5. **Report** summary of all actions

Brand grouping ensures providers have at most one contact per brand group:
- GMI / GMD → Global Medical Staffing
- WMS / WBY → Weatherby
- CHS / CHA → CompHealth

## Integration

For autonomous agents or Copilot CLI, the `run.py` script is invoked as a subprocess. The MCP tools are referenced in the script but are invoked directly—they don't need separate registration.

## Troubleshooting

- **"No ATLASSIAN_EMAIL"** — Set `ATLASSIAN_EMAIL` and `ATLASSIAN_API_TOKEN` in `.env`
- **"sf: command not found"** — Install Salesforce CLI: `npm install -g @salesforce/cli`
- **"Not authenticated to prod"** — Run `sf org login web --alias prod` first
- **"requests package not installed"** — Install MCP dependencies: `pip install -r ../../mcp-servers/pde-jsm/requirements.txt`

## Files

- `SKILL.md` — Full technical specification for manual workflow
- `run.py` — Automated Python script (preferred; handles parallelization and batching)
