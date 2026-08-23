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

import os
import sys
from datetime import date

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    import markdown as _markdown_lib
except ImportError:
    print("ERROR: 'markdown' is not installed. Run: pip install markdown", file=sys.stderr)
    sys.exit(1)

try:
    import markdownify as _markdownify_lib
except ImportError:
    print("ERROR: 'markdownify' is not installed. Run: pip install markdownify", file=sys.stderr)
    sys.exit(1)

# Same cloud ID as worker.py/orchestrator.py — each file keeps its own copy
# rather than sharing an import, matching existing precedent (orchestrator.py:63,
# worker.py:45).
CLOUD_ID = "e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2"
CONFLUENCE_V2_BASE = f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/api/v2"
CONFLUENCE_V1_BASE = f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/rest/api"

DEFAULT_SPACE_KEY = "PDE"
DEFAULT_PARENT_PAGE_ID = "5148311567"

# Each ticket's four possible child pages. "editable" controls which banner
# is prepended, and (in worker.py) which two of these ever get pulled back.
ARTIFACT_TYPES = {
    "discovery": {"title": "Discovery", "editable": True},
    "implementation": {"title": "Implementation Notes", "editable": True},
    "review": {"title": "Review Notes", "editable": False},
    "merge": {"title": "Merge Notes", "editable": False},
}

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


def render_storage_body(artifact_type: str, markdown_content: str) -> str:
    """Convert a .md artifact to Confluence storage format, with the
    editable/read-only banner as part of the same markdown document (so it
    converts to a real <p>/<hr> pair, not raw text dropped into HTML)."""
    banner_md = EDITABLE_BANNER if ARTIFACT_TYPES[artifact_type]["editable"] else READONLY_BANNER
    return _markdown_lib.markdown(banner_md + markdown_content, extensions=["fenced_code"])


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
    wording when this returns None."""
    try:
        title = ARTIFACT_TYPES[artifact_type]["title"]
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
    except Exception:
        return None


def pull(key: str, artifact_type: str, auth) -> str | None:
    """Returns the artifact's current Confluence content as markdown, or
    None if no such page exists yet (a legitimate no-op — e.g. this
    ticket's very first run). Raises ConfluencePullError for any other
    failure; callers must not treat that the same as "not found"."""
    try:
        parent = _find_page(auth, key, _parent_page_id())
        if not parent:
            return None
        title = ARTIFACT_TYPES[artifact_type]["title"]
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
    (that stage was never reached) is skipped, not created."""
    try:
        parent = _find_page(auth, key, _parent_page_id())
        if not parent:
            return
        placeholder = CLEARED_PLACEHOLDER.format(date=date.today().isoformat())
        for info in ARTIFACT_TYPES.values():
            existing = _find_page(auth, info["title"], parent["id"])
            if not existing:
                continue
            requests.put(
                f"{CONFLUENCE_V2_BASE}/pages/{existing['id']}",
                json={
                    "id": existing["id"],
                    "status": "current",
                    "title": info["title"],
                    "body": {"representation": "storage", "value": f"<p>{placeholder}</p>"},
                    "version": {"number": existing["version"] + 1},
                },
                auth=auth,
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
    except Exception:
        pass
