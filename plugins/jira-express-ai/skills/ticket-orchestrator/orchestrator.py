#!/usr/bin/env python3
"""
PDE AI Ticket Orchestrator

Stateless dispatcher: query Jira, check each ticket's worker lock, launch or
resume worker sessions, exit.
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

import contextlib
import fcntl
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

# ── Worker lock ───────────────────────────────────────────────────────────────
#
# Liveness is answered by an OS-level flock, not by a stored pid + kill(0)
# check. This isn't just simpler, it's more correct: flock is tied to the
# open file description, not to any code path running cleanup — it's
# released unconditionally by the kernel the instant the holding process's
# file descriptors go away, whether that's a clean exit, an uncaught
# exception, kill -9, an OOM kill, or the whole machine losing power. A
# stored pid can be legitimately reused by an unrelated process after a
# reboot (numbering restarts low); a held-or-not flock can't lie that way.
#
# The lock is handed to the launched worker across the exec boundary: this
# process opens + locks the file, passes that exact fd to the child via
# subprocess.Popen's pass_fds, then closes its own reference. flock locks
# are associated with the open file description, which the child's inherited
# fd also refers to — so the lock stays held by the child for as long as the
# child keeps that fd open, i.e. its entire lifetime, with no gap between
# "orchestrator decided to launch" and "child is actually holding the lock."

LOCK_FILENAME = ".worker.lock"


def is_locked(ticket_dir: Path) -> bool:
    """Non-consuming peek: is a live worker session currently holding this
    ticket's lock? Never holds the lock itself — acquires non-blockingly and
    immediately releases if it succeeds, just to answer the question."""
    lock_path = ticket_dir / LOCK_FILENAME
    if not lock_path.exists():
        return False
    with open(lock_path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def try_acquire_lock(ticket_dir: Path):
    """Try to acquire and KEEP HOLDING this ticket's worker lock — unlike
    is_locked(), does not release on success. Returns the open file object
    (the caller must pass its fd to the launched child via pass_fds and then
    close its own reference — see launch_session()) if acquired, or None if
    another process already holds it."""
    lock_path = ticket_dir / LOCK_FILENAME
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh

@contextlib.contextmanager
def repo_lock(repos_dir: Path, repo: str):
    """Hold the same per-repo flock ticket-implementation/SKILL.md takes
    around `git worktree add` (see its "Repo setup" section) — at the same
    path, $REPOS_DIR/.repo-locks/<repo>.lock — around any other operation
    that touches that repo's shared .git/worktrees/ metadata. `git worktree
    remove`/`prune` (below) is exactly such an operation: it's easy to assume
    only *creating* a worktree touches shared state, but removing one does
    too, and a concurrent add+remove on the same repo can race on it just
    the same. Blocks and waits rather than skipping if held — unlike the
    per-ticket worker lock, this isn't optional to act on, just brief."""
    lock_dir = repos_dir / ".repo-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{repo}.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

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

    # Remove git worktree (ticket's isolated checkout). Locked the same way
    # ticket-implementation locks `git worktree add` — removal touches the
    # same shared .git/worktrees/ metadata as creation, and a concurrent add
    # (some other ticket setting up on this same repo right now) can race
    # against it just the same.
    worktree_path = ticket_dir / repo
    main_clone = repos_dir / repo
    if worktree_path.exists() and main_clone.exists():
        try:
            with repo_lock(repos_dir, repo):
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
    # cleanly. A ticket dropped out of active_keys because it's now Done,
    # Cancelled, Released, or Backlog-ed (all excluded by JQL), or its
    # AI-Work label was removed — in every case the orchestrator has no
    # reason to keep it around. Only exception: leave a genuinely in-flight
    # session (its lock still held) alone rather than yanking its working
    # directory out from under it.
    if tickets_dir.exists():
        for folder in tickets_dir.iterdir():
            if not folder.is_dir() or folder == archive_dir:
                continue
            key = folder.name
            if key in active_keys:
                continue
            try:
                if is_locked(folder):
                    log.info(f"{key}: no longer in Jira's active results but session still running — leaving in place")
                    continue
                archive_ticket(key, folder, archive_dir, repos_dir)
            except Exception as e:
                # One bad folder (unreadable file, permission error, etc.)
                # must not stop every other folder in this pass from being
                # archived — and must not propagate up and abort dispatch
                # for every ticket this run, every run, forever.
                log.error(f"{key}: failed to archive — {e}", exc_info=True)

    # Purge archives older than ARCHIVE_MAX_DAYS
    if archive_dir.exists():
        cutoff = time.time() - (ARCHIVE_MAX_DAYS * 86400)
        for folder in archive_dir.iterdir():
            try:
                if folder.is_dir() and folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder)
                    log.info(f"{folder.name}: purged archive (>{ARCHIVE_MAX_DAYS} days old)")
            except Exception as e:
                log.error(f"{folder.name}: failed to purge archive — {e}", exc_info=True)

# ── Session rename ────────────────────────────────────────────────────────────

def rename_stale_session(key: str) -> None:
    """Before a fresh 'To Do' launch, rename any existing claude session
    named <key> out of the way via a headless /rename prompt, so --name and
    --resume can both just use the plain ticket key without ever colliding
    with a leftover session from a prior run of this same ticket.

    Verified directly: claude session names are NOT unique — launching a
    second fresh session that reuses a --name already in use creates a
    second, distinct session sharing that name, and a later --resume by
    that name then hard-errors ("matches N sessions... pass one of these
    session IDs to disambiguate") instead of resolving. /rename does work
    as a plain headless -p prompt (no interactive session needed), so
    renaming the old one out of the way first — mirroring Copilot CLI's
    original same-purpose step, just via Claude Code's own command instead
    of hand-editing session files — keeps the plain key always resolvable.

    Safe to call even when no such session exists yet (the common case for
    a ticket's very first "To Do"): --resume on a name with zero matches
    just fails to find anything to rename, which is not an error worth
    surfacing.
    """
    archive_name = f"{key}-archived-{date.today()}"
    try:
        subprocess.run(
            ["claude", f"--resume={key}", "-p", f"/rename {archive_name}",
             "--permission-mode=bypassPermissions"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        log.warning(f"{key}: rename-stale-session attempt failed (likely nothing to rename): {e}")

# ── Session launch ────────────────────────────────────────────────────────────

def launch_session(key: str, ticket_dir: Path, repos_dir: Path, is_new: bool, lock_fh) -> int:
    """Launch a detached claude worker session. Returns the child PID.

    --name/--resume both just use the plain ticket key — verified directly
    that Claude Code resolves --resume by the name set via --name at launch,
    non-interactively, no UUID needed, as long as the name is unambiguous.
    rename_stale_session() (called from process_ticket before a fresh
    launch) is what keeps it unambiguous.

    The repos directory rides along as plain text after the skill invocation
    in the same prompt — verified directly that Claude Code passes text
    following a skill name straight through as real input. No context file
    needed. ticket-worker is invoked by its bare name, not namespaced under
    this plugin — verified directly that an installed plugin's skills
    resolve by bare name too, as long as it's unambiguous, so there's also
    no need to copy its (or any specialist's) SKILL.md into this ticket
    directory first; the installed plugin is already globally reachable.

    lock_fh is the already-held worker lock (see try_acquire_lock()) —
    pass_fds hands its fd to the child across the exec boundary, and closing
    our own reference afterward is what makes the lock live exactly as long
    as the child does, with no gap.
    """
    if is_new:
        cmd = ["claude", f"--name={key}"]
    else:
        cmd = ["claude", f"--resume={key}"]
    cmd += [
        "--permission-mode=bypassPermissions",
        f"--add-dir={ticket_dir}",
        f"--add-dir={repos_dir}",
        "-p", f"/ticket-worker Repos directory: {repos_dir}",
    ]
    log_file = open(ticket_dir / "session.log", "a")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ticket_dir),
        stdout=log_file,
        stderr=log_file,
        pass_fds=(lock_fh.fileno(),),
        start_new_session=True,  # detach — survives orchestrator exit
    )
    lock_fh.close()  # our reference; the child's inherited fd keeps the lock held
    return proc.pid


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
    ticket_dir if a session was actually launched this call, None otherwise
    (already running, or nothing to do). Doesn't wait to see whether the
    launch actually succeeds — see this module's docstring for why that's
    fine to not know: whatever caused a launch to fail, the next run finds
    the lock free again and just retries, exactly as if it had noticed the
    failure immediately.

    Owns nothing inside ticket_dir except LOCK_FILENAME. Everything else in
    there — the state the worker tracks about itself, the actual research/
    implementation/review/merge artifacts — belongs entirely to the worker
    and its specialists; this function never reads or writes any of it.
    """
    ticket_dir = cwd / "tickets" / key
    had_prior_dir = ticket_dir.exists()

    if had_prior_dir and is_locked(ticket_dir):
        log.info(f"{key}: session already running — skipping")
        return None

    # ── Stage rewind check ───────────────────────────────────────────────────
    # The human may have manually moved the ticket to a status that skips earlier
    # stages. Detect that and rewind Jira (and our local effective_status) to the
    # earliest stage that hasn't been done yet.
    jira_status = rewind_if_needed(key, jira_status, ticket_dir, auth)

    # If there is no prior directory and this isn't already "To Do", treat it
    # as a fresh start — the human may have manually set a mid-pipeline status
    # on a brand new ticket, or we rewound to a stage that was never started.
    no_prior_session = not had_prior_dir
    is_new = jira_status == "To Do" or (no_prior_session and jira_status == "In Discovery")

    if is_new:
        # Rename any stale claude session still holding this ticket's plain
        # key name — a leftover from a prior run of this same ticket — before
        # this launch reuses that name, so --resume stays unambiguous. See
        # rename_stale_session()'s docstring for why this is necessary.
        rename_stale_session(key)
        if had_prior_dir:
            archive_ticket(key, ticket_dir, cwd / "tickets" / "archive", repos_dir)

    # Ensure directory exists — the lock file has to live inside it.
    ticket_dir.mkdir(parents=True, exist_ok=True)

    lock_fh = try_acquire_lock(ticket_dir)
    if lock_fh is None:
        # Lost a race with another process between the initial is_locked()
        # peek and here — vanishingly unlikely (nothing else should be
        # touching this exact ticket concurrently), but fail safe rather
        # than launch a second session on top of it.
        log.warning(f"{key}: lock held by another process at launch time — skipping this run")
        return None

    pid = launch_session(key, ticket_dir, repos_dir, is_new, lock_fh)
    log.info(f"{key}: launched {'new' if is_new else 'resumed'} session (PID {pid})")
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
        try:
            cleanup_pass(tickets_dir, archive_dir, active_keys, repos_dir)
        except Exception as e:
            log.error(f"cleanup_pass failed: {e}", exc_info=True)
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

    # Cleanup pass (archive done, purge old archives). Wrapped like each
    # per-ticket dispatch below — a failure here must not abort dispatch for
    # every ticket this run, and must not recur forever on every future run.
    tickets_dir = cwd / "tickets"
    archive_dir = cwd / "tickets" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        cleanup_pass(tickets_dir, archive_dir, active_keys, repos_dir)
    except Exception as e:
        log.error(f"cleanup_pass failed: {e}", exc_info=True)

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

    # Dispatch each ticket. Fire-and-forget — no waiting to confirm a launch
    # actually took; see process_ticket()'s docstring for why that's fine.
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
            process_ticket(key, jira_status, assignee, cwd, repos_dir, auth, current_user_id)
        except Exception as e:
            log.error(f"{key}: unhandled error — {e}", exc_info=True)


if __name__ == "__main__":
    main()
