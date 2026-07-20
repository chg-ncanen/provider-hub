# Resolve Duplicate Contact Alerts

AI skill for resolving JSM duplicate contact alerts by verifying Salesforce contact existence.

## Overview

This skill processes open JSM "More than one contact found..." alerts and automatically resolves those where the duplicate issue no longer exists in Salesforce prod.

## Dependencies

### Required MCPs

1. **`pde-mcp`** (bundled in this same plugin)
   - Available at: `../../mcp-servers/pde-mcp/`
   - Provides: Alert listing, details, notes, closing, and email search
   - Setup: See `../../mcp-servers/pde-mcp/README.md`

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

- `ATLASSIAN_EMAIL` and `ATLASSIAN_API_TOKEN` in `../../mcp-servers/pde-mcp/.env` — `run.py` is
  invoked directly, not through `.mcp.json`, so it never receives Claude Code's `userConfig`
  substitution the way the `pde-mcp` MCP server process does. On Claude Code, the plugin's
  `SessionStart` hook mirrors `userConfig` into this `.env` for exactly this reason, so it should
  already be there after `/plugin configure pde@provider-hub` + a session restart. On Copilot CLI
  (no `userConfig`), create it by hand. If `run.py` still reports missing credentials, fall back to
  manual `pde-mcp` MCP tool calls instead, which get credentials straight from `userConfig`.
- Optional: `EMAIL_USERNAME` / `EMAIL_PASSWORD` for email validation
- **`sf` CLI** — Salesforce CLI (required): `run.py` calls it directly (`sf data query`) at step 3,
  it does not go through the `salesforce-prod` MCP server above (that's only used by the manual
  workflow's MCP tool calls).
  - Install: `npm install -g @salesforce/cli`
  - Authenticate: `sf org login web --alias prod`
  - Verify: `sf org list --all` should show `prod` as available

`run.py` checks all of the above itself before doing any work (see Troubleshooting) — it reports
exactly what's missing and exits cleanly rather than failing partway through.

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

`run.py` runs a dependency check before doing anything else and prints exactly what's wrong — the
messages below are what to do about each one it can report:

- **Missing ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN** — see "Environment & Tools" above (Claude Code:
  restart the session after `/plugin configure`; Copilot CLI: create `.env` by hand)
- **`sf` CLI not found on PATH** — Install: `npm install -g @salesforce/cli`
- **`sf` CLI has no 'prod' org alias** — Run `sf org login web --alias prod` (interactive browser
  login, can't be automated)
- **"requests package not installed"** — Install MCP dependencies: `pip install -r ../../mcp-servers/pde-mcp/requirements.txt`

## Files

- `SKILL.md` — Full technical specification for manual workflow
- `run.py` — Automated Python script (preferred; handles parallelization and batching)
