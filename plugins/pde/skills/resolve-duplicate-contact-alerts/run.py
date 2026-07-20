#!/usr/bin/env python3
"""
Resolve duplicate contact JSM alerts.

Fetches open "More than one contact found" alerts, verifies which contacts
still exist in Salesforce prod, then closes resolved alerts and reports on
the rest.

Usage:
    python run.py [--dry-run|--live]

Requires:
    - pde-ops-api installed (pip install -e tools/team/pde/pde-ops-api, or via
      the pde-mcp MCP server's requirements.txt, which depends on it)
    - ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN in .env (or environment)
    - EMAIL_USERNAME / EMAIL_PASSWORD in .env for email checks (optional)
    - `sf` CLI authenticated to the 'prod' org alias
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# .env and app_config.json are shared with the pde-mcp MCP server this skill uses.
# __file__ is plugins/pde/skills/resolve-duplicate-contact-alerts/run.py, so
# parents[2] is plugins/pde — parents[1] (skills/) was one level too shallow.
_MCP_SERVER_DIR = Path(__file__).resolve().parents[2] / "mcp-servers" / "pde-mcp"

try:
    from dotenv import load_dotenv
    load_dotenv(_MCP_SERVER_DIR / ".env")
except Exception:
    pass

from api.jsm.client import JSMOpsAPI
from api.jsm.config import AppConfig
from api.mail.email_tool import EmailTool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DUPLICATE_CONTACT_MESSAGE = "More than one contact found"
SF_ORG = "prod"

# Brand groups: brands in the same group are treated as one.
# A provider having contacts in two different sub-brands within a group is
# still a duplicate.
BRAND_GROUPS: dict[str, str] = {
    "GMI": "GMS",
    "GMD": "GMS",
    "WMS": "WEATHERBY",
    "WBY": "WEATHERBY",
    "CHS": "COMPHEALTH",
    "CHA": "COMPHEALTH",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def brand_group(brand: str) -> str:
    """Normalise a brand code to its parent group (or itself if ungrouped)."""
    return BRAND_GROUPS.get(brand.upper().strip(), brand.upper().strip())


def sf_query(soql: str) -> list[dict]:
    """Run a SOQL query against Salesforce prod and return the records list."""
    result = subprocess.run(
        ["sf", "data", "query", "--query", soql, "--target-org", SF_ORG, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Salesforce CLI returned non-JSON: {result.stdout[:300]}") from exc

    if data.get("status") != 0:
        raise RuntimeError(
            f"Salesforce query failed: {data.get('message') or result.stderr[:300]}"
        )
    return data.get("result", {}).get("records", [])


def parse_contact_ids(detail: dict) -> list[str]:
    """
    Extract contact IDs from alert detail.
    Primary source: extraProperties.contactIds (JSON array string).
    Fallback: regex scan of description.
    """
    extra = detail.get("extraProperties") or {}
    raw = extra.get("contactIds")
    if raw:
        try:
            ids = json.loads(raw) if isinstance(raw, str) else list(raw)
            if ids:
                return ids
        except Exception:
            pass
    # Fallback: 18-char Salesforce Contact IDs start with '003'
    description = detail.get("description") or ""
    return list(dict.fromkeys(re.findall(r"003[A-Za-z0-9]{15}", description)))


def parse_brand(description: str, contact_id: str) -> str:
    """
    Extract brand from a description line of the form:
        https://chg.my.salesforce.com/<ID> - BRAND - Name
    """
    for line in description.splitlines():
        if contact_id in line:
            parts = line.split(" - ")
            if len(parts) >= 2:
                return parts[1].strip()
    return ""


def imap_date(dt: datetime) -> str:
    """Format datetime as DD-Mon-YYYY for IMAP SINCE."""
    return dt.strftime("%d-%b-%Y")


def has_duplicate(survivors: list[dict]) -> bool:
    """
    Return True if 2+ contacts survive within the same brand group.
    Each survivor dict has keys: id, name, brand, group.
    """
    groups: dict[str, int] = {}
    for s in survivors:
        groups[s["group"]] = groups.get(s["group"], 0) + 1
    return any(count >= 2 for count in groups.values())


def email_mentions_any(emails: list[dict], contact_ids: list[str]) -> bool:
    """Return True if any fetched email body contains at least one contact ID."""
    for em in emails:
        body = em.get("body") or ""
        if any(cid in body for cid in contact_ids):
            return True
    return False


def check_dependencies(cfg: AppConfig) -> tuple[list[str], list[str]]:
    """Preflight check for everything this script actually needs to run.

    Returns (problems, notes): problems are blocking (script can't do real
    work without them); notes are informational (e.g. optional email check
    being skipped) and don't stop the run. Checked here rather than letting
    the script fail wherever the first missing dependency happens to bite —
    JSM credentials would only surface at Step 1, a missing/unauthenticated
    `sf` CLI wouldn't surface until Step 3 (as an uncaught FileNotFoundError
    or RuntimeError), both as unhelpful mid-run tracebacks.
    """
    problems: list[str] = []
    notes: list[str] = []

    try:
        cfg.validate_for_alert_fetch()
    except ValueError as exc:
        # Unlike pde-mcp itself (which gets credentials from .mcp.json's
        # ${user_config.*} substitution when Claude Code spawns it), this
        # script is invoked directly and never goes through that path. On
        # Claude Code, bootstrap-deps.sh's SessionStart hook mirrors userConfig
        # into mcp-servers/pde-mcp/.env specifically so this script has a
        # source too — so landing here on Claude Code usually means the hook
        # hasn't run yet this session (needs a session restart after
        # install/configure) or userConfig was never filled in. On Copilot CLI
        # (no userConfig, no hook mirroring) it means that .env was never
        # hand-created.
        problems.append(
            f"{exc}\n"
            f"    Credentials expected in {_MCP_SERVER_DIR / '.env'} (or your shell "
            "environment) and weren't found.\n"
            "    Claude Code: if you've already run '/plugin configure "
            "pde@provider-hub', restart the session so the SessionStart hook can "
            "mirror it into .env — configuring alone doesn't do it.\n"
            "    Otherwise: copy mcp-servers/pde-mcp/.env.example to that path and "
            "fill in ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN yourself.\n"
            "    Alternatively, skip this script and use the pde-mcp MCP tools "
            "directly (list_alerts, get_alert, etc.) — those get credentials "
            "straight from userConfig on Claude Code regardless of this script."
        )

    if not os.getenv("EMAIL_USERNAME") or not os.getenv("EMAIL_PASSWORD"):
        notes.append(
            "EMAIL_USERNAME/EMAIL_PASSWORD not set — the email-notification check "
            "(step 5a) will report 'no email found' for every unresolved duplicate, "
            "even if one was actually sent. Optional; set both in .env to enable it."
        )

    sf_path = shutil.which("sf")
    if not sf_path:
        problems.append(
            "`sf` CLI not found on PATH, but this script queries Salesforce prod "
            "directly via `sf data query` at step 3.\n"
            "    Install: npm install -g @salesforce/cli\n"
            "    Or run the setup-companion-tools skill and pick salesforce-prod, "
            "which checks/guides this for you."
        )
    else:
        try:
            result = subprocess.run(
                ["sf", "alias", "list", "--json"], capture_output=True, text=True, timeout=15,
            )
            data = json.loads(result.stdout)
            has_prod_alias = any(a.get("alias") == SF_ORG for a in data.get("result", []))
        except Exception as exc:
            # A parse/timeout failure here isn't proof of a missing alias —
            # don't block the run on a flaky check when step 3 already has
            # its own error handling for a genuine auth failure; just warn.
            notes.append(
                f"Couldn't verify whether `sf` is authenticated to '{SF_ORG}' "
                f"({exc}) — proceeding anyway; if step 3 fails with an auth "
                f"error, run 'sf org login web --alias {SF_ORG}'."
            )
        else:
            if not has_prod_alias:
                problems.append(
                    f"`sf` CLI has no '{SF_ORG}' org alias authenticated — run "
                    f"'sf org login web --alias {SF_ORG}' (interactive browser login, "
                    "can't be automated)."
                )

    return problems, notes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve duplicate-contact JSM alerts.",
        epilog=(
            "Dry run (default): report only, no mutations.\n"
            "Live: add notes to anomalous alerts and close resolved ones."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Report only, no changes (default)",
    )
    mode.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="Add notes and close resolved alerts in JSM",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run: bool = args.dry_run
    mode_label = "DRY RUN" if dry_run else "LIVE"

    print(f"\n{'='*70}")
    print(f"  Resolve Duplicate Contact Alerts  [{mode_label}]")
    print(f"{'='*70}\n")

    # -- Setup -------------------------------------------------------------
    cfg = AppConfig.from_env(config_path=str(_MCP_SERVER_DIR / "app_config.json"))

    print("Checking dependencies…")
    problems, notes = check_dependencies(cfg)
    for note in notes:
        print(f"  ⚠ {note}")
    if problems:
        print(f"\n{len(problems)} problem(s) found — can't continue:\n")
        for i, problem in enumerate(problems, start=1):
            print(f"{i}. {problem}\n")
        return 1
    print("  All good.\n")

    api = JSMOpsAPI(config=cfg)
    email_tool = EmailTool()

    # -- Step 1: list open duplicate contact alerts ----------------------------
    print("Step 1: Fetching open duplicate contact alerts…")
    list_result = api.list_alerts(
        query='responders:"PDE" AND status:open',
        text=DUPLICATE_CONTACT_MESSAGE,
        limit=100,
    )
    dup_alerts = [
        a for a in list_result.get("alerts", [])
        if DUPLICATE_CONTACT_MESSAGE in (a.get("message") or "")
    ]
    print(f"  Found {len(dup_alerts)} duplicate-contact alert(s).")
    if not dup_alerts:
        print("\nNothing to do.")
        return 0

    # Earliest alert date for email search window (1 day buffer)
    dates = []
    for a in dup_alerts:
        raw = a.get("createdAt") or a.get("created_at") or ""
        if raw:
            try:
                dates.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
            except Exception:
                pass
    earliest = min(dates) if dates else datetime.now(timezone.utc)
    email_since = imap_date(earliest - timedelta(days=1))

    # -- Step 2: parallel get_alert + email search ----------------------------
    print("\nStep 2: Fetching alert details and searching emails (parallel)…")
    alert_details: dict[str, dict] = {}
    emails: list[dict] = []

    with ThreadPoolExecutor(max_workers=min(len(dup_alerts) + 1, 20)) as ex:
        detail_futures = {
            ex.submit(api.get_alert, a["id"]): a["id"]
            for a in dup_alerts
        }
        email_future = ex.submit(
            email_tool.find_emails,
            subject="Possible provider merge needed",
            since=email_since,
            limit=100,
        )

        for fut in as_completed(list(detail_futures) + [email_future]):
            if fut is email_future:
                try:
                    emails = email_future.result().get("emails", [])
                    print(f"  Email search: {len(emails)} email(s) found since {email_since}.")
                except Exception as e:
                    print(f"  WARNING: email search failed: {e}")
            else:
                alert_id = detail_futures[fut]
                try:
                    alert_details[alert_id] = fut.result().get("alert") or {}
                except Exception as e:
                    print(f"  WARNING: failed to get details for {alert_id}: {e}")
                    alert_details[alert_id] = {}

    print(f"  Got details for {len(alert_details)}/{len(dup_alerts)} alert(s).")

    # -- Step 3: batch Salesforce query ----------------------------------------
    alert_contacts: dict[str, list[str]] = {}
    all_ids: set[str] = set()

    for a in dup_alerts:
        detail = alert_details.get(a["id"], {})
        contact_ids = parse_contact_ids(detail)
        alert_contacts[a["id"]] = contact_ids
        all_ids.update(contact_ids)

    if not all_ids:
        print("\nERROR: No contact IDs found in any alert. Cannot continue.")
        return 1

    print(f"\nStep 3: Querying Salesforce prod for {len(all_ids)} contact ID(s)…")
    id_clause = ", ".join(f"'{cid}'" for cid in sorted(all_ids))
    try:
        sf_records = sf_query(f"SELECT Id, Name FROM Contact WHERE Id IN ({id_clause})")
    except Exception as exc:
        # The preflight check already confirmed `sf` is installed and
        # authenticated to SF_ORG, so a failure here is something that
        # changed mid-run (expired session, network blip, bad query) rather
        # than a missing dependency — still worth failing cleanly instead of
        # an uncaught traceback.
        print(f"\nERROR: Salesforce query failed: {exc}")
        return 1
    surviving: dict[str, str] = {r["Id"]: r["Name"] for r in sf_records}
    print(f"  {len(surviving)} contact(s) still exist in Salesforce.")

    # -- Steps 4–5: process each alert -----------------------------------------
    print("\nStep 4–5: Applying brand rules and deciding actions…\n")

    summary: list[tuple[str, str, str]] = []

    for a in dup_alerts:
        alert_id = a["id"]
        tiny_id = a.get("tinyId", "?")
        message = (a.get("message") or a.get("title") or "")
        detail = alert_details.get(alert_id, {})
        description = detail.get("description") or a.get("description") or ""
        contact_ids = alert_contacts.get(alert_id, [])

        # Build survivor list with brand metadata
        survivors = []
        for cid in contact_ids:
            if cid in surviving:
                b = parse_brand(description, cid)
                survivors.append({
                    "id": cid,
                    "name": surviving[cid],
                    "brand": b,
                    "group": brand_group(b),
                })

        # --- Decision ---------------------------------------------------------
        if len(survivors) == 0:
            note = (
                "Verified in Salesforce prod. No surviving contacts found for any "
                "referenced ID. Requires manual review — not closing automatically."
            )
            action = "❓ ANOMALY — 0 contacts survive; requires manual review"
            if not dry_run:
                try:
                    api.add_note(alert_id, note)
                    action += " (note added)"
                except Exception as e:
                    action += f" (note FAILED: {e})"

        elif not has_duplicate(survivors):
            surviving_str = "; ".join(
                f"{s['id']} - {s['brand']} - {s['name']}" for s in survivors
            )
            note = (
                f"Verified in Salesforce prod. Surviving contact(s): {surviving_str}. "
                "No duplicate remains. Closing as resolved."
            )
            action = f"✅ CLOSE — {len(survivors)} contact(s) remain, no duplicate"
            if not dry_run:
                try:
                    api.add_note(alert_id, note)
                    api.close(alert_id)
                    action += " (closed)"
                except Exception as e:
                    action += f" (FAILED: {e})"
            else:
                action += " [would close]"

        else:
            # Duplicate still exists — check whether email was sent
            email_sent = email_mentions_any(emails, contact_ids)

            if not email_sent:
                # Targeted follow-up in case the initial batch fetch was capped:
                # search the body directly for one of this alert's contact IDs,
                # rather than repeating the same subject+since query with a
                # smaller limit (which can only ever be a subset of what the
                # initial fetch already covered).
                for cid in contact_ids:
                    try:
                        followup = email_tool.find_emails(
                            subject="Possible provider merge needed",
                            body_contains=cid,
                            limit=5,
                        )
                    except Exception:
                        continue
                    if email_mentions_any(followup.get("emails", []), contact_ids):
                        email_sent = True
                        break

            email_status = "📧 email sent" if email_sent else "📭 no email found"
            action = (
                f"⚠️  SKIP — {len(survivors)} contacts still exist ({email_status})"
            )

        print(f"  #{tiny_id}: {action}")
        summary.append((f"#{tiny_id}", message[:68], action))

    # -- Step 6: summary table -------------------------------------------------
    print(f"\n{'='*100}")
    print(f"{'Alert':<10} {'Message':<70} Action")
    print(f"{'-'*100}")
    for alert_col, msg_col, action_col in summary:
        print(f"{alert_col:<10} {msg_col:<70} {action_col}")
    print(f"{'='*100}")
    print(f"\nDone. Mode: {mode_label}. Processed {len(dup_alerts)} alert(s).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
