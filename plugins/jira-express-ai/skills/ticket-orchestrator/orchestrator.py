#!/usr/bin/env python3
"""
PDE AI Ticket Orchestrator

Stateless dispatcher: query Jira, check state files, launch worker sessions, exit.
Safe to run on a schedule (cron every 5-15 minutes).

Usage:
    cd /path/to/pde-ops-agent   # target project root — tickets/, repo clones live here
    python3 "$CLAUDE_PLUGIN_ROOT/skills/ticket-orchestrator/orchestrator.py"

Required env vars:
    ATLASSIAN_EMAIL       Atlassian account email
    ATLASSIAN_API_TOKEN   Atlassian API token

Optional env vars:
    REPOS_DIR             Directory containing repo clones (default: cwd)
    CLAUDE_PLUGIN_ROOT    This plugin's install location (set automatically by
                           Claude Code / Copilot CLI). Used to find this
                           script's sibling skill files regardless of cwd.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

CLOUD_ID = "e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2"
JIRA_BASE = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"

JQL = (
    'project = PDE'
    ' AND labels = "AI-Work"'
    ' AND statusCategory != Done'
    ' AND status != Cancelled'
    ' AND status != Released'
    ' AND status != Backlog'
)

ACTIONABLE_STATUSES = {"To Do", "In Discovery", "In Progress", "UAT Review"}
PICKUP_TIMEOUT_SECS = 30
ARCHIVE_MAX_DAYS = 7

# This plugin's install root — where the sibling skill files (SKILL.md for
# the worker, discovery, implementation, review, and merge agents) live.
# Claude Code and Copilot CLI both set CLAUDE_PLUGIN_ROOT (Copilot also sets
# the equivalent PLUGIN_ROOT / COPILOT_PLUGIN_ROOT) to this plugin's real
# install location, independent of the cwd the orchestrator is run from.
# Falls back to deriving it from this file's own location — orchestrator.py
# → ticket-orchestrator/ → skills/ → plugin root — for direct/local runs
# with no plugin env var set.
def _plugin_root() -> Path:
    env_root = (
        os.environ.get("CLAUDE_PLUGIN_ROOT")
        or os.environ.get("PLUGIN_ROOT")
        or os.environ.get("COPILOT_PLUGIN_ROOT")
    )
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parent.parent.parent


PLUGIN_ROOT = _plugin_root()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Jira helpers ──────────────────────────────────────────────────────────────

def _auth() -> tuple[str, str]:
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    if not email or not token:
        log.error("ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN must be set")
        sys.exit(1)
    return (email, token)


def _jira_get(path: str, auth, **params) -> dict:
    r = requests.get(
        f"{JIRA_BASE}{path}",
        auth=auth,
        headers={"Accept": "application/json"},
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def search_tickets(auth) -> list[dict]:
    r = requests.post(
        f"{JIRA_BASE}/search/jql",
        json={"jql": JQL, "fields": ["status", "assignee", "summary"], "maxResults": 100},
        auth=auth,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("issues", [])


def get_current_user(auth) -> dict:
    return _jira_get("/myself", auth)


def assign_ticket(key: str, account_id: str, auth) -> None:
    r = requests.put(
        f"{JIRA_BASE}/issue/{key}/assignee",
        json={"accountId": account_id},
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()

# ── State file ────────────────────────────────────────────────────────────────

def read_state(ticket_dir: Path) -> dict | None:
    f = ticket_dir / ".session.state"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def write_state(ticket_dir: Path, state: dict) -> None:
    (ticket_dir / ".session.state").write_text(json.dumps(state, indent=2))

# ── PID check ─────────────────────────────────────────────────────────────────

def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

# ── Session rename ────────────────────────────────────────────────────────────

def rename_old_session(key: str) -> None:
    """Rename any copilot session named <KEY> to <KEY>-archived-<date>."""
    state_root = Path.home() / ".copilot" / "session-state"
    if not state_root.exists():
        return
    archive_name = f"{key}-archived-{date.today()}"
    pattern = re.compile(rf"^name: {re.escape(key)}$", re.MULTILINE)
    for sid in state_root.iterdir():
        wy = sid / "workspace.yaml"
        if not wy.exists():
            continue
        content = wy.read_text()
        if pattern.search(content):
            content = re.sub(r"^name: .*$", f"name: {archive_name}", content, flags=re.MULTILINE)
            content = re.sub(r"^user_named: .*$", "user_named: false", content, flags=re.MULTILINE)
            wy.write_text(content)
            log.info(f"{key}: renamed old session → {archive_name}")
            break

# ── Cleanup ───────────────────────────────────────────────────────────────────

# ── Cleanup ───────────────────────────────────────────────────────────────────

def close_remote_artifacts(key: str, ticket_dir: Path, repos_dir: Path) -> None:
    """Close open PR, delete branch, and remove git worktree if review-context.md exists."""
    review_context = ticket_dir / "review-context.md"
    if not review_context.exists():
        return

    content = review_context.read_text()

    pr_match = re.search(r"\*\*PR:\*\*\s*#(\d+)", content)
    repo_match = re.search(r"\*\*Repo:\*\*\s*(\S+)", content)
    branch_match = re.search(r"\*\*Branch:\*\*\s*(\S+)", content)

    if not pr_match or not repo_match:
        log.warning(f"{key}: review-context.md found but missing PR/Repo — skipping remote cleanup")
        return

    pr_number = pr_match.group(1)
    repo = repo_match.group(1)
    branch = branch_match.group(1) if branch_match else None

    # Remove git worktree (ticket's isolated checkout)
    worktree_path = ticket_dir / repo
    main_clone = repos_dir / repo
    if worktree_path.exists() and main_clone.exists():
        try:
            subprocess.run(
                ["git", "-C", str(main_clone), "worktree", "remove", str(worktree_path), "--force"],
                check=True, timeout=30,
            )
            subprocess.run(
                ["git", "-C", str(main_clone), "worktree", "prune"],
                check=True, timeout=30,
            )
            log.info(f"{key}: removed worktree {worktree_path}")
        except Exception as e:
            log.warning(f"{key}: failed to remove worktree — {e}")

    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--repo", f"chghealthcare/{repo}", "--json", "state", "--jq", ".state"],
            capture_output=True, text=True, timeout=20,
        )
        pr_state = result.stdout.strip()
    except Exception as e:
        log.warning(f"{key}: could not check PR #{pr_number} state — {e}")
        return

    if pr_state == "OPEN":
        try:
            subprocess.run(
                ["gh", "pr", "close", pr_number, "--repo", f"chghealthcare/{repo}",
                 "--comment", f"🤖 Closing PR — ticket {key} has been archived (Done/Cancelled/Reset)."],
                check=True, timeout=20,
            )
            log.info(f"{key}: closed PR #{pr_number}")
        except Exception as e:
            log.warning(f"{key}: failed to close PR #{pr_number} — {e}")

    if branch:
        try:
            subprocess.run(
                ["gh", "api", f"repos/chghealthcare/{repo}/git/refs/heads/{branch}",
                 "-X", "DELETE"],
                check=True, timeout=20,
            )
            log.info(f"{key}: deleted branch {branch}")
        except Exception as e:
            log.warning(f"{key}: failed to delete branch {branch} — {e}")


def archive_ticket(key: str, ticket_dir: Path, archive_dir: Path, repos_dir: Path) -> None:
    """Close remote artifacts, move ticket_dir to a timestamped archive folder, rename copilot session."""
    close_remote_artifacts(key, ticket_dir, repos_dir)
    dest = archive_dir / f"{key}-{date.today()}"
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = archive_dir / f"{key}-{date.today()}-{suffix}"
    shutil.move(str(ticket_dir), str(dest))
    log.info(f"{key}: archived → {dest}")
    rename_old_session(key)


def cleanup_pass(tickets_dir: Path, archive_dir: Path, active_keys: set[str], repos_dir: Path) -> None:
    # Archive done tickets no longer in Jira results
    if tickets_dir.exists():
        for folder in tickets_dir.iterdir():
            if not folder.is_dir():
                continue
            key = folder.name
            if key in active_keys:
                continue
            state = read_state(folder)
            if state and state.get("status") == "done":
                archive_ticket(key, folder, archive_dir, repos_dir)

    # Purge archives older than ARCHIVE_MAX_DAYS
    if archive_dir.exists():
        cutoff = time.time() - (ARCHIVE_MAX_DAYS * 86400)
        for folder in archive_dir.iterdir():
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder)
                log.info(f"{folder.name}: purged archive (>{ARCHIVE_MAX_DAYS} days old)")

# ── Skill copy ────────────────────────────────────────────────────────────────

SKILLS_TO_COPY = [
    "ticket-worker",
    "ticket-discovery",
    "ticket-implementation",
    "ticket-review",
    "ticket-merge",
]

def copy_worker_skill(ticket_dir: Path) -> None:
    for skill in SKILLS_TO_COPY:
        src = PLUGIN_ROOT / "skills" / skill / "SKILL.md"
        dest = ticket_dir / ".agents" / "skills" / skill / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))

# ── Context file ──────────────────────────────────────────────────────────────

def write_context_md(ticket_dir: Path, repos_dir: Path) -> None:
    (ticket_dir / ".context.md").write_text(
        f"# Session Context\n\n"
        f"**Repos directory:** {repos_dir}\n\n"
        f"This directory contains local clones organized by repository name.\n"
        f"Use for code exploration. Falls back to GitHub search if unavailable.\n"
    )

# ── Session launch ────────────────────────────────────────────────────────────

def launch_session(key: str, ticket_dir: Path, repos_dir: Path, is_new: bool) -> int:
    """Launch a detached copilot worker session. Returns the child PID."""
    cmd = [
        "copilot",
        "-C", str(ticket_dir),
        f"--name={key}" if is_new else f"--resume={key}",
        "--allow-all-tools",
        "--allow-all-urls",
        f"--add-dir={ticket_dir}",
        f"--add-dir={repos_dir}",
        "-p", "/ticket-worker",
    ]
    log_file = open(ticket_dir / "session.log", "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,  # detach — survives orchestrator exit
    )
    return proc.pid


def poll_pickup(ticket_dir: Path, timeout: int = PICKUP_TIMEOUT_SECS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state(ticket_dir)
        if state and state.get("picked_up_at"):
            return True
        time.sleep(1)
    # One final check after deadline — avoids false negatives on tight timing
    state = read_state(ticket_dir)
    return bool(state and state.get("picked_up_at"))

# ── Stage rewind ─────────────────────────────────────────────────────────────

# Jira transition IDs for PDE workflow
JIRA_TRANSITION_IDS = {
    "To Do":          251,
    "In Discovery":   421,
    "QA Review":      301,
    "In Progress":    331,
    "In Review":      291,
    "UAT Review":     311,
    "Done":           231,
    "Blocked":         21,
    "Backlog":        271,
}

# Ordered stages with their deliverable artifacts.
# A stage is "done" if and only if its deliverable exists in ticket_dir.
STAGE_ORDER = [
    ("In Discovery",  "discovery.md"),
    ("In Progress",   "implementation-notes.md"),
    ("UAT Review",    "review-notes.md"),
]

# Which statuses imply that certain stages should already be done
# Map: jira_status → number of stages that must be completed before reaching it
STAGES_REQUIRED_BEFORE = {
    # Status          min completed stages needed
    "In Discovery":   0,
    "QA Review":      1,   # discovery must be done
    "In Progress":    1,   # discovery must be done
    "In Review":      2,   # discovery + implementation must be done
    "UAT Review":     3,   # discovery + implementation + review must be done
}


def transition_ticket(key: str, target_status: str, auth) -> None:
    transition_id = JIRA_TRANSITION_IDS.get(target_status)
    if not transition_id:
        log.warning(f"{key}: no transition ID known for '{target_status}' — skipping transition")
        return
    r = requests.post(
        f"{JIRA_BASE}/issue/{key}/transitions",
        json={"transition": {"id": str(transition_id)}},
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    log.info(f"{key}: transitioned → {target_status}")


def count_completed_stages(ticket_dir: Path) -> int:
    """Return how many stages have their deliverable artifact in ticket_dir."""
    if not ticket_dir.exists():
        return 0
    count = 0
    for _, artifact in STAGE_ORDER:
        if (ticket_dir / artifact).exists():
            count += 1
        else:
            break  # stages must complete in order
    return count


def rewind_if_needed(
    key: str,
    jira_status: str,
    ticket_dir: Path,
    auth,
) -> str:
    """
    Check whether the ticket's Jira status is ahead of its actual completed stages.
    If so, transition Jira to the earliest incomplete stage's status and return
    the corrected status. Otherwise return jira_status unchanged.
    """
    required = STAGES_REQUIRED_BEFORE.get(jira_status)
    if required is None:
        return jira_status  # To Do or unknown — no rewind logic needed

    completed = count_completed_stages(ticket_dir)

    if completed >= required:
        return jira_status  # ticket is where it should be

    effective_status = STAGE_ORDER[completed][0]

    log.warning(
        f"{key}: Jira says '{jira_status}' but only {completed}/{required} prerequisite "
        f"stage(s) are done — rewinding to '{effective_status}'"
    )
    try:
        transition_ticket(key, effective_status, auth)
    except Exception as e:
        log.error(f"{key}: failed to rewind Jira to '{effective_status}': {e}")
        # Still use the corrected status locally even if Jira call fails
    return effective_status


# ── Per-ticket processing ─────────────────────────────────────────────────────

def process_ticket(
    key: str,
    jira_status: str,
    assignee: dict | None,
    cwd: Path,
    repos_dir: Path,
    auth,
    current_user_id: str | None,
) -> None:
    ticket_dir = cwd / "tickets" / key

    # Check state file for running/crashed sessions
    state = read_state(ticket_dir)
    if state:
        if state.get("status") == "running":
            pid = state.get("pid")
            if pid_alive(pid):
                log.info(f"{key}: session already running (PID {pid}) — skipping")
                return
            else:
                log.info(f"{key}: crashed session detected (PID {pid}) — proceeding")
                state["status"] = "crashed"
                write_state(ticket_dir, state)
        # waiting / done / crashed → proceed

    # ── Stage rewind check ───────────────────────────────────────────────────
    # The human may have manually moved the ticket to a status that skips earlier
    # stages. Detect that and rewind Jira (and our local effective_status) to the
    # earliest stage that hasn't been done yet.
    jira_status = rewind_if_needed(key, jira_status, ticket_dir, auth)

    # If there is no prior session (no state file, no ticket dir) and this isn't
    # already "To Do", treat it as a fresh start — the human may have manually
    # set a mid-pipeline status on a brand new ticket, or we rewound to a stage
    # that was never started.
    no_prior_session = not ticket_dir.exists() or read_state(ticket_dir) is None
    is_new = jira_status == "To Do" or (no_prior_session and jira_status == "In Discovery")

    if is_new:
        # Archive prior artifacts and free the session name
        if ticket_dir.exists():
            archive_ticket(key, ticket_dir, cwd / "tickets" / "archive", repos_dir)
        else:
            rename_old_session(key)

    # Ensure directory exists
    ticket_dir.mkdir(parents=True, exist_ok=True)

    # Write context and copy skill
    write_context_md(ticket_dir, repos_dir)
    copy_worker_skill(ticket_dir)

    # Reset / update state file before launch
    if is_new:
        write_state(ticket_dir, {
            "status": "running",
            "pid": None,
            "started_at": None,
            "picked_up_at": None,
            "updated_at": None,
            "stage": None,
        })
    else:
        s = read_state(ticket_dir) or {}
        s["status"] = "running"
        s["picked_up_at"] = None
        write_state(ticket_dir, s)

    # Launch session
    pid = launch_session(key, ticket_dir, repos_dir, is_new)
    log.info(f"{key}: launched {'new' if is_new else 'resumed'} session (PID {pid})")

    # Write real PID
    s = read_state(ticket_dir) or {}
    s["pid"] = pid
    write_state(ticket_dir, s)

    # Confirm pickup
    if poll_pickup(ticket_dir):
        log.info(f"{key}: session confirmed pickup — moving to next ticket")
    else:
        log.warning(f"{key}: session did not pick up within {PICKUP_TIMEOUT_SECS}s — flagged as failed to start")

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    cwd = Path.cwd()
    repos_dir = Path(os.environ["REPOS_DIR"]) if "REPOS_DIR" in os.environ else cwd
    auth = _auth()

    log.info(f"Repos directory: {repos_dir}")
    try:
        issues = search_tickets(auth)
    except requests.HTTPError as e:
        log.error(f"Jira query failed: {e.response.status_code} {e.response.text}")
        sys.exit(1)
    except Exception as e:
        log.error(f"Jira query failed: {e}")
        sys.exit(1)

    if not issues:
        log.info("No AI-Work tickets found")
        active_keys = set()
        tickets_dir = cwd / "tickets"
        archive_dir = cwd / "tickets" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        cleanup_pass(tickets_dir, archive_dir, active_keys, repos_dir)
        return

    log.info(f"Found {len(issues)} ticket(s)")

    # Safety check — only PDE- keys
    active_keys: set[str] = set()
    valid_issues: list[dict] = []
    for issue in issues:
        key = issue["key"]
        if not key.startswith("PDE-"):
            log.warning(f"{key}: does not start with PDE- — skipping (hard safety check)")
            continue
        active_keys.add(key)
        valid_issues.append(issue)

    # Cleanup pass (archive done, purge old archives)
    tickets_dir = cwd / "tickets"
    archive_dir = cwd / "tickets" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    cleanup_pass(tickets_dir, archive_dir, active_keys, repos_dir)

    # Get current user — only process tickets assigned to this user
    current_user_id: str | None = None
    try:
        me = get_current_user(auth)
        current_user_id = me.get("accountId")
    except Exception as e:
        log.warning(f"Could not get current user — cannot filter by assignee: {e}")

    # Dispatch each ticket
    for issue in valid_issues:
        key = issue["key"]
        jira_status = issue["fields"]["status"]["name"]
        assignee = issue["fields"].get("assignee")
        assignee_id = (assignee or {}).get("accountId")

        if jira_status not in ACTIONABLE_STATUSES:
            log.info(f"{key}: {jira_status} — skipping")
            continue

        # Only process tickets assigned to the current user
        if current_user_id and assignee_id != current_user_id:
            who = (assignee or {}).get("displayName", "unassigned")
            log.info(f"{key}: not assigned to current user (assigned to: {who}) — skipping")
            continue

        log.info(f"{key}: processing ({jira_status})")
        try:
            process_ticket(key, jira_status, assignee, cwd, repos_dir, auth, current_user_id)
        except Exception as e:
            log.error(f"{key}: unhandled error — {e}", exc_info=True)


if __name__ == "__main__":
    main()
