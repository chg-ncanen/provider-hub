# Resolve Duplicate Contact Alerts

AI skill for resolving JSM duplicate contact alerts by verifying Salesforce contact existence.

## Overview

This skill processes open JSM "More than one contact found..." alerts and automatically resolves those where the duplicate issue no longer exists in Salesforce prod.

## Dependencies

### Required MCPs

1. **`pde-jsm`** (this repo)
   - Available at: `../../../mcp/team/pde/pde-jsm/`
   - Provides: Alert listing, details, notes, closing, and email search
   - Setup: See `../../../mcp/team/pde/pde-jsm/README.md`

2. **`salesforce-prod`** (built-in Copilot CLI)
   - Provides: SOQL queries against Salesforce prod
   - Prerequisite: `sf` CLI authenticated to the `prod` org alias

### Environment

- `ATLASSIAN_EMAIL` and `ATLASSIAN_API_TOKEN` in `.env`
- Optional: `EMAIL_USERNAME` / `EMAIL_PASSWORD` for email validation
- `sf` CLI authenticated to `prod` org

## Running

### Quick Start

Fastest approach—run the Python script:

```bash
# From provider-hub root or any project importing this skill:
python ai-skills/team/pde/resolve-duplicate-contact-alerts/run.py

# Dry run (default — no changes):
python ai-skills/team/pde/resolve-duplicate-contact-alerts/run.py --dry-run

# Live mode (add notes and close resolved alerts):
python ai-skills/team/pde/resolve-duplicate-contact-alerts/run.py --live
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
- **"Not authenticated to prod"** — Run `sf org authenticate org_name:prod` first
- **"requests package not installed"** — Install MCP dependencies: `pip install -r ../../../mcp/team/pde/pde-jsm/requirements.txt`

## Files

- `SKILL.md` — Full technical specification for manual workflow
- `run.py` — Automated Python script (preferred; handles parallelization and batching)
