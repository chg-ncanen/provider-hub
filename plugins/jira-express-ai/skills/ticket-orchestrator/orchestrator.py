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

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
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

# Deliberate cap, not a paging bug: each dispatched ticket can spawn a
# long-running worker session, so a single cron tick shouldn't try to launch
# an unbounded number of them. If the query ever actually hits this cap,
# search_tickets() logs it loudly rather than silently dropping the overflow —
# see its "isLast"/count check below.
MAX_TICKETS_PER_RUN = 100

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
    # Claude Code propagates plugin userConfig values as CLAUDE_PLUGIN_OPTION_<KEY>
    # (uppercased), never as the bare key name — confirmed against this plugin's
    # own bootstrap-deps.sh equivalent in pde-ops-tools. Bare ATLASSIAN_EMAIL/
    # ATLASSIAN_API_TOKEN still take priority for direct/manual runs (e.g.
    # exported by hand outside a plugin-managed session). Re-exporting the
    # resolved values under the bare names means every subprocess this script
    # launches (nested claude sessions, and whatever *they* launch) sees plain
    # ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN via normal env inheritance, so none of
    # the sub-agent SKILL.md files need to know about the CLAUDE_PLUGIN_OPTION_
    # prefix at all.
    email = (
        os.environ.get("ATLASSIAN_EMAIL", "").strip()
        or os.environ.get("CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL", "").strip()
    )
    token = (
        os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
        or os.environ.get("CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN", "").strip()
    )
    if not email or not token:
        log.error("ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN must be set (directly, or via this plugin's userConfig)")
        sys.exit(1)
    os.environ["ATLASSIAN_EMAIL"] = email
    os.environ["ATLASSIAN_API_TOKEN"] = token
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
        json={"jql": JQL, "fields": ["status", "assignee", "summary"], "maxResults": MAX_TICKETS_PER_RUN},
        auth=auth,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    issues = body.get("issues", [])
    # MAX_TICKETS_PER_RUN is an intentional per-run cap (see its definition),
    # not something we page through — but if it's actually being hit, that's
    # worth knowing loudly rather than silently dropping the overflow forever.
    if body.get("isLast") is False or len(issues) >= MAX_TICKETS_PER_RUN:
        log.warning(
            f"Jira query returned {len(issues)} issue(s), at or above the "
            f"MAX_TICKETS_PER_RUN cap of {MAX_TICKETS_PER_RUN} — there may be "
            f"more matching tickets than this run can see."
        )
    return issues


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


def update_state(ticket_dir: Path, mutate) -> dict:
    """Atomically read-modify-write .session.state under an exclusive flock.

    A plain read_state() + write_state() pair is NOT atomic against a
    concurrent writer — e.g. the worker session this same call just launched,
    which does its own read-modify-write at startup to set picked_up_at. If
    that lands between this process's read and write, whichever write lands
    last silently clobbers the other's update (concretely: the orchestrator's
    stale copy, missing picked_up_at, overwrites the worker's update, and
    poll_all_pickups() then times out on a session that actually started fine).
    flock only serializes cooperating callers — the worker's own startup
    write (ticket-worker/SKILL.md step 4) takes the same lock for this to
    actually hold.
    """
    path = ticket_dir / ".session.state"
    path.touch(exist_ok=True)
    with open(path, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            content = fh.read()
            try:
                state = json.loads(content) if content else {}
            except Exception:
                state = {}
            state = mutate(state)
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(state, indent=2))
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return state

# ── PID check ─────────────────────────────────────────────────────────────────

def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

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
    """Close remote artifacts and move ticket_dir to a timestamped archive folder."""
    close_remote_artifacts(key, ticket_dir, repos_dir)
    dest = archive_dir / f"{key}-{date.today()}"
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = archive_dir / f"{key}-{date.today()}-{suffix}"
    shutil.move(str(ticket_dir), str(dest))
    log.info(f"{key}: archived → {dest}")


def cleanup_pass(tickets_dir: Path, archive_dir: Path, active_keys: set[str], repos_dir: Path) -> None:
    # Archive tickets no longer in Jira results — not just ones that finished
    # cleanly (status: done). A ticket dropped out of active_keys because it's
    # now Done, Cancelled, Released, or Backlog-ed (all excluded by JQL), or
    # its AI-Work label was removed — in every case the orchestrator has no
    # reason to keep it around. Only exception: leave a genuinely in-flight
    # session (status: running with a live PID) alone rather than yanking its
    # working directory out from under it; everything else (waiting, done,
    # crashed, a running status with a dead PID, or no state file at all) is
    # safe to archive.
    if tickets_dir.exists():
        for folder in tickets_dir.iterdir():
            if not folder.is_dir() or folder == archive_dir:
                continue
            key = folder.name
            if key in active_keys:
                continue
            state = read_state(folder)
            if state and state.get("status") == "running" and pid_alive(state.get("pid")):
                log.info(f"{key}: no longer in Jira's active results but session still running (PID {state.get('pid')}) — leaving in place")
                continue
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

def render_transition_table() -> str:
    """Render JIRA_TRANSITION_IDS (defined below) as the markdown table
    ticket-worker/SKILL.md embeds, so the deployed copy can't drift from the
    orchestrator's own table — see copy_worker_skill()."""
    lines = ["| Target status | Transition ID |", "|---|---|"]
    lines += [f"| {status} | {tid} |" for status, tid in JIRA_TRANSITION_IDS.items()]
    return "\n".join(lines)


TRANSITION_TABLE_RE = re.compile(
    r"\| Target status \| Transition ID \|\n\|---\|---\|\n(?:\|.*\|\n?)*"
)


def copy_worker_skill(ticket_dir: Path) -> None:
    for skill in SKILLS_TO_COPY:
        src = PLUGIN_ROOT / "skills" / skill / "SKILL.md"
        dest = ticket_dir / ".claude" / "skills" / skill / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = src.read_text()
        if skill == "ticket-worker":
            # JIRA_TRANSITION_IDS is the single source of truth; render it
            # into the copy instead of trusting a hand-maintained markdown
            # table in the SKILL.md source to stay in sync with the code.
            content = TRANSITION_TABLE_RE.sub(render_transition_table() + "\n", content)
        dest.write_text(content)

# ── Context file ──────────────────────────────────────────────────────────────

def write_context_md(ticket_dir: Path, repos_dir: Path) -> None:
    (ticket_dir / ".context.md").write_text(
        f"# Session Context\n\n"
        f"**Repos directory:** {repos_dir}\n\n"
        f"This directory contains local clones organized by repository name.\n"
        f"Use for code exploration. Falls back to GitHub search if unavailable.\n"
    )

# ── Session launch ────────────────────────────────────────────────────────────

def launch_session(key: str, session_id: str, ticket_dir: Path, repos_dir: Path, is_new: bool) -> int:
    """Launch a detached claude worker session. Returns the child PID.

    `--name` is a cosmetic display label only (shown in the prompt box, the
    /resume picker, and the terminal title) — it is NOT a valid --resume
    target. The actual resume handle is `session_id`, a UUID we generate
    ourselves and persist in .session.state (see process_ticket), passed as
    --session-id on first launch and --resume on every later one.
    """
    cmd = [
        "claude",
        f"--name={key}",
        f"--session-id={session_id}" if is_new else f"--resume={session_id}",
        "--permission-mode=bypassPermissions",
        f"--add-dir={ticket_dir}",
        f"--add-dir={repos_dir}",
        "-p", "/ticket-worker",
    ]
    log_file = open(ticket_dir / "session.log", "a")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ticket_dir),
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,  # detach — survives orchestrator exit
    )
    return proc.pid


def poll_all_pickups(launched: dict[str, Path], timeout: int = PICKUP_TIMEOUT_SECS) -> None:
    """Poll every ticket launched this run for pickup confirmation concurrently,
    against one shared deadline — not serially, PICKUP_TIMEOUT_SECS per ticket.
    With N tickets launched in a single run, a serial per-ticket wait could
    take N*PICKUP_TIMEOUT_SECS and blow past the 5-15 minute cron interval
    this is meant to run on; polling the whole batch together caps the total
    wait at PICKUP_TIMEOUT_SECS regardless of how many tickets launched."""
    pending = dict(launched)
    deadline = time.monotonic() + timeout

    def _check_once():
        for key in list(pending):
            state = read_state(pending[key])
            if state and state.get("picked_up_at"):
                log.info(f"{key}: session confirmed pickup")
                del pending[key]

    while pending and time.monotonic() < deadline:
        _check_once()
        if pending:
            time.sleep(1)
    _check_once()  # one final check after the deadline — avoids false negatives on tight timing

    for key in pending:
        log.warning(f"{key}: session did not pick up within {timeout}s — flagged as failed to start")

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
# review-notes.md is deliberately NOT a stage here: it's an optional artifact
# from a second implementation pass (addressing PR comments), never produced
# when a ticket goes straight from In Review to UAT Review on first approval.
# Treating it as a required 3rd stage made rewind_if_needed compute
# STAGE_ORDER[2][0] == "UAT Review" for a ticket that had legitimately already
# reached UAT Review — a self-transition, firing every run.
STAGE_ORDER = [
    ("In Discovery",  "discovery.md"),
    ("In Progress",   "implementation-notes.md"),
]

# Which statuses imply that certain stages should already be done. Only
# covers statuses rewind_if_needed is actually called for — the ACTIONABLE_STATUSES
# rewind_if_needed's caller (process_ticket) is invoked for. "QA Review" and
# "In Review" are human-gate statuses that never reach rewind_if_needed at
# all (process_ticket only runs for ACTIONABLE_STATUSES), so they don't
# belong here — this table used to carry both anyway, silently unreachable.
# Map: jira_status → number of stages that must be completed before reaching it
STAGES_REQUIRED_BEFORE = {
    # Status          min completed stages needed
    "In Discovery":   0,
    "In Progress":    1,   # discovery must be done
    "UAT Review":     2,   # discovery + implementation must be done
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
) -> Path | None:
    """Launch (or resume) this ticket's session if warranted. Returns the
    ticket_dir if a session was actually launched this call, so the caller
    can poll it for pickup afterward — None if nothing was launched (already
    running, or a non-actionable path). Does NOT poll for pickup itself; see
    poll_all_pickups() in main(), which polls every ticket launched this run
    concurrently against one shared deadline instead of serially per ticket.
    """
    ticket_dir = cwd / "tickets" / key

    # Check state file for running/crashed sessions
    state = read_state(ticket_dir)
    if state:
        if state.get("status") == "running":
            pid = state.get("pid")
            if pid_alive(pid):
                log.info(f"{key}: session already running (PID {pid}) — skipping")
                return None
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
        # Archive prior artifacts — a fresh session just gets a fresh UUID
        # below, so there's no old session identity to free up first.
        if ticket_dir.exists():
            archive_ticket(key, ticket_dir, cwd / "tickets" / "archive", repos_dir)

    # Ensure directory exists
    ticket_dir.mkdir(parents=True, exist_ok=True)

    # Write context and copy skill
    write_context_md(ticket_dir, repos_dir)
    copy_worker_skill(ticket_dir)

    # Reset / update state file before launch. claude_session_id is the real
    # --resume handle (a UUID we mint ourselves) — --name is cosmetic only,
    # see launch_session().
    if is_new:
        session_id = str(uuid.uuid4())
        write_state(ticket_dir, {
            "status": "running",
            "pid": None,
            "started_at": None,
            "picked_up_at": None,
            "updated_at": None,
            "stage": None,
            "claude_session_id": session_id,
        })
    else:
        s = read_state(ticket_dir) or {}
        session_id = s.get("claude_session_id")
        if not session_id:
            # Pre-existing ticket dir with no recorded session id (e.g. an
            # upgrade from an older run) — nothing to resume, start fresh.
            log.warning(f"{key}: no claude_session_id on record — starting a new session instead of resuming")
            session_id = str(uuid.uuid4())
            is_new = True
        s["status"] = "running"
        s["picked_up_at"] = None
        s["claude_session_id"] = session_id
        write_state(ticket_dir, s)

    # Launch session
    pid = launch_session(key, session_id, ticket_dir, repos_dir, is_new)
    log.info(f"{key}: launched {'new' if is_new else 'resumed'} session (PID {pid}, claude_session_id={session_id})")

    # Write real PID — via update_state(), not read_state()+write_state(), since
    # the just-launched worker session is concurrently doing its own
    # read-modify-write to set picked_up_at/started_at (see update_state()'s
    # docstring for the exact clobber this prevents).
    def _set_pid(s):
        s["pid"] = pid
        return s
    update_state(ticket_dir, _set_pid)

    return ticket_dir

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

    # Get current user — only process tickets assigned to this user. This is a
    # hard safety requirement (see ticket-orchestrator/SKILL.md's "To Do" step),
    # so a failed lookup must fail closed (skip dispatch for this run) rather
    # than fail open — `if current_user_id and ...` alone would silently treat
    # a None current_user_id as "no filter" and process every ticket regardless
    # of assignee, including ones assigned to other engineers.
    try:
        me = get_current_user(auth)
        current_user_id = me.get("accountId")
    except Exception as e:
        log.error(f"Could not get current user — refusing to dispatch this run (assignee filter cannot be enforced): {e}")
        return
    if not current_user_id:
        log.error("Jira /myself returned no accountId — refusing to dispatch this run (assignee filter cannot be enforced)")
        return

    # Dispatch each ticket
    launched: dict[str, Path] = {}  # key -> ticket_dir, for tickets launched this run
    for issue in valid_issues:
        key = issue["key"]
        jira_status = issue["fields"]["status"]["name"]
        assignee = issue["fields"].get("assignee")
        assignee_id = (assignee or {}).get("accountId")

        if jira_status not in ACTIONABLE_STATUSES:
            log.info(f"{key}: {jira_status} — skipping")
            continue

        # Only process tickets assigned to the current user
        if assignee_id != current_user_id:
            who = (assignee or {}).get("displayName", "unassigned")
            log.info(f"{key}: not assigned to current user (assigned to: {who}) — skipping")
            continue

        log.info(f"{key}: processing ({jira_status})")
        try:
            ticket_dir = process_ticket(key, jira_status, assignee, cwd, repos_dir, auth, current_user_id)
            if ticket_dir is not None:
                launched[key] = ticket_dir
        except Exception as e:
            log.error(f"{key}: unhandled error — {e}", exc_info=True)

    # Poll every session launched this run for pickup confirmation together —
    # see poll_all_pickups()'s docstring for why this replaced a per-ticket
    # serial wait.
    if launched:
        poll_all_pickups(launched)


if __name__ == "__main__":
    main()
