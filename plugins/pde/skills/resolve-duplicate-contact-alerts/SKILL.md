---
name: resolve-duplicate-contact-alerts
description: >-
    Resolves open JSM alerts of type "More than one contact found..." by checking
    whether duplicates still exist in Salesforce prod. Closes alerts where no true
    duplicate remains. Use when the user asks to resolve, process, or run duplicate
    contact alerts, or mentions "duplicate contact alert resolution".
user-invocable: true
---

# Resolve Duplicate Contact Alerts

## Overview

This skill processes open JSM alerts of type **"More than one contact found for ... Notify Data Governance"**. It checks whether duplicates still exist in Salesforce prod and automatically closes alerts where the issue is already resolved.

Always confirm whether to run in **dry run** (default) or **live** mode before doing any work.

---

## Script (preferred — much faster)

A Python script at `run.py` in this directory automates all steps. **Use this instead of manual MCP tool calls** whenever possible — it parallelises JSM detail fetches, batches the Salesforce query, and processes all alerts in one pass.

**Prerequisites:** `mcp-servers/pde-mcp/.env` with `ATLASSIAN_EMAIL` + `ATLASSIAN_API_TOKEN`, optional `EMAIL_*` vars for email check, and `sf` CLI authenticated to the `prod` org alias. This script runs directly, not through `.mcp.json`, so it never sees Claude Code's `userConfig` substitution the way `pde-mcp` itself does — on Claude Code, the plugin's `SessionStart` hook mirrors `userConfig` into this `.env` for exactly this reason, so it should just be there after `/plugin configure pde@provider-hub` + a session restart. On Copilot CLI (no `userConfig`), create it by hand.

`run.py` checks all of the above itself before doing any real work — it prints exactly what's missing (credentials, `sf` CLI not installed, or not authenticated to `prod`) and exits, rather than failing partway through with a traceback. If it reports missing credentials, fall back to manual `pde-mcp` MCP tool calls instead (those get credentials straight from `userConfig` on Claude Code regardless of this script) — that fallback can't cover a missing `sf` CLI, though, since that's this script's own direct dependency for the Salesforce query.

```bash
# from this skill's own directory (its "Base directory" shown when invoked)

# dry run (default — no changes, just report)
python run.py

# live — add notes and close resolved alerts
python run.py --live
```

After the script runs, review the printed summary table and share it with the user. Only fall back to manual MCP tool calls when the script fails or needs debugging.

---

## Steps

### 1. Fetch open duplicate contact alerts

Use the `pde-mcp` MCP to list all open alerts. Filter to only those whose message contains `"More than one contact found"`.

### 2. Fetch alert details, emails, and Salesforce username in parallel

Call `get_alert` for **all alerts at once** (parallel tool calls), using the full `id` field from the list response (e.g. `1b24df9e-8601-4640-8488-7a498d54a61a-1784207488560`) — **not** `tinyId`, which returns 404. In the same batch, also fire:
- A `salesforce-prod` `get_username` call with `defaultTargetOrg: false` to get the authenticated username for use in step 3
- A single `find_emails` call — do not wait for Salesforce results first:

- Search subject: `Possible provider merge needed`
- Use 1 day before the `created_at` of the earliest alert from step 1 as the `since` date — emails will always be sent after their alert is created, and the 1-day buffer accounts for any clock skew

### 3. Batch Salesforce query

Once `get_alert` results are back, read contact IDs directly from `extraProperties.contactIds` (a JSON array string, e.g. `["003PF00000eQyPhYAK","003PF00000mo4G7YAI"]`) — do **not** parse the description text. The sub-brand codes (WBY, GMI, CHS, etc.) come from the description lines in this format:
```
https://chg.my.salesforce.com/<CONTACT_ID> - <BRAND> - <Name>
```

Collect every contact ID across all alerts into a **single** `salesforce-prod` MCP `run_soql_query`, using the username from step 2:
```sql
SELECT Id, Name FROM Contact WHERE Id IN ('<id1>', '<id2>', ...<all IDs from all alerts>)
```
Match results back to their alerts by ID. Only contacts returned still exist.

### 4. Apply brand rules

Some sub-brands belong to the same parent brand and are treated as **brand groups**. A provider should have at most **one contact across the entire group** — having contacts in two different sub-brands within the same group is still a duplicate. A duplicate exists if 2+ contacts exist within the same brand group (regardless of whether the sub-brands differ).

Known brand groups:
- **GMI / GMD** (Global Medical Staffing)
- **WMS / WBY** (Weatherby)
- **CHS / CHA** (CompHealth)

For brands not in any group above, a duplicate exists if 2 or more contacts from the same brand still exist.

### 5. Decision

- ✅ **1 contact survives** → In live mode: add a note to the alert listing the surviving contact and why it's resolved, then close it. In dry run: report what would be closed.
- ⚠️ **2+ contacts survive (duplicate still exists)** → Check whether a notification email has already been sent (see step 5a). Do nothing to the alert. Note it for the summary report.
- ❓ **0 contacts survive** → All referenced contacts have been deleted from Salesforce. Do **not** close automatically — this is anomalous and requires manual review. Add a note and flag in the summary report.

**Note format when closing (1 survivor):**
```
Verified in Salesforce prod. Surviving contact(s): <ID> - <Brand> - <Name>. No duplicate remains. Closing as resolved.
```

**Note format for 0-survivor anomaly:**
```
Verified in Salesforce prod. No surviving contacts found for any referenced ID. Requires manual review — not closing automatically.
```

#### 5a. Email check for unresolved alerts

Use the results from the `find_emails` call made in step 2 (already fetched). The email bodies contain Salesforce contact IDs and names — **not** the provider's email address. Match emails to alerts by checking whether any of the alert's contact IDs appear in the email body.

If no match is found in the pre-fetched results, do a targeted follow-up `find_emails` call to confirm — the pre-fetched result set may be capped:
- Search subject: `Possible provider merge needed`
- Search body/query: one of the alert's contact IDs (e.g. `003PF00000eQyPhYAK`)

Report in the summary whether an email was found or not:
- 📧 **Email sent** — Data Governance has been notified
- 📭 **No email found** — Data Governance has NOT been notified (may need manual follow-up)

### 6. Summary report

After processing all alerts, output a table:

| Alert | Message | Action Taken |
|---|---|---|
| #12345 | More than one contact found for x@y.com in weatherby | Closed — 1 contact remains |
| #12346 | More than one contact found for a@b.com in gmedical | Skipped — 2 contacts still exist (needs Data Governance) |

---

## Invocation

- **Dry run (default):** "dry run duplicate contact alert resolution" or "resolve duplicate contact alerts"
- **Live mode:** "run duplicate contact alert resolution" or "resolve duplicate contact alerts for real"

**Always ask the user to confirm the mode (dry run or live) before doing any work**, even if the invocation implies a default. Use a multiple-choice prompt offering both options.

---

## MCPs Required

- `pde-mcp` — for fetching alerts, adding notes, closing alerts, and searching emails (`find_emails`)
- `salesforce-prod` — for querying contact existence
