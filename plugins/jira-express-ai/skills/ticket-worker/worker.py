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

Required env vars: ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN — may instead come
from this plugin's userConfig via the same .env relay as
ticket-orchestrator/orchestrator.py (see _load_plugin_env()).

Optional env vars:
    AGENT_CHILD_LOG_DIR   If set, specialist session logs are collected here
                          (named after their session name) instead of inside
                          this ticket's own directory — see
                          launch_specialist(). Resolved to an absolute path
                          and re-exported at startup so it means the same
                          location regardless of this process's own cwd.
"""

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import confluence_sync

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

SENTINEL_TIMEOUT = 7200  # 2 hours
SENTINEL_POLL_INTERVAL = 30

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# This plugin's install root — see orchestrator.py's _plugin_root() for why
# CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT/COPILOT_PLUGIN_ROOT are checked in that
# order, and why deriving it from this file's own location is the fallback.
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


def _load_plugin_env(plugin_root: Path) -> None:
    # See orchestrator.py's _load_plugin_env() for why this is needed: Claude
    # Code never exports userConfig to a Bash tool call, only to hook
    # processes and MCP/LSP server subprocesses, so bootstrap-env.sh (a
    # SessionStart hook) mirrors it into <plugin_root>/.env instead.
    env_file = plugin_root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


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


def _normalize_agent_child_log_dir() -> None:
    """Resolve AGENT_CHILD_LOG_DIR to an absolute path and re-export it. A
    relative value would otherwise resolve against whichever process reads
    it — this worker's own cwd (tickets/<KEY>/) already differs from the
    orchestrator's, so the same relative string would silently land in two
    different directories, defeating centralized log collection with no
    warning. Idempotent (resolving an already-absolute path is a no-op), so
    this is safe to call whether or not the orchestrator already did it
    before launching — including when worker.py is run standalone."""
    value = os.environ.get("AGENT_CHILD_LOG_DIR")
    if value:
        os.environ["AGENT_CHILD_LOG_DIR"] = str(Path(value).expanduser().resolve())


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


def jira_transition(key: str, status_name: str, auth, fields: dict | None = None) -> None:
    transition_id = TRANSITION_IDS[status_name]
    payload = {"transition": {"id": transition_id}}
    if fields:
        payload["fields"] = fields
    r = requests.post(
        f"{JIRA_BASE}/issue/{key}/transitions",
        json=payload,
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    log.info(f"{key}: transitioned to {status_name}")


# This workflow's Blocked transition (ID 21) has a required-at-execution-time
# validator on this field — Jira enforces it as a 400 regardless of what the
# transition metadata's own "required" flag reports (verified directly
# against the live API: GET .../transitions?expand=transitions.fields
# reports required=false for it, but POSTing the transition without it
# fails with "Blocked Reason is Required"). Its schema is a plain
# textfield custom field (verified the same way) — a bare string value,
# not the {"value": ...}/{"id": ...} shape a select-list field would need.
BLOCKED_REASON_FIELD = "customfield_16637"

# Jira's textfield custom field type has a hard 255-character limit —
# exceeding it fails the same transition with a different 400. Truncating
# here means a long Blocker section still transitions successfully; the
# ticket comment (not field-length-limited) always has the full text.
BLOCKED_REASON_MAX_LEN = 255


def _blocked_reason_field(reason: str) -> dict:
    reason = reason.strip()
    if len(reason) > BLOCKED_REASON_MAX_LEN:
        reason = reason[: BLOCKED_REASON_MAX_LEN - 1].rstrip() + "…"
    return {BLOCKED_REASON_FIELD: reason}


# The status a ticket should be moved back to once its Blocked reason is
# resolved — always the status it was in immediately before the Blocked
# transition, i.e. wherever the given stage runs from. Keyed by the same
# stage name used for the `stage`/`skill` argument at every call site below.
RESUME_STATUS_FOR_STAGE = {
    "ticket-discovery": "In Discovery",
    "ticket-implementation": "In Progress",
    "ticket-review": "In Progress",
    "ticket-merge": "UAT Review",
}

# Maps the same stage name used for `stage`/`skill` at every call site to
# the confluence_sync.ARTIFACT_TYPES key for that stage's artifact.
STAGE_TO_ARTIFACT_TYPE = {
    "ticket-discovery": "discovery",
    "ticket-implementation": "implementation",
    "ticket-review": "review",
    "ticket-merge": "merge",
}


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

def _extract_status_text(content: str) -> str:
    m = re.search(r"^\*\*Status:\*\*\s*(\w+)", content, re.MULTILINE)
    return m.group(1).upper() if m else "UNKNOWN"


def extract_status(artifact_path: Path) -> str:
    return _extract_status_text(artifact_path.read_text())


def extract_bold_field(content: str, name: str) -> str:
    m = re.search(rf"\*\*{re.escape(name)}:\*\*\s*(.+)", content)
    return m.group(1).strip() if m else ""


def extract_section(content: str, heading: str) -> str:
    m = re.search(rf"##\s*{re.escape(heading)}\s*\n+(.*?)(?:\n##|\Z)", content, re.DOTALL)
    return m.group(1).strip() if m else ""


def _confluence_pointer(confluence_url: str | None, fallback: str, separator: str = "") -> str:
    """The trailing "where to read the whole thing" line every
    artifact-derived Jira comment ends with: the Confluence page when the
    push succeeded, today's "see the .md file" wording otherwise (the .md
    file is still there and still authoritative either way).

    `fallback` differs per call site, and `separator` carries that call
    site's existing spacing (`""`, `" "`, or `"\\n"`) so it stays part of the
    pointer rather than being conditionally re-assembled at each of the six
    callers. An empty `fallback` means "say nothing at all when there's no
    URL" — used where the surrounding comment already names its artifact."""
    if confluence_url:
        return f"{separator}Full details: {confluence_url}"
    return f"{separator}{fallback}" if fallback else ""


def build_qa_review_comment(key: str, artifact: Path, confluence_url: str | None) -> str:
    """QA Review handoff comment, shared by the end of the discovery path
    and by a resumed re-entry into the QA Review gate — so both stay in
    sync automatically. Leads with the action needed (a reviewer who reads
    only the first line still knows what to do), then a short summary
    pulled straight from discovery.md, then a pointer to the full content —
    a Confluence link when the sync succeeded, the local filename
    otherwise. Action line varies: discovery can recommend closing outright
    (NO_CHANGES_NEEDED) instead of the default hand-off to implementation."""
    content = artifact.read_text()
    status = _extract_status_text(content)
    summary = extract_section(content, "TL;DR") or "(no summary found in discovery.md)"

    if status == "NO_CHANGES_NEEDED":
        action = (
            f"🤖 {key}: discovery found no code changes are needed — recommends closing this ticket.\n"
            f"• Approve: move to Done\n"
            f"• Reject: move back to In Discovery with a comment explaining what to revisit"
        )
    else:
        action = (
            f"🤖 {key}: discovery is complete and ready for review.\n"
            f"• Approve: move to In Progress\n"
            f"• Reject: move back to In Discovery with a comment explaining what to revisit"
        )

    pointer = _confluence_pointer(confluence_url, "See discovery.md for full findings.")
    return f"{action}\n\nSummary: {summary}\n\n{pointer}"


def build_in_review_comment(key: str, notes: Path, review_context: Path, confluence_url: str | None) -> str:
    """In Review handoff comment, shared by the end of the implementation
    path and by a resumed re-entry into the In Review gate — so both stay
    in sync, mirroring build_qa_review_comment()'s pattern. Action line
    varies: implementation can recommend closing outright
    (NO_CHANGES_NEEDED) instead of the default hand-off to PR review."""
    content = notes.read_text()
    status = _extract_status_text(content)

    if status == "NO_CHANGES_NEEDED":
        summary = extract_section(content, "PR Readiness") or "(no summary found in implementation-notes.md)"
        pointer = _confluence_pointer(confluence_url, "See implementation-notes.md for full findings.")
        return (
            f"🤖 {key}: implementation found no code changes are needed — recommends closing this ticket.\n"
            f"• Approve: move to Done\n"
            f"• Reject: move back to In Progress with a comment explaining what to revisit\n\n"
            f"Summary: {summary}\n\n{pointer}"
        )

    pr_url = extract_bold_field(review_context.read_text(), "PR URL") if review_context.exists() else ""
    suffix = f": {pr_url}" if pr_url else ""
    pointer = _confluence_pointer(confluence_url, "", separator=" ")
    return (
        f"🤖 {key} is ready for review{suffix}. When approved, move to UAT Review "
        f"to trigger merge. If you have comments to address, move back to In Progress.{pointer}"
    )

# ── Sub-agent launch ──────────────────────────────────────────────────────────

def launch_specialist(skill: str, ticket_dir: Path, repos_dir: Path, auth) -> None:
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
    orchestrator.py's launch_session() uses for the worker's own log.

    Each specialist is invoked as /jexpress:<skill> (namespaced), not by its
    bare name — see orchestrator.py's launch_session() docstring for why,
    and for why this alone doesn't fix plugin-enablement scoping."""
    agent_name = f"{ticket_dir.name}-{skill}"
    log_dir = Path(os.environ.get("AGENT_CHILD_LOG_DIR") or ticket_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / f"{agent_name}.log", "a")
    except OSError as e:
        # A misconfigured AGENT_CHILD_LOG_DIR (unwritable, or a path that's
        # already a plain file) must not crash this session with a bare
        # traceback and no Jira trail — every other failure in this script
        # goes through report_failure() so a human always sees something.
        report_failure(ticket_dir.name, f"could not prepare a log file for {skill} at {log_dir} ({e})", auth, stage=skill)
        return  # unreachable — report_failure() always exits
    proc = subprocess.Popen(
        ["claude", f"--name={agent_name}",
         "--permission-mode=bypassPermissions",
         f"--add-dir={ticket_dir}", f"--add-dir={repos_dir}",
         "-p", f"/jexpress:{skill} Repos directory: {repos_dir}"],
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

def report_failure(key: str, reason: str, auth, stage: str) -> None:
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
            jira_transition(key, "Blocked", auth, fields=_blocked_reason_field(reason))
            jira_comment(
                key,
                f"🤖 {key} has failed 3 times in a row with no progress — stopping "
                f"automatic retries. See the recent comments above and this session's logs for details.\n"
                f"Once resolved, move back to {RESUME_STATUS_FOR_STAGE[stage]} to continue.",
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

    confluence_url = confluence_sync.push(key, STAGE_TO_ARTIFACT_TYPE[stage], content, auth)
    pointer = _confluence_pointer(confluence_url, "", separator="\n")

    jira_transition(key, "Blocked", auth, fields=_blocked_reason_field(blocker))
    message = f"🤖 {stage} is blocked on {key}: {blocker}"
    if next_step:
        message += f"\n{next_step}"
    message += f"\nOnce resolved, move back to {RESUME_STATUS_FOR_STAGE[stage]} to continue.{pointer}"
    jira_comment(key, message, auth)

    log.warning(f"{key}: BLOCKED ({stage}) — {blocker}")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────

def run_discovery(key: str, ticket_dir: Path, repos_dir: Path, auth, skip_confluence_pull: bool = False) -> None:
    sentinel = ticket_dir / ".discovery-agent-done"
    artifact = ticket_dir / "discovery.md"

    # Unconditional, not resume-specific: on this ticket's very first-ever
    # discovery pass no Confluence page exists yet, so pull() returns None
    # and this is a no-op. Same code path handles "first run" and "revision
    # after a QA rejection" without needing to tell them apart.
    #
    # Pulled content is only ever applied to a file that ALREADY exists.
    # Elsewhere in this script, discovery.md/implementation-notes.md exist
    # if and only if that specialist actually wrote them — run_implementation()
    # dispatches on notes.exists(), and completed_stage_count() derives rewind
    # state from the same fact. A pull must not manufacture an artifact that
    # no specialist produced: right after a fresh 'To Do' restart, that would
    # write clear_all()'s own "Cleared — ticket restarted on ..." placeholder
    # into a brand-new discovery.md, which ticket-discovery would then read as
    # "this is a revision, find what to revise" instead of a genuine first
    # pass. Every intended pull-back (a QA-rejection revision, a
    # NO_CHANGES_NEEDED redo) always has the local file already there.
    if not skip_confluence_pull:
        try:
            pulled = confluence_sync.pull(key, "discovery", auth)
        except confluence_sync.ConfluencePullError as e:
            report_failure(key, f"could not fetch prior discovery.md content from Confluence: {e}", auth, stage="ticket-discovery")
            return
        if pulled is not None and artifact.exists():
            artifact.write_text(pulled)

    # Always run fresh — including the sentinel. A stale sentinel left over
    # from a prior discovery pass (e.g. a human rejected the ticket back
    # from QA Review to In Discovery) must not skip a redo: this status is
    # only ever reached again via that rejection path, so every visit here
    # is a legitimate request to re-run discovery, not a repeat within the
    # same invocation. discovery.md is deliberately NOT deleted first
    # (unlike review/merge's artifacts) — the specialist reads its own
    # prior discovery.md to know this is a revision; see
    # ticket-discovery/SKILL.md.
    sentinel.unlink(missing_ok=True)
    launch_specialist("ticket-discovery", ticket_dir, repos_dir, auth)
    if not wait_for_sentinel("ticket-discovery", sentinel):
        report_failure(key, "ticket-discovery did not complete within 900s", auth, stage="ticket-discovery")
        return

    if not artifact.exists():
        report_failure(key, "ticket-discovery finished but discovery.md is missing", auth, stage="ticket-discovery")
        return

    if extract_status(artifact) == "BLOCKED":
        apply_blocked_routing(key, artifact, "ticket-discovery", auth)
        return

    confluence_url = confluence_sync.push(key, "discovery", artifact.read_text(), auth)
    jira_transition(key, "QA Review", auth)
    jira_comment(key, build_qa_review_comment(key, artifact, confluence_url), auth)
    log.info(f"{key}: waiting for QA review")


def run_implementation(key: str, ticket_dir: Path, repos_dir: Path, auth, skip_confluence_pull: bool = False) -> None:
    notes = ticket_dir / "implementation-notes.md"
    review_context = ticket_dir / "review-context.md"

    # review-context.md is the real signal for "is there a PR to review" —
    # the implementation agent is the only thing that writes it, and only on
    # a genuine non-blocked, non-no-op completion (see _run_first_implementation_pass).
    # A prior NO_CHANGES_NEEDED or BLOCKED pass has notes.exists() == True but
    # never reached that write, so a human moving the ticket back to In
    # Progress means "redo implementation," not "address PR review comments"
    # (_run_review_pass launches the PR review agent, which has nothing to
    # review here). Route it through the same fresh-implementation path as a
    # first attempt.
    if not notes.exists() or not review_context.exists():
        _run_first_implementation_pass(key, ticket_dir, repos_dir, notes, review_context, auth, skip_confluence_pull)
    else:
        _run_review_pass(key, ticket_dir, repos_dir, review_context, auth)


def _run_first_implementation_pass(key: str, ticket_dir: Path, repos_dir: Path, notes: Path, review_context: Path, auth, skip_confluence_pull: bool = False) -> None:
    # Unconditional, not resume-specific — same reasoning as run_discovery's
    # pull: a genuine first attempt has no Confluence page yet, so pull()
    # returns None and this is a no-op. Applied only when notes already
    # exists, for the same reason as there — and here that existence means
    # this is a redo (NO_CHANGES_NEEDED or BLOCKED-before-PR — the two ways
    # run_implementation() dispatches here with notes already present),
    # which is precisely the case the pull-back is for.
    if not skip_confluence_pull:
        try:
            pulled = confluence_sync.pull(key, "implementation", auth)
        except confluence_sync.ConfluencePullError as e:
            report_failure(key, f"could not fetch prior implementation-notes.md content from Confluence: {e}", auth, stage="ticket-implementation")
            return
        if pulled is not None and notes.exists():
            notes.write_text(pulled)

    # Always run fresh — including the sentinel. A stale sentinel left over
    # from a prior attempt that reported done without producing
    # implementation-notes.md (a bug in that specialist, not something
    # expected in normal operation) must not skip a retry and just repeat
    # the same failure forever — see run_discovery()'s identical fix for
    # the full reasoning. notes.md itself is deliberately NOT unlinked here
    # — like discovery.md, a redo after a rejected NO_CHANGES_NEEDED or an
    # unresolved BLOCKED-before-PR reads its own prior version for context
    # (see run_implementation()'s dispatch, the only way this function runs
    # with notes already existing).
    sentinel = ticket_dir / ".implementation-agent-done"
    sentinel.unlink(missing_ok=True)
    launch_specialist("ticket-implementation", ticket_dir, repos_dir, auth)
    if not wait_for_sentinel("ticket-implementation", sentinel):
        report_failure(key, "ticket-implementation did not complete within 900s", auth, stage="ticket-implementation")
        return

    if not notes.exists():
        report_failure(key, "ticket-implementation finished but implementation-notes.md is missing", auth, stage="ticket-implementation")
        return

    status = extract_status(notes)
    if status == "BLOCKED":
        apply_blocked_routing(key, notes, "ticket-implementation", auth)
        return

    if status == "NO_CHANGES_NEEDED":
        confluence_url = confluence_sync.push(key, "implementation", notes.read_text(), auth)
        jira_transition(key, "In Review", auth)
        jira_comment(key, build_in_review_comment(key, notes, review_context, confluence_url), auth)
        log.info(f"{key}: implementation found no changes needed — waiting for human decision")
        return

    # The implementation agent writes review-context.md itself — it's the
    # only one with direct knowledge of which repo(s)/branch(es) it touched.
    # Missing after a non-BLOCKED, non-NO_CHANGES_NEEDED run is a bug in that
    # agent, not something to work around here.
    if not review_context.exists():
        report_failure(key, "ticket-implementation finished but review-context.md is missing", auth, stage="ticket-implementation")
        return

    confluence_url = confluence_sync.push(key, "implementation", notes.read_text(), auth)
    jira_transition(key, "In Review", auth)
    jira_comment(key, build_in_review_comment(key, notes, review_context, confluence_url), auth)
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

    launch_specialist("ticket-review", ticket_dir, repos_dir, auth)
    if not wait_for_sentinel("ticket-review", sentinel):
        report_failure(key, "ticket-review did not complete within 900s", auth, stage="ticket-review")
        return

    if not notes.exists():
        report_failure(key, "ticket-review finished but review-notes.md is missing", auth, stage="ticket-review")
        return

    if extract_status(notes) == "BLOCKED":
        apply_blocked_routing(key, notes, "ticket-review", auth)
        return

    pr_url = extract_bold_field(review_context.read_text(), "PR URL") if review_context.exists() else ""
    confluence_url = confluence_sync.push(key, "review", notes.read_text(), auth)
    pointer = _confluence_pointer(confluence_url, "", separator=" ")
    jira_transition(key, "In Review", auth)
    jira_comment(
        key,
        f"🤖 PR review pass complete for {key}. PR: {pr_url}\n"
        f"Comments addressed. Ready for approval — move to UAT Review when approved.\n"
        f"If you have more comments, move back to In Progress.{pointer}",
        auth,
    )
    log.info(f"{key}: waiting for PR approval")


def run_merge(key: str, ticket_dir: Path, repos_dir: Path, auth) -> None:
    notes = ticket_dir / "merge-notes.md"
    sentinel = ticket_dir / ".merge-agent-done"
    notes.unlink(missing_ok=True)  # always run fresh
    sentinel.unlink(missing_ok=True)

    launch_specialist("ticket-merge", ticket_dir, repos_dir, auth)
    if not wait_for_sentinel("ticket-merge", sentinel):
        report_failure(key, "ticket-merge did not complete within 900s", auth, stage="ticket-merge")
        return

    if not notes.exists():
        report_failure(key, "ticket-merge finished but merge-notes.md is missing", auth, stage="ticket-merge")
        return

    content = notes.read_text()
    status = extract_status(notes)
    # Pushed regardless of status/whether a comment follows — the Confluence
    # page should reflect whatever's actually on disk (spec: "Sync fires
    # whenever an artifact file is read").
    confluence_url = confluence_sync.push(key, "merge", content, auth)
    pointer = _confluence_pointer(confluence_url, "", separator=" ")

    if status == "SUCCESS":
        jira_transition(key, "Done", auth)
        jira_comment(key, f"🤖 Merge complete for {key}. Ticket resolved.{pointer}", auth)
        log.info(f"{key}: merge complete — ticket resolved")
    elif status == "BLOCKED":
        reason = extract_section(content, "Blocker") or "(no Blocker section found)"
        jira_transition(key, "Blocked", auth, fields=_blocked_reason_field(reason))
        jira_comment(
            key,
            f"🤖 Merge blocked for {key}: {reason}\n"
            f"Once resolved, move back to {RESUME_STATUS_FOR_STAGE['ticket-merge']} to continue.{pointer}",
            auth,
        )
        log.warning(f"{key}: merge blocked — {reason}")
    elif status == "PENDING":
        # Nothing is wrong, just not ready yet — no Jira transition, no
        # comment. The ticket stays at UAT Review exactly as it is; the next
        # orchestrator run resumes this session and checks again.
        reason = extract_section(content, "Reason")
        log.info(f"{key}: merge pending — {reason} — will check again next run")
    else:
        report_failure(key, f"ticket-merge returned unrecognized status '{status}'", auth, stage="ticket-merge")


def run_qa_review_gate(key: str, ticket_dir: Path, auth) -> None:
    """Human gate, resumed directly into QA Review — re-post the same
    handoff comment as the end of the discovery path, re-syncing to
    Confluence too since nothing else does at this resume point.
    discovery.md is guaranteed to exist here: reaching this status already
    passed sanity_check_and_rewind()'s stage-completion check."""
    artifact = ticket_dir / "discovery.md"
    confluence_url = confluence_sync.push(key, "discovery", artifact.read_text(), auth)
    jira_comment(key, build_qa_review_comment(key, artifact, confluence_url), auth)
    log.info(f"{key}: re-posted QA Review comment, waiting")


def run_in_review_gate(key: str, ticket_dir: Path, auth) -> None:
    """Human gate, resumed directly into In Review with no status change
    since the implementation/review path already posted the handoff
    comment — re-post it (and re-sync to Confluence) so a resumed session
    doesn't just exit silently. implementation-notes.md is guaranteed to
    exist here: reaching this status already passed
    sanity_check_and_rewind()'s stage-completion check. Shares
    build_in_review_comment() with the end of the implementation path so
    both stay in sync, including the NO_CHANGES_NEEDED variant."""
    notes = ticket_dir / "implementation-notes.md"
    review_context = ticket_dir / "review-context.md"
    confluence_url = confluence_sync.push(key, "implementation", notes.read_text(), auth)
    jira_comment(key, build_in_review_comment(key, notes, review_context, confluence_url), auth)
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
    aren't gated at all).

    A gap of exactly one stage (e.g. QA Review with no discovery.md yet) is
    the normal, expected case: a human moved a brand-new ticket straight to
    a later status, skipping one stage. A gap of two — In Review or UAT
    Review with *nothing* on disk, not even discovery.md — is a different
    situation: no human plausibly drags a totally untouched ticket that far
    ahead. Seeing that gap almost always means this session's `ticket_dir`
    doesn't actually point at the ticket's real directory (a cwd bug at
    launch — see the Aug 2026 incident notes in this ticket's own Jira
    history), not that the work was genuinely never done. Silently rewinding
    in that case redoes 15+ minutes of already-complete work and confuses
    anyone watching the ticket jump backward for no visible reason. Escalate
    to Blocked instead of auto-correcting when the gap is that large, so a
    human confirms what's actually going on before anything gets redone."""
    required = STAGE_REQUIREMENTS.get(status)
    if required is None:
        return status

    have = completed_stage_count(ticket_dir)
    if have >= required:
        return status

    gap = required - have
    if gap >= 2:
        reason = (
            f"Jira says '{status}' (requires {required} completed stage(s)) but this "
            f"session found {have} in {ticket_dir}. A gap this large usually means "
            f"the session's working directory doesn't actually point at this ticket's "
            f"real directory, not that the work was never done — stopping rather than "
            f"silently rewinding to In Discovery and redoing everything."
        )
        log.error(f"{key}: {reason}")
        jira_transition(key, "Blocked", auth, fields=_blocked_reason_field(reason))
        jira_comment(
            key,
            f"🤖 {key}: {reason}\n"
            f"Please verify {ticket_dir} actually contains this ticket's prior "
            f"discovery.md/implementation-notes.md before deciding how to proceed — "
            f"move back to '{status}' once confirmed, or to 'In Discovery' if the work "
            f"genuinely needs to be redone from scratch.",
            auth,
        )
        sys.exit(1)

    corrected = REWIND_STATUS_FOR_COUNT[have]
    log.warning(f"{key}: Jira says '{status}' but only {have}/{required} prerequisite stage(s) are done — rewinding to '{corrected}'")
    jira_transition(key, corrected, auth)
    return corrected

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_plugin_env(PLUGIN_ROOT)
    args = sys.argv[1:]
    skip_confluence_pull = "--skip-confluence-pull" in args
    positional = [a for a in args if not a.startswith("--")]
    if len(positional) != 1:
        log.error("usage: worker.py [--skip-confluence-pull] <repos-dir>")
        sys.exit(1)
    repos_dir = Path(positional[0])
    ticket_dir = Path.cwd()
    key = ticket_dir.name
    auth = _auth()
    _normalize_agent_child_log_dir()

    log.info(f"Session started for {key}")

    status = read_status(key, auth)
    log.info(f"Ticket {key}: status={status}")

    if status == "To Do":
        # Best-effort: blanks (doesn't delete) any of this key's existing
        # Confluence child pages from a prior attempt, so a restart doesn't
        # leave stale content visible under a page that looks current.
        confluence_sync.clear_all(key, auth)
        jira_transition(key, "In Discovery", auth)
        status = "In Discovery"

    status = sanity_check_and_rewind(key, status, ticket_dir, auth)

    if status == "In Discovery":
        run_discovery(key, ticket_dir, repos_dir, auth, skip_confluence_pull)
    elif status == "QA Review":
        run_qa_review_gate(key, ticket_dir, auth)
    elif status == "In Progress":
        run_implementation(key, ticket_dir, repos_dir, auth, skip_confluence_pull)
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
