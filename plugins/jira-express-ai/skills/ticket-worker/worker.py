#!/usr/bin/env python3
"""
PDE AI Ticket Worker

Lifecycle/state-machine manager for ONE ticket: reads Jira status, sanity-checks
it against what's actually on disk (rewinding if a human skipped a stage),
launches the matching specialist (still an LLM-driven `claude -p "/<skill>"`
session — this script never does discovery/implementation/review/merge work
itself), validates its output artifact, and transitions Jira. Run once per
`ticket-worker` session; exits after routing to exactly one path.

Usage:
    cd tickets/<KEY>   # the ticket-worker session's own cwd
    python3 "$CLAUDE_PLUGIN_ROOT/skills/ticket-worker/worker.py" <repos-dir>

Required env vars: ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN (same
CLAUDE_PLUGIN_OPTION_* fallback as ticket-orchestrator/orchestrator.py).
"""

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

CLOUD_ID = "e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2"
JIRA_BASE = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"

# This is the only copy of this table — orchestrator.py makes no Jira writes
# at all, so there's nothing else to keep in sync with it.
TRANSITION_IDS = {
    "Blocked": "21",
    "To Do": "251",
    "In Discovery": "421",
    "QA Review": "301",
    "In Progress": "331",
    "In Review": "291",
    "UAT Review": "311",
    "Done": "231",
    "Backlog": "271",
}

# A stage only counts as done if the one before it also does — see
# completed_stage_count(). Statuses not listed here (To Do, Done, Backlog,
# Cancelled, Released) aren't gated: To Do is handled before this check ever
# runs (see main()), and the rest are terminal/ignored regardless of what's
# on disk. review-notes.md is deliberately not a gated stage — it's an
# optional artifact from a second implementation pass (addressing PR
# comments), never produced when a ticket is approved straight through to
# UAT Review on the first pass. Requiring it would make a legitimately
# complete ticket look incomplete and self-correct into a loop.
STAGE_REQUIREMENTS = {
    "In Discovery": 0,
    "QA Review": 1,
    "In Progress": 1,
    "In Review": 2,
    "UAT Review": 2,
}
REWIND_STATUS_FOR_COUNT = {0: "In Discovery", 1: "In Progress"}

SENTINEL_TIMEOUT = 900  # 15 minutes
SENTINEL_POLL_INTERVAL = 30

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Jira helpers ──────────────────────────────────────────────────────────────

def _auth() -> tuple[str, str]:
    # See orchestrator.py's _auth() for why both the bare and
    # CLAUDE_PLUGIN_OPTION_-prefixed env vars are checked.
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
    return (email, token)


def read_status(key: str, auth) -> str:
    r = requests.get(
        f"{JIRA_BASE}/issue/{key}",
        params={"fields": "status"},
        auth=auth,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["fields"]["status"]["name"]


def jira_transition(key: str, status_name: str, auth) -> None:
    transition_id = TRANSITION_IDS[status_name]
    r = requests.post(
        f"{JIRA_BASE}/issue/{key}/transitions",
        json={"transition": {"id": transition_id}},
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    log.info(f"{key}: transitioned to {status_name}")


def jira_comment(key: str, text: str, auth) -> None:
    r = requests.post(
        f"{JIRA_BASE}/issue/{key}/comment",
        json={"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ]}},
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()


def _adf_text(node) -> str:
    """Flatten an Atlassian Document Format node tree down to plain text —
    just enough to recognize this plugin's own comment prefixes (🤖, 🤖 ⚠️),
    not a general ADF renderer."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(_adf_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return "".join(_adf_text(c) for c in node)
    return ""


def get_recent_comments_text(key: str, auth, limit: int = 20) -> list[str]:
    """Most-recent-first plain text of this ticket's last `limit` comments."""
    r = requests.get(
        f"{JIRA_BASE}/issue/{key}/comment",
        params={"orderBy": "-created", "maxResults": limit},
        auth=auth,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    comments = r.json().get("comments", [])
    return [_adf_text(c.get("body", {})) for c in comments]

# ── Artifact parsing ──────────────────────────────────────────────────────────

def extract_status(artifact_path: Path) -> str:
    content = artifact_path.read_text()
    m = re.search(r"^\*\*Status:\*\*\s*(\w+)", content, re.MULTILINE)
    return m.group(1).upper() if m else "UNKNOWN"


def extract_bold_field(content: str, name: str) -> str:
    m = re.search(rf"\*\*{re.escape(name)}:\*\*\s*(.+)", content)
    return m.group(1).strip() if m else ""


def extract_section(content: str, heading: str) -> str:
    m = re.search(rf"##\s*{re.escape(heading)}\s*\n+(.*?)(?:\n##|\Z)", content, re.DOTALL)
    return m.group(1).strip() if m else ""

# ── Sub-agent launch ──────────────────────────────────────────────────────────

def launch_specialist(skill: str, ticket_dir: Path, repos_dir: Path) -> None:
    """Launch a one-shot specialist session. Never resumed across separate
    launches, so --name is a cosmetic display label only (prompt box,
    /resume picker, terminal title) — no --session-id/--resume needed, and
    no timestamp needed either: unlike the worker/orchestrator (where a
    reused name is ambiguous for --resume), nothing ever looks this name up
    again, so a relaunch of the same skill for the same ticket reusing the
    same name is harmless — it also means a relaunch's log output
    accumulates into the same file as the first attempt (append mode).

    Logs to AGENT_CHILD_LOG_DIR/<session-name>.log if that env var is set —
    the calling system wants sessions' output collected centrally — or to
    ticket_dir/<session-name>.log otherwise. Same convention
    orchestrator.py's launch_session() uses for the worker's own log."""
    agent_name = f"{ticket_dir.name}-{skill}"
    log_dir = Path(os.environ.get("AGENT_CHILD_LOG_DIR") or ticket_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"{agent_name}.log", "a")
    proc = subprocess.Popen(
        ["claude", f"--name={agent_name}",
         "--permission-mode=bypassPermissions",
         f"--add-dir={ticket_dir}", f"--add-dir={repos_dir}",
         "-p", f"/{skill} Repos directory: {repos_dir}"],
        cwd=str(ticket_dir),
        stdout=log_file, stderr=log_file,
        start_new_session=True,
    )
    log.info(f"Launched {agent_name} (PID {proc.pid})")


def wait_for_sentinel(skill: str, sentinel_path: Path, timeout: int = SENTINEL_TIMEOUT) -> bool:
    elapsed = 0
    while elapsed < timeout:
        if sentinel_path.exists():
            log.info(f"Sub-agent complete ({sentinel_path.name} found)")
            return True
        log.info(f"Waiting for {skill}... ({elapsed}s elapsed)")
        time.sleep(SENTINEL_POLL_INTERVAL)
        elapsed += SENTINEL_POLL_INTERVAL
    return sentinel_path.exists()

# ── Failure / blocked routing ─────────────────────────────────────────────────

def report_failure(key: str, reason: str, auth) -> None:
    """A sub-agent timing out or a validation check failing isn't a
    deliberate signal like BLOCKED — it's just something that didn't work,
    and it might not happen again on retry. Comment and leave the ticket
    exactly where it is (no Jira transition) for the first two occurrences,
    letting the next orchestrator run resume and retry automatically. Only
    escalate to Blocked once the same kind of thing has failed three times
    in a row with no progress in between. Always exits — every caller
    reports a failure and stops in the same breath, even if Jira itself is
    what's unreachable right now (see the try/except below)."""
    try:
        jira_comment(key, f"🤖 ⚠️ {key}: {reason} — will retry automatically.", auth)

        consecutive = 0
        for text in get_recent_comments_text(key, auth):
            if text.startswith("🤖 ⚠️"):
                consecutive += 1
            else:
                break  # a normal 🤖 progress comment or a human comment — real progress since the last failure

        log.warning(f"{key}: failure reported ({consecutive} consecutive) — {reason}")

        if consecutive >= 3:
            jira_transition(key, "Blocked", auth)
            jira_comment(
                key,
                f"🤖 {key} has failed 3 times in a row with no progress — stopping "
                f"automatic retries. See the recent comments above and this session's logs for details.",
                auth,
            )
            log.error(f"{key}: escalated to Blocked after 3 consecutive failures")
    except Exception as e:
        # Reporting the failure is itself best-effort — if Jira is what's
        # unreachable, don't let an unhandled exception here obscure the
        # original failure with an unrelated stack trace. Either way this
        # function always exits non-zero, so the next orchestrator run
        # retries exactly as it would have anyway.
        log.error(f"{key}: failed to report the failure itself ({e}) — exiting for retry regardless")

    sys.exit(1)


def apply_blocked_routing(key: str, artifact_path: Path, stage: str, auth) -> None:
    """A specialist deliberately saying it can't proceed — always escalate
    immediately, on the first occurrence. Always exits."""
    content = artifact_path.read_text()
    blocker = extract_section(content, "Blocker") or "(no Blocker section found)"
    next_step = extract_section(content, "Suggested Next Step")

    jira_transition(key, "Blocked", auth)
    message = f"🤖 {stage} is blocked on {key}: {blocker}"
    if next_step:
        message += f"\n{next_step}"
    jira_comment(key, message, auth)

    log.warning(f"{key}: BLOCKED ({stage}) — {blocker}")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────

def run_discovery(key: str, ticket_dir: Path, repos_dir: Path, auth) -> None:
    sentinel = ticket_dir / ".discovery-agent-done"
    artifact = ticket_dir / "discovery.md"

    if not sentinel.exists():
        artifact.unlink(missing_ok=True)  # ensure the specialist starts fresh
        launch_specialist("ticket-discovery", ticket_dir, repos_dir)
        if not wait_for_sentinel("ticket-discovery", sentinel):
            report_failure(key, "ticket-discovery did not complete within 900s", auth)
            return

    if not artifact.exists():
        report_failure(key, "ticket-discovery finished but discovery.md is missing", auth)
        return

    if extract_status(artifact) == "BLOCKED":
        apply_blocked_routing(key, artifact, "ticket-discovery", auth)
        return

    jira_transition(key, "QA Review", auth)
    jira_comment(
        key,
        f"🤖 Discovery complete for {key}. Please review discovery.md and either:\n"
        f"• Approve: move to In Progress\n"
        f"• Reject: move back to In Discovery with a comment explaining what to revisit",
        auth,
    )
    log.info(f"{key}: waiting for QA review")


def run_implementation(key: str, ticket_dir: Path, repos_dir: Path, auth) -> None:
    notes = ticket_dir / "implementation-notes.md"
    review_context = ticket_dir / "review-context.md"

    if not notes.exists():
        _run_first_implementation_pass(key, ticket_dir, repos_dir, notes, review_context, auth)
    else:
        _run_review_pass(key, ticket_dir, repos_dir, review_context, auth)


def _run_first_implementation_pass(key: str, ticket_dir: Path, repos_dir: Path, notes: Path, review_context: Path, auth) -> None:
    sentinel = ticket_dir / ".implementation-agent-done"
    if not sentinel.exists():
        launch_specialist("ticket-implementation", ticket_dir, repos_dir)
        if not wait_for_sentinel("ticket-implementation", sentinel):
            report_failure(key, "ticket-implementation did not complete within 900s", auth)
            return

    if not notes.exists():
        report_failure(key, "ticket-implementation finished but implementation-notes.md is missing", auth)
        return

    if extract_status(notes) == "BLOCKED":
        apply_blocked_routing(key, notes, "ticket-implementation", auth)
        return

    # The implementation agent writes review-context.md itself — it's the
    # only one with direct knowledge of which repo(s)/branch(es) it touched.
    # Missing after a non-BLOCKED run is a bug in that agent, not something
    # to work around here.
    if not review_context.exists():
        report_failure(key, "ticket-implementation finished but review-context.md is missing", auth)
        return

    pr_url = extract_bold_field(review_context.read_text(), "PR URL")
    jira_transition(key, "In Review", auth)
    jira_comment(
        key,
        f"🤖 Implementation complete for {key}. PR ready for review: {pr_url}\n"
        f"When approved, move to UAT Review to trigger merge.\n"
        f"If you have comments to address, move back to In Progress.",
        auth,
    )
    log.info(f"{key}: waiting for PR review")


def _run_review_pass(key: str, ticket_dir: Path, repos_dir: Path, review_context: Path, auth) -> None:
    """implementation-notes.md already exists — a human moved the ticket back
    to In Progress after implementation was already done, to address PR
    comments. Run the review agent instead of implementation."""
    notes = ticket_dir / "review-notes.md"
    sentinel = ticket_dir / ".review-agent-done"
    # Always run fresh — including the sentinel, not just the artifact: a
    # stale sentinel left over from a prior review pass would make
    # wait_for_sentinel() below report "done" before the freshly launched
    # agent has even started.
    notes.unlink(missing_ok=True)
    sentinel.unlink(missing_ok=True)

    launch_specialist("ticket-review", ticket_dir, repos_dir)
    if not wait_for_sentinel("ticket-review", sentinel):
        report_failure(key, "ticket-review did not complete within 900s", auth)
        return

    if not notes.exists():
        report_failure(key, "ticket-review finished but review-notes.md is missing", auth)
        return

    if extract_status(notes) == "BLOCKED":
        apply_blocked_routing(key, notes, "ticket-review", auth)
        return

    pr_url = extract_bold_field(review_context.read_text(), "PR URL") if review_context.exists() else ""
    jira_transition(key, "In Review", auth)
    jira_comment(
        key,
        f"🤖 PR review pass complete for {key}. PR: {pr_url}\n"
        f"Comments addressed. Ready for approval — move to UAT Review when approved.\n"
        f"If you have more comments, move back to In Progress.",
        auth,
    )
    log.info(f"{key}: waiting for PR approval")


def run_merge(key: str, ticket_dir: Path, repos_dir: Path, auth) -> None:
    notes = ticket_dir / "merge-notes.md"
    sentinel = ticket_dir / ".merge-agent-done"
    notes.unlink(missing_ok=True)  # always run fresh
    sentinel.unlink(missing_ok=True)

    launch_specialist("ticket-merge", ticket_dir, repos_dir)
    if not wait_for_sentinel("ticket-merge", sentinel):
        report_failure(key, "ticket-merge did not complete within 900s", auth)
        return

    if not notes.exists():
        report_failure(key, "ticket-merge finished but merge-notes.md is missing", auth)
        return

    content = notes.read_text()
    status = extract_status(notes)

    if status == "SUCCESS":
        jira_transition(key, "Done", auth)
        jira_comment(key, f"🤖 Merge complete for {key}. Ticket resolved.", auth)
        log.info(f"{key}: merge complete — ticket resolved")
    elif status == "BLOCKED":
        reason = extract_section(content, "Blocker") or "(no Blocker section found)"
        jira_transition(key, "Blocked", auth)
        jira_comment(key, f"🤖 Merge blocked for {key}: {reason}", auth)
        log.warning(f"{key}: merge blocked — {reason}")
    elif status == "PENDING":
        # Nothing is wrong, just not ready yet — no Jira transition, no
        # comment. The ticket stays at UAT Review exactly as it is; the next
        # orchestrator run resumes this session and checks again.
        reason = extract_section(content, "Reason")
        log.info(f"{key}: merge pending — {reason} — will check again next run")
    else:
        report_failure(key, f"ticket-merge returned unrecognized status '{status}'", auth)


def run_qa_review_gate(key: str, auth) -> None:
    """Human gate, resumed directly into QA Review — re-post the same
    handoff comment as the end of the discovery path."""
    jira_comment(
        key,
        f"🤖 Discovery complete for {key}. Please review discovery.md and either:\n"
        f"• Approve: move to In Progress\n"
        f"• Reject: move back to In Discovery with a comment explaining what to revisit",
        auth,
    )
    log.info(f"{key}: re-posted QA Review comment, waiting")


def run_in_review_gate(key: str, ticket_dir: Path, auth) -> None:
    """Human gate, resumed directly into In Review with no status change
    since the implementation/review path already posted the handoff
    comment — re-post it so a resumed session doesn't just exit silently."""
    review_context = ticket_dir / "review-context.md"
    pr_url = extract_bold_field(review_context.read_text(), "PR URL") if review_context.exists() else ""
    suffix = f": {pr_url}" if pr_url else ""
    jira_comment(
        key,
        f"🤖 {key} is ready for review{suffix}. When approved, move to UAT Review "
        f"to trigger merge. If you have comments to address, move back to In Progress.",
        auth,
    )
    log.info(f"{key}: re-posted In Review comment, waiting")

# ── Rewind ────────────────────────────────────────────────────────────────────

def completed_stage_count(ticket_dir: Path) -> int:
    """A stage only counts as done if the one before it also does."""
    if not (ticket_dir / "discovery.md").exists():
        return 0
    if not (ticket_dir / "implementation-notes.md").exists():
        return 1
    return 2


def sanity_check_and_rewind(key: str, status: str, ticket_dir: Path, auth) -> str:
    """Humans can manually move a ticket to any Jira status, including one
    ahead of what's actually been done (e.g. In Progress with no
    discovery.md yet). Self-correct Jira to the earliest stage that's
    actually missing rather than skipping work, and return the corrected
    status for routing. Returns `status` unchanged for any status not in
    STAGE_REQUIREMENTS (To Do is handled before this runs; terminal statuses
    aren't gated at all)."""
    required = STAGE_REQUIREMENTS.get(status)
    if required is None:
        return status

    have = completed_stage_count(ticket_dir)
    if have >= required:
        return status

    corrected = REWIND_STATUS_FOR_COUNT[have]
    log.warning(f"{key}: Jira says '{status}' but only {have}/{required} prerequisite stage(s) are done — rewinding to '{corrected}'")
    jira_transition(key, corrected, auth)
    return corrected

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) != 2:
        log.error("usage: worker.py <repos-dir>")
        sys.exit(1)
    repos_dir = Path(sys.argv[1])
    ticket_dir = Path.cwd()
    key = ticket_dir.name
    auth = _auth()

    log.info(f"Session started for {key}")

    status = read_status(key, auth)
    log.info(f"Ticket {key}: status={status}")

    if status == "To Do":
        jira_transition(key, "In Discovery", auth)
        status = "In Discovery"

    status = sanity_check_and_rewind(key, status, ticket_dir, auth)

    if status == "In Discovery":
        run_discovery(key, ticket_dir, repos_dir, auth)
    elif status == "QA Review":
        run_qa_review_gate(key, auth)
    elif status == "In Progress":
        run_implementation(key, ticket_dir, repos_dir, auth)
    elif status == "In Review":
        run_in_review_gate(key, ticket_dir, auth)
    elif status == "UAT Review":
        run_merge(key, ticket_dir, repos_dir, auth)
    elif status in ("Done", "Backlog", "Cancelled", "Released"):
        log.info(f"{key}: {status} — nothing to do, exiting")
    else:
        log.warning(f"{key}: unrecognized status '{status}' — exiting without action")


if __name__ == "__main__":
    main()
