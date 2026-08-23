#!/usr/bin/env python3
"""
Confluence sync for JiraExpressAI ticket artifacts.

Mirrors discovery.md / implementation-notes.md / review-notes.md /
merge-notes.md to Confluence for developer visibility, and (discovery and
implementation only) reads human edits back for the next revision. See
docs/superpowers/specs/2026-08-22-jexpress-confluence-sync-design.md for the
full design this implements.

Imported directly by worker.py — not a subprocess, not an MCP call. Talks to
the Confluence REST API via `requests`, reusing the same
ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN worker.py already requires for Jira.
"""

import logging
import os
import re
import sys
from datetime import date

# Same convention as worker.py's own module-level logger. Best-effort sync
# failures are swallowed (see push()/clear_all()) but never silent: without a
# log line, a permanent misconfiguration (a colliding page title, a 403, a
# wrong space key) is indistinguishable from Confluence being briefly
# unreachable.
log = logging.getLogger(__name__)

# `requests` is a hard dependency of this whole plugin — worker.py needs it
# for Jira regardless of whether Confluence sync works — so a missing
# `requests` exits here exactly as it does in worker.py.
try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# The two rendering libraries are different in kind: they're needed only for
# Confluence sync, which is best-effort by design. Exiting here would kill
# worker.py during `import confluence_sync` — before any Jira work, comment,
# transition, or report_failure — so a plugin upgrade without
# `pip install markdown markdownify` would stall every in-flight ticket,
# including merges and human-approval gates that have nothing to do with
# Confluence. Fail soft instead and treat it like any other sync failure.
_MISSING_RENDERING_DEPS = []
try:
    import markdown as _markdown_lib
except ImportError:
    _markdown_lib = None
    _MISSING_RENDERING_DEPS.append("markdown")

try:
    import markdownify as _markdownify_lib
except ImportError:
    _markdownify_lib = None
    _MISSING_RENDERING_DEPS.append("markdownify")

_RENDERING_AVAILABLE = not _MISSING_RENDERING_DEPS
_RENDERING_UNAVAILABLE_REASON = (
    "Confluence sync is unavailable: {} not installed (run: pip install {})".format(
        " and ".join(f"'{d}'" for d in _MISSING_RENDERING_DEPS) or "rendering dependencies",
        " ".join(_MISSING_RENDERING_DEPS) or "markdown markdownify",
    )
)

# Same cloud ID as worker.py/orchestrator.py — each file keeps its own copy
# rather than sharing an import, matching existing precedent (orchestrator.py:63,
# worker.py:45).
CLOUD_ID = "e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2"
CONFLUENCE_V2_BASE = f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/api/v2"
CONFLUENCE_V1_BASE = f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/rest/api"

DEFAULT_SPACE_KEY = "PDE"
DEFAULT_PARENT_PAGE_ID = "5148311567"

# Each ticket's four possible child pages. "label" is the human-readable
# artifact name; the actual page title is always ticket-key-namespaced by
# _child_title() (see there for why). "editable" controls which banner is
# prepended, and (in worker.py) which two of these ever get pulled back.
ARTIFACT_TYPES = {
    "discovery": {"label": "Discovery", "editable": True},
    "implementation": {"label": "Implementation Notes", "editable": True},
    "review": {"label": "Review Notes", "editable": False},
    "merge": {"label": "Merge Notes", "editable": False},
}

# Confluence enforces unique page titles per *space*, not per parent — so a
# bare "Discovery"/"Implementation Notes" child title could only ever be
# claimed by one ticket in the whole space (and, verified against the live
# PDE space, an unrelated "Implementation Notes" page already exists there).
# Every other ticket's create would 400 forever, silently swallowed by
# push()'s best-effort handler. Namespacing with the ticket key makes each
# title unique. The parent page's own title stays the bare key — already
# unique in practice.
CHILD_TITLE_SEPARATOR = " — "


def _child_title(key: str, artifact_type: str) -> str:
    return f"{key}{CHILD_TITLE_SEPARATOR}{ARTIFACT_TYPES[artifact_type]['label']}"

EDITABLE_BANNER = (
    "✏️ Edits made on this page are read back into the ticket the next "
    "time this stage runs (e.g. after a QA rejection or a 'no changes needed' "
    "redo). Feel free to revise, correct, or add context here.\n\n---\n\n"
)
READONLY_BANNER = (
    "\U0001F4CC This page is a read-only mirror for reference — it's "
    "regenerated fresh each run and edits here are not read back into the "
    "pipeline.\n\n---\n\n"
)

CLEARED_PLACEHOLDER = (
    "Cleared — ticket restarted on {date}. See this page's version "
    "history for the previous attempt."
)


class ConfluencePullError(Exception):
    """A pull attempt failed for a reason other than 'page doesn't exist
    yet'. Callers must not treat this the same as a not-found no-op — a
    human's Confluence-only edit could be silently lost that way."""


def _space_key() -> str:
    return (
        os.environ.get("CONFLUENCE_SPACE_KEY", "").strip()
        or os.environ.get("CLAUDE_PLUGIN_OPTION_CONFLUENCE_SPACE_KEY", "").strip()
        or DEFAULT_SPACE_KEY
    )


def _parent_page_id() -> str:
    return (
        os.environ.get("CONFLUENCE_PARENT_PAGE_ID", "").strip()
        or os.environ.get("CLAUDE_PLUGIN_OPTION_CONFLUENCE_PARENT_PAGE_ID", "").strip()
        or DEFAULT_PARENT_PAGE_ID
    )


# A fenced code block's opening/closing delimiter, indented up to 3 spaces
# (CommonMark's limit, and what python-markdown's fenced_code accepts).
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _escape_angle_brackets_outside_code_spans(text: str) -> str:
    """Escape every bare `<` in one line except inside a backtick code span,
    where python-markdown already escapes it correctly on its own."""
    parts: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            ticks = text[i:j]
            close = text.find(ticks, j)
            if close == -1:  # unterminated run — not a code span, just text
                parts.append(ticks)
                i = j
            else:
                parts.append(text[i:close + len(ticks)])
                i = close + len(ticks)
        elif ch == "<":
            parts.append("&lt;")
            i += 1
        else:
            parts.append(ch)
            i += 1
    return "".join(parts)


def _escape_bare_angle_brackets(markdown_text: str) -> str:
    """Pre-escape bare `<` in markdown source so literal placeholder text
    (`<KEY>`, `<path>`, `<repo>`, `<summary of ...>` — used throughout the
    specialists' artifact templates) survives as text instead of being
    treated as raw HTML.

    Verified empirically against the real templates: without this,
    python-markdown treats `<summary of whether...>` as the start of a raw
    HTML *block* and stops parsing markdown for the rest of the document —
    every heading and pipe table after it comes out as unstructured literal
    text — and emits unclosed pseudo-tags like `<KEY>` verbatim, which
    Confluence's strict storage-format parser rejects with a 400 (silently
    swallowed by push()). Pre-escaping the source (rather than
    post-processing the HTML) is what preserves the document structure;
    post-processing can only fix the tags, not the parse that was already
    abandoned.

    Only `<` needs escaping: markdown escapes a bare `>` to `&gt;` on its
    own, and escaping `>` would break blockquote syntax. Fenced code blocks
    and inline code spans are skipped — markdown already escapes their
    contents correctly, and touching them would render a literal `&lt;`.
    Two deliberate, documented trade-offs: an autolink (`<https://x>`)
    becomes literal text rather than a link, and a `<` inside a
    4-space-*indented* (non-fenced) code block renders as a literal `&lt;`.
    Every artifact template uses fenced blocks and bare URLs."""
    out: list[str] = []
    fence: str | None = None
    for line in markdown_text.split("\n"):
        m = _FENCE_RE.match(line)
        if fence is None:
            # A backtick fence's info string may not itself contain backticks.
            if m and (m.group("fence")[0] == "~" or "`" not in m.group("info")):
                fence = m.group("fence")
                out.append(line)
                continue
            out.append(_escape_angle_brackets_outside_code_spans(line))
        else:
            closes = (
                m
                and m.group("fence")[0] == fence[0]
                and len(m.group("fence")) >= len(fence)
                and not m.group("info").strip()
            )
            if closes:
                fence = None
            out.append(line)  # inside a fence: markdown escapes it correctly
    return "\n".join(out)


def render_storage_body(artifact_type: str, markdown_content: str) -> str:
    """Convert a .md artifact to Confluence storage format, with the
    editable/read-only banner as part of the same markdown document (so it
    converts to a real <p>/<hr> pair, not raw text dropped into HTML).

    `tables` is enabled because the real artifact templates
    (`ticket-implementation`'s "Changes Made", `ticket-review`'s "Comments
    Addressed") are pipe tables, which render as one unstructured blob
    without it. See _escape_bare_angle_brackets() for why the source is
    pre-escaped first."""
    banner_md = EDITABLE_BANNER if ARTIFACT_TYPES[artifact_type]["editable"] else READONLY_BANNER
    source = _escape_bare_angle_brackets(banner_md + markdown_content)
    return _markdown_lib.markdown(source, extensions=["fenced_code", "tables"])


def _strip_banner(markdown_content: str, artifact_type: str) -> str:
    """Reverse of the banner half of render_storage_body(), applied to
    content that's already been round-tripped through markdownify. Cuts
    everything through the first horizontal-rule line if the banner's
    distinctive first line is still present; otherwise returns the content
    unchanged (a human may have deleted or rewritten the banner)."""
    banner_text = EDITABLE_BANNER if ARTIFACT_TYPES[artifact_type]["editable"] else READONLY_BANNER
    banner_first_line = banner_text.split("\n", 1)[0].strip()
    lines = markdown_content.splitlines()
    if lines and banner_first_line in lines[0]:
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() in ("---", "***", "___"):
                return "\n".join(lines[i + 1:]).lstrip("\n")
    return markdown_content


def _space_id(auth) -> str:
    r = requests.get(
        f"{CONFLUENCE_V2_BASE}/spaces",
        params={"keys": _space_key()},
        auth=auth,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    results = r.json()["results"]
    if not results:
        raise ConfluencePullError(f"no Confluence space found for key '{_space_key()}'")
    return results[0]["id"]


def _find_page(auth, title: str, ancestor_id: str) -> dict | None:
    """CQL title search scoped to a specific ancestor (a page or a folder —
    both are valid CQL ancestors). Returns {"id": str, "version": int} or
    None if no match."""
    cql = f'title = "{title}" and ancestor = {ancestor_id}'
    r = requests.get(
        f"{CONFLUENCE_V1_BASE}/content/search",
        params={"cql": cql, "expand": "version"},
        auth=auth,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return None
    return {"id": results[0]["id"], "version": results[0]["version"]["number"]}


def _get_or_create_parent(key: str, auth) -> str:
    existing = _find_page(auth, key, _parent_page_id())
    if existing:
        return existing["id"]
    r = requests.post(
        f"{CONFLUENCE_V2_BASE}/pages",
        json={
            "spaceId": _space_id(auth),
            "status": "current",
            "title": key,
            "parentId": _parent_page_id(),
            "body": {
                "representation": "storage",
                "value": f'<p><a href="https://chghealthcare.atlassian.net/browse/{key}">{key} in Jira</a></p>',
            },
        },
        auth=auth,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["id"]


def push(key: str, artifact_type: str, markdown_content: str, auth) -> str | None:
    """Best-effort. Returns the child page's web URL on success, None on any
    failure — callers fall back to today's 'see the .md file' comment
    wording when this returns None. Every failure is logged: swallowed is
    not the same as untraceable."""
    if not _RENDERING_AVAILABLE:
        log.warning(f"{key}: {_RENDERING_UNAVAILABLE_REASON} — skipping push ({artifact_type})")
        return None
    try:
        title = _child_title(key, artifact_type)
        body = render_storage_body(artifact_type, markdown_content)
        parent_id = _get_or_create_parent(key, auth)
        existing = _find_page(auth, title, parent_id)
        if existing:
            r = requests.put(
                f"{CONFLUENCE_V2_BASE}/pages/{existing['id']}",
                json={
                    "id": existing["id"],
                    "status": "current",
                    "title": title,
                    "body": {"representation": "storage", "value": body},
                    "version": {"number": existing["version"] + 1},
                },
                auth=auth,
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
            page_id = existing["id"]
        else:
            r = requests.post(
                f"{CONFLUENCE_V2_BASE}/pages",
                json={
                    "spaceId": _space_id(auth),
                    "status": "current",
                    "title": title,
                    "parentId": parent_id,
                    "body": {"representation": "storage", "value": body},
                },
                auth=auth,
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
            page_id = r.json()["id"]
        return f"https://chghealthcare.atlassian.net/wiki/spaces/{_space_key()}/pages/{page_id}"
    except Exception as e:
        log.warning(f"{key}: Confluence push failed ({artifact_type}): {e}")
        return None


def pull(key: str, artifact_type: str, auth) -> str | None:
    """Returns the artifact's current Confluence content as markdown, or
    None if no such page exists yet (a legitimate no-op — e.g. this
    ticket's very first run). Raises ConfluencePullError for any other
    failure; callers must not treat that the same as "not found".

    Missing rendering dependencies are a real failure here, deliberately
    asymmetric with push()/clear_all()'s early return: pushing without
    rendering is just "can't sync, move on", but *pulling* without it could
    silently discard a human's Confluence-only edit. That's exactly what
    ConfluencePullError exists to escalate."""
    if not _RENDERING_AVAILABLE:
        raise ConfluencePullError(_RENDERING_UNAVAILABLE_REASON)
    try:
        parent = _find_page(auth, key, _parent_page_id())
        if not parent:
            return None
        title = _child_title(key, artifact_type)
        existing = _find_page(auth, title, parent["id"])
        if not existing:
            return None
        r = requests.get(
            f"{CONFLUENCE_V2_BASE}/pages/{existing['id']}",
            params={"body-format": "storage"},
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        html = r.json()["body"]["storage"]["value"]
    except Exception as e:
        raise ConfluencePullError(str(e)) from e

    markdown_content = _markdownify_lib.markdownify(html)
    return _strip_banner(markdown_content, artifact_type)


def clear_all(key: str, auth) -> None:
    """Best-effort. Called once, at the moment worker.py detects a genuine
    fresh 'To Do' restart. Blanks each existing child page's content with a
    placeholder rather than deleting it, so Confluence's own page version
    history keeps the previous attempt's content. A missing child page
    (that stage was never reached) is skipped, not created. One page failing
    doesn't stop the others from being cleared; every failure is logged."""
    if not _RENDERING_AVAILABLE:
        log.warning(f"{key}: {_RENDERING_UNAVAILABLE_REASON} — skipping page clear")
        return
    try:
        parent = _find_page(auth, key, _parent_page_id())
        if not parent:
            return
    except Exception as e:
        log.warning(f"{key}: Confluence clear failed (parent page lookup): {e}")
        return

    placeholder = CLEARED_PLACEHOLDER.format(date=date.today().isoformat())
    for artifact_type in ARTIFACT_TYPES:
        title = _child_title(key, artifact_type)
        try:
            existing = _find_page(auth, title, parent["id"])
            if not existing:
                continue
            requests.put(
                f"{CONFLUENCE_V2_BASE}/pages/{existing['id']}",
                json={
                    "id": existing["id"],
                    "status": "current",
                    "title": title,
                    "body": {"representation": "storage", "value": f"<p>{placeholder}</p>"},
                    "version": {"number": existing["version"] + 1},
                },
                auth=auth,
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
        except Exception as e:
            log.warning(f"{key}: Confluence clear failed ({artifact_type}): {e}")
            continue
