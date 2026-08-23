# JiraExpressAI Confluence Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync JiraExpressAI ticket artifacts (`discovery.md`, `implementation-notes.md`, `review-notes.md`, `merge-notes.md`) to Confluence for developer visibility, with `discovery.md`/`implementation-notes.md` editable from Confluence and read back into the pipeline on the next revision.

**Architecture:** A new `confluence_sync.py` module (imported directly by `worker.py`, no MCP/subprocess) talks to the Confluence REST API via `requests`, reusing the existing `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN`. `worker.py` calls `push()` at every point it already reads an artifact's content for a Jira comment, and calls `pull()` immediately before re-running discovery or a NO_CHANGES_NEEDED implementation redo. A parent page per ticket (title = ticket key) holds up to four child pages, found by CQL title search each time — no new local state file. Pull failures are treated as seriously as any other transient failure (reusing `report_failure()`); push failures are silently non-blocking.

**Tech Stack:** Python 3, `requests` (already a dependency), `markdown` (new), `markdownify` (new), `unittest` + `unittest.mock` (existing test convention).

**Spec:** `docs/superpowers/specs/2026-08-22-jexpress-confluence-sync-design.md`

## Global Constraints

- No MCP dependency — all Confluence access is direct REST via `requests`, matching how this plugin already talks to Jira (`ticket-worker/SKILL.md:104-109`).
- No new local state file in `tickets/<KEY>/` — every push/pull re-derives the target page via CQL title search (spec: "Idempotency — no new local state").
- Zero changes to any specialist `SKILL.md` (`ticket-discovery`, `ticket-implementation`, `ticket-review`, `ticket-merge`).
- Push is always best-effort/non-blocking; a push failure must never raise out of `worker.py`'s call sites and must never stop a Jira transition.
- Pull failure (a real error, not "page doesn't exist yet") must route through the existing `report_failure()` mechanism (`worker.py:392-432`) — it must not silently fall back to stale local content.
- Confluence config defaults, verified directly against the live instance: space key `PDE`, parent folder id `5148311567`, cloud id `e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2` (same constant already hardcoded in `orchestrator.py:63` / `worker.py:45`).
- Confluence page bodies use storage format (HTML); `.md` → HTML via `markdown` (`fenced_code` extension); HTML → `.md` via `markdownify`, for the two pullable artifacts only.

---

## File Structure

- **Create** `plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py` — all Confluence REST logic (push/pull/clear), no Jira knowledge.
- **Create** `plugins/jira-express-ai/tests/test_confluence_sync.py` — unit tests for the module above, mocking `requests`.
- **Modify** `plugins/jira-express-ai/skills/ticket-worker/worker.py` — import `confluence_sync`; add push calls at 5 existing artifact-read sites; add pull calls at 2 sites; add `--skip-confluence-pull` CLI flag; add `clear_all()` call at the fresh-`To Do` branch.
- **Modify** `plugins/jira-express-ai/tests/test_worker.py` — extend existing test classes to assert the above wiring, mocking `confluence_sync`.
- **Modify** `plugins/jira-express-ai/.claude-plugin/plugin.json` — add `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_PARENT_PAGE_ID` to `userConfig`.
- **Modify** `plugins/jira-express-ai/scripts/bootstrap-env.sh` — relay the two new vars into `.env`, same pattern as `REPOS_DIR`.
- **Modify** `plugins/jira-express-ai/README.md` — document the two new `pip install` dependencies and the two new config fields.
- **Modify** `plugins/jira-express-ai/skills/ticket-worker/SKILL.md` — widen the "Sandbox — hard limits" section to permit Confluence API calls; add the "continue anyway" judgment step.

---

### Task 1: Config plumbing + `confluence_sync.py` skeleton (constants, config, content conversion)

**Files:**
- Create: `plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py`
- Test: `plugins/jira-express-ai/tests/test_confluence_sync.py`
- Modify: `plugins/jira-express-ai/.claude-plugin/plugin.json`
- Modify: `plugins/jira-express-ai/scripts/bootstrap-env.sh`
- Modify: `plugins/jira-express-ai/README.md`

**Interfaces:**
- Produces: `confluence_sync.ARTIFACT_TYPES` (dict), `confluence_sync.ConfluencePullError` (exception class), `confluence_sync._space_key() -> str`, `confluence_sync._parent_page_id() -> str`, `confluence_sync.render_storage_body(artifact_type: str, markdown_content: str) -> str`, `confluence_sync._strip_banner(markdown_content: str, artifact_type: str) -> str`.

**Optional manual check before writing code (not an automated step — do it if you have real Atlassian credentials in your environment, skip otherwise and revisit if Task 3's live behavior looks wrong):**

```bash
source plugins/jira-express-ai/.env 2>/dev/null  # or export ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN yourself
curl -s -u "$ATLASSIAN_EMAIL:$ATLASSIAN_API_TOKEN" \
  "https://api.atlassian.com/ex/confluence/e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2/wiki/api/v2/spaces?keys=PDE"
```
Expected: a JSON body with `"results": [{"id": "929781", "key": "PDE", ...}]`. If this 401s, Basic Auth doesn't work against this gateway path the way it does for Jira, and `CONFLUENCE_V2_BASE`/`CONFLUENCE_V1_BASE` below need to point at `https://chghealthcare.atlassian.net/wiki/...` directly instead — flag this immediately rather than discovering it after Task 5.

- [ ] **Step 1: Write the failing tests for config helpers and content conversion**

```python
# plugins/jira-express-ai/tests/test_confluence_sync.py
import os
import sys
import unittest
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "ticket-worker"
sys.path.insert(0, str(_SKILL_DIR))

import confluence_sync  # noqa: E402


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("CONFLUENCE_SPACE_KEY", "CONFLUENCE_PARENT_PAGE_ID",
                      "CLAUDE_PLUGIN_OPTION_CONFLUENCE_SPACE_KEY",
                      "CLAUDE_PLUGIN_OPTION_CONFLUENCE_PARENT_PAGE_ID")
        }

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_space_key_defaults_to_pde(self) -> None:
        self.assertEqual(confluence_sync._space_key(), "PDE")

    def test_space_key_reads_env_override(self) -> None:
        os.environ["CONFLUENCE_SPACE_KEY"] = "OTHERSPACE"
        self.assertEqual(confluence_sync._space_key(), "OTHERSPACE")

    def test_parent_page_id_defaults(self) -> None:
        self.assertEqual(confluence_sync._parent_page_id(), "5148311567")

    def test_parent_page_id_reads_env_override(self) -> None:
        os.environ["CONFLUENCE_PARENT_PAGE_ID"] = "999"
        self.assertEqual(confluence_sync._parent_page_id(), "999")


class TestRenderStorageBody(unittest.TestCase):
    def test_editable_artifact_gets_editable_banner(self) -> None:
        body = confluence_sync.render_storage_body("discovery", "# Hello\n\nSome text.")
        self.assertIn("read back into the ticket", body)
        self.assertIn("<h1>Hello</h1>", body)

    def test_readonly_artifact_gets_readonly_banner(self) -> None:
        body = confluence_sync.render_storage_body("review", "# Hello")
        self.assertIn("read-only mirror", body)

    def test_fenced_code_block_converts(self) -> None:
        body = confluence_sync.render_storage_body("discovery", "```python\nx = 1\n```")
        self.assertIn("<pre>", body)
        self.assertIn("x = 1", body)


class TestStripBanner(unittest.TestCase):
    def test_strips_editable_banner_round_trip(self) -> None:
        original = "## Summary\n\nSome findings here."
        html = confluence_sync.render_storage_body("discovery", original)
        markdown_back = confluence_sync._markdownify_lib.markdownify(html)
        stripped = confluence_sync._strip_banner(markdown_back, "discovery")
        self.assertNotIn("read back into the ticket", stripped)
        self.assertIn("Summary", stripped)
        self.assertIn("Some findings here.", stripped)

    def test_leaves_content_unchanged_if_banner_missing(self) -> None:
        content = "## Summary\n\nNo banner here at all."
        stripped = confluence_sync._strip_banner(content, "discovery")
        self.assertEqual(stripped, content)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'confluence_sync'`

- [ ] **Step 3: Install the two new dependencies**

```bash
pip install markdown markdownify
```

- [ ] **Step 4: Write `confluence_sync.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Add the two new `userConfig` fields**

In `plugins/jira-express-ai/.claude-plugin/plugin.json`, add to the `userConfig` object (after `REPOS_DIR`):

```json
    "CONFLUENCE_SPACE_KEY": {
      "type": "string",
      "title": "Confluence space key",
      "description": "Confluence space where ticket artifact pages are synced. Defaults to PDE if unset.",
      "required": false
    },
    "CONFLUENCE_PARENT_PAGE_ID": {
      "type": "string",
      "title": "Confluence parent folder/page ID",
      "description": "ID of the Confluence folder or page under which each ticket's parent page is created. Defaults to 5148311567 if unset.",
      "required": false
    }
```

- [ ] **Step 7: Relay the two new vars in `bootstrap-env.sh`**

In `plugins/jira-express-ai/scripts/bootstrap-env.sh`, extend the `grep -vE` exclusion list and the write block:

```bash
      grep -vE '^(ATLASSIAN_EMAIL|ATLASSIAN_API_TOKEN|REPOS_DIR|CONFLUENCE_SPACE_KEY|CONFLUENCE_PARENT_PAGE_ID)=' "$ENV_FILE" || true
```
(replacing the existing `grep -vE` line), and add two more conditional `echo` lines alongside the existing three:

```bash
    [ -n "${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SPACE_KEY:-}" ] && echo "CONFLUENCE_SPACE_KEY=${CLAUDE_PLUGIN_OPTION_CONFLUENCE_SPACE_KEY}"
    [ -n "${CLAUDE_PLUGIN_OPTION_CONFLUENCE_PARENT_PAGE_ID:-}" ] && echo "CONFLUENCE_PARENT_PAGE_ID=${CLAUDE_PLUGIN_OPTION_CONFLUENCE_PARENT_PAGE_ID}"
```

Also update the `if [ -n ... ]` guard a few lines above to include the two new `CLAUDE_PLUGIN_OPTION_*` vars, so the file still gets written when only these (and not the Atlassian/repo ones) are configured.

- [ ] **Step 8: Update README.md**

In `plugins/jira-express-ai/README.md`, near the existing line 152-154 dependency note, add: `markdown` and `markdownify` are also required (`pip install markdown markdownify`), and document the two new optional config fields (space key, parent page/folder id) with their defaults (`PDE`, `5148311567`).

- [ ] **Step 9: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py \
        plugins/jira-express-ai/tests/test_confluence_sync.py \
        plugins/jira-express-ai/.claude-plugin/plugin.json \
        plugins/jira-express-ai/scripts/bootstrap-env.sh \
        plugins/jira-express-ai/README.md
git commit -m "jexpress: add confluence_sync config, banner rendering, and stripping"
```

---

### Task 2: `confluence_sync.py` — page lookup and space/parent resolution

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py`
- Test: `plugins/jira-express-ai/tests/test_confluence_sync.py`

**Interfaces:**
- Consumes: `CONFLUENCE_V1_BASE`, `CONFLUENCE_V2_BASE`, `_space_key()`, `_parent_page_id()` (Task 1).
- Produces: `_space_id(auth) -> str`, `_find_page(auth, title: str, ancestor_id: str) -> dict | None` (returns `{"id": str, "version": int}`), `_get_or_create_parent(key: str, auth) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to plugins/jira-express-ai/tests/test_confluence_sync.py
from unittest.mock import MagicMock, patch


def _mock_response(json_data, status_ok=True):
    resp = MagicMock()
    resp.json.return_value = json_data
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.HTTPError("boom")
    return resp


class TestSpaceId(unittest.TestCase):
    @patch("confluence_sync.requests.get")
    def test_resolves_space_id(self, mock_get) -> None:
        mock_get.return_value = _mock_response({"results": [{"id": "929781", "key": "PDE"}]})
        self.assertEqual(confluence_sync._space_id(("e", "t")), "929781")

    @patch("confluence_sync.requests.get")
    def test_raises_when_space_not_found(self, mock_get) -> None:
        mock_get.return_value = _mock_response({"results": []})
        with self.assertRaises(confluence_sync.ConfluencePullError):
            confluence_sync._space_id(("e", "t"))


class TestFindPage(unittest.TestCase):
    @patch("confluence_sync.requests.get")
    def test_returns_none_when_not_found(self, mock_get) -> None:
        mock_get.return_value = _mock_response({"results": []})
        self.assertIsNone(confluence_sync._find_page(("e", "t"), "PDE-1234", "5148311567"))

    @patch("confluence_sync.requests.get")
    def test_returns_id_and_version_when_found(self, mock_get) -> None:
        mock_get.return_value = _mock_response(
            {"results": [{"id": "42", "version": {"number": 3}}]}
        )
        result = confluence_sync._find_page(("e", "t"), "PDE-1234", "5148311567")
        self.assertEqual(result, {"id": "42", "version": 3})


class TestGetOrCreateParent(unittest.TestCase):
    @patch("confluence_sync._find_page")
    def test_returns_existing_parent_id(self, mock_find) -> None:
        mock_find.return_value = {"id": "100", "version": 1}
        self.assertEqual(confluence_sync._get_or_create_parent("PDE-1234", ("e", "t")), "100")

    @patch("confluence_sync.requests.post")
    @patch("confluence_sync._space_id")
    @patch("confluence_sync._find_page")
    def test_creates_parent_when_missing(self, mock_find, mock_space_id, mock_post) -> None:
        mock_find.return_value = None
        mock_space_id.return_value = "929781"
        mock_post.return_value = _mock_response({"id": "200"})
        page_id = confluence_sync._get_or_create_parent("PDE-1234", ("e", "t"))
        self.assertEqual(page_id, "200")
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["title"], "PDE-1234")
        self.assertEqual(kwargs["json"]["parentId"], "5148311567")
```

Add `import requests` to the top of the test file (needed for `requests.HTTPError` in `_mock_response`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: FAIL — `AttributeError: module 'confluence_sync' has no attribute '_space_id'` (and similarly for `_find_page`/`_get_or_create_parent`)

- [ ] **Step 3: Implement the three functions**

Append to `confluence_sync.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: PASS (11 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py plugins/jira-express-ai/tests/test_confluence_sync.py
git commit -m "jexpress: add confluence page lookup and parent creation"
```

---

### Task 3: `confluence_sync.py` — `push()`

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py`
- Test: `plugins/jira-express-ai/tests/test_confluence_sync.py`

**Interfaces:**
- Consumes: `_get_or_create_parent`, `_find_page`, `render_storage_body`, `_space_id`, `_space_key` (Tasks 1-2).
- Produces: `push(key: str, artifact_type: str, markdown_content: str, auth) -> str | None`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to plugins/jira-express-ai/tests/test_confluence_sync.py
class TestPush(unittest.TestCase):
    @patch("confluence_sync.requests.post")
    @patch("confluence_sync._find_page")
    @patch("confluence_sync._get_or_create_parent")
    def test_creates_child_page_when_missing(self, mock_parent, mock_find, mock_post) -> None:
        mock_parent.return_value = "100"
        mock_find.return_value = None  # no existing "Discovery" child
        mock_post.return_value = _mock_response({"id": "300"})
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertEqual(url, "https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300")
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["parentId"], "100")
        self.assertEqual(kwargs["json"]["title"], "Discovery")

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    @patch("confluence_sync._get_or_create_parent")
    def test_updates_existing_child_page_with_fresh_version(self, mock_parent, mock_find, mock_put) -> None:
        mock_parent.return_value = "100"
        mock_find.return_value = {"id": "300", "version": 5}
        mock_put.return_value = _mock_response({"id": "300"})
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings v2", ("e", "t"))
        self.assertEqual(url, "https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300")
        _, kwargs = mock_put.call_args
        self.assertEqual(kwargs["json"]["version"]["number"], 6)

    @patch("confluence_sync._get_or_create_parent")
    def test_returns_none_on_any_failure(self, mock_parent) -> None:
        mock_parent.side_effect = RuntimeError("network down")
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertIsNone(url)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: FAIL — `AttributeError: module 'confluence_sync' has no attribute 'push'`

- [ ] **Step 3: Implement `push()`**

Append to `confluence_sync.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: PASS (14 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py plugins/jira-express-ai/tests/test_confluence_sync.py
git commit -m "jexpress: add confluence push (best-effort create-or-update)"
```

---

### Task 4: `confluence_sync.py` — `pull()`

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py`
- Test: `plugins/jira-express-ai/tests/test_confluence_sync.py`

**Interfaces:**
- Consumes: `_find_page`, `_parent_page_id`, `_strip_banner`, `ConfluencePullError` (Tasks 1-2).
- Produces: `pull(key: str, artifact_type: str, auth) -> str | None` (raises `ConfluencePullError` on a real failure).

- [ ] **Step 1: Write the failing tests**

```python
# Append to plugins/jira-express-ai/tests/test_confluence_sync.py
class TestPull(unittest.TestCase):
    @patch("confluence_sync._find_page")
    def test_returns_none_when_no_parent_page(self, mock_find) -> None:
        mock_find.return_value = None
        self.assertIsNone(confluence_sync.pull("PDE-1234", "discovery", ("e", "t")))

    @patch("confluence_sync._find_page")
    def test_returns_none_when_no_child_page(self, mock_find) -> None:
        mock_find.side_effect = [{"id": "100", "version": 1}, None]
        self.assertIsNone(confluence_sync.pull("PDE-1234", "discovery", ("e", "t")))

    @patch("confluence_sync.requests.get")
    @patch("confluence_sync._find_page")
    def test_returns_stripped_markdown_when_found(self, mock_find, mock_get) -> None:
        mock_find.side_effect = [{"id": "100", "version": 1}, {"id": "300", "version": 2}]
        html = confluence_sync.render_storage_body("discovery", "## Summary\n\nHuman edit here.")
        mock_get.return_value = _mock_response({"body": {"storage": {"value": html}}})
        result = confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))
        self.assertIn("Human edit here.", result)
        self.assertNotIn("read back into the ticket", result)

    @patch("confluence_sync.requests.get")
    @patch("confluence_sync._find_page")
    def test_raises_pull_error_on_api_failure(self, mock_find, mock_get) -> None:
        mock_find.side_effect = [{"id": "100", "version": 1}, {"id": "300", "version": 2}]
        mock_get.return_value = _mock_response({}, status_ok=False)
        with self.assertRaises(confluence_sync.ConfluencePullError):
            confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: FAIL — `AttributeError: module 'confluence_sync' has no attribute 'pull'`

- [ ] **Step 3: Implement `pull()`**

Append to `confluence_sync.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: PASS (18 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py plugins/jira-express-ai/tests/test_confluence_sync.py
git commit -m "jexpress: add confluence pull with distinct not-found vs error handling"
```

---

### Task 5: `confluence_sync.py` — `clear_all()`

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py`
- Test: `plugins/jira-express-ai/tests/test_confluence_sync.py`

**Interfaces:**
- Consumes: `_find_page`, `_parent_page_id`, `ARTIFACT_TYPES`, `CLEARED_PLACEHOLDER` (Tasks 1-2).
- Produces: `clear_all(key: str, auth) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to plugins/jira-express-ai/tests/test_confluence_sync.py
class TestClearAll(unittest.TestCase):
    @patch("confluence_sync._find_page")
    def test_noop_when_no_parent_page(self, mock_find) -> None:
        mock_find.return_value = None
        confluence_sync.clear_all("PDE-1234", ("e", "t"))  # must not raise

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    def test_clears_only_existing_children(self, mock_find, mock_put) -> None:
        # Parent found; only "Discovery" child exists, the other three don't.
        def find_side_effect(auth, title, ancestor_id):
            if title == "PDE-1234":
                return {"id": "100", "version": 1}
            if title == "Discovery":
                return {"id": "300", "version": 4}
            return None
        mock_find.side_effect = find_side_effect
        mock_put.return_value = _mock_response({"id": "300"})
        confluence_sync.clear_all("PDE-1234", ("e", "t"))
        self.assertEqual(mock_put.call_count, 1)
        _, kwargs = mock_put.call_args
        self.assertEqual(kwargs["json"]["version"]["number"], 5)
        self.assertIn("Cleared", kwargs["json"]["body"]["value"])

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    def test_swallows_errors(self, mock_find, mock_put) -> None:
        mock_find.side_effect = RuntimeError("network down")
        confluence_sync.clear_all("PDE-1234", ("e", "t"))  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: FAIL — `AttributeError: module 'confluence_sync' has no attribute 'clear_all'`

- [ ] **Step 3: Implement `clear_all()`**

Append to `confluence_sync.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_confluence_sync.py -v`
Expected: PASS (21 tests total)

- [ ] **Step 5: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/confluence_sync.py plugins/jira-express-ai/tests/test_confluence_sync.py
git commit -m "jexpress: add confluence clear_all for fresh ticket restarts"
```

---

### Task 6: `worker.py` — wire push into the discovery path

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/worker.py:1-35` (imports), `:282-307` (`build_qa_review_comment`), `:454-483` (`run_discovery`), `:626-633` (`run_qa_review_gate`)
- Test: `plugins/jira-express-ai/tests/test_worker.py`

**Interfaces:**
- Consumes: `confluence_sync.push(key, artifact_type, markdown_content, auth) -> str | None` (Task 3).
- Produces: `build_qa_review_comment(key: str, artifact: Path, confluence_url: str | None) -> str` (signature change — was `(key, artifact)`).

- [ ] **Step 1: Write the failing tests**

Find the existing test class covering `build_qa_review_comment`/`run_discovery` in `test_worker.py` (search for `build_qa_review_comment` and `run_discovery` to find its exact class name and existing fixture helpers — reuse the same `TempDirTestCase` base and existing `discovery.md` fixture content already used by nearby tests, don't invent a new fixture). Add:

```python
    @patch("worker.confluence_sync.push")
    def test_qa_review_comment_includes_confluence_link_on_success(self, mock_push) -> None:
        mock_push.return_value = "https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300"
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** READY\n\n## Summary\n\nFound the bug.\n")
        comment = worker.build_qa_review_comment("PDE-1234", artifact, "https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300")
        self.assertIn("Full details: https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300", comment)
        self.assertNotIn("See discovery.md", comment)

    def test_qa_review_comment_falls_back_when_no_confluence_url(self) -> None:
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** READY\n\n## Summary\n\nFound the bug.\n")
        comment = worker.build_qa_review_comment("PDE-1234", artifact, None)
        self.assertIn("See discovery.md for full findings.", comment)

    @patch("worker.jira_comment")
    @patch("worker.jira_transition")
    @patch("worker.confluence_sync.push")
    @patch("worker.confluence_sync.pull")
    @patch("worker.wait_for_sentinel", return_value=True)
    @patch("worker.launch_specialist")
    def test_run_discovery_pushes_to_confluence(self, mock_launch, mock_wait, mock_pull, mock_push, mock_transition, mock_comment) -> None:
        mock_pull.return_value = None
        mock_push.return_value = "https://example.atlassian.net/wiki/x"
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** READY\n\n## Summary\n\nFound it.\n")
        worker.run_discovery("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)
        mock_push.assert_called_once_with("PDE-1234", "discovery", artifact.read_text(), self.auth)
        args, _ = mock_comment.call_args
        self.assertIn("https://example.atlassian.net/wiki/x", args[1])
```

Locate the existing `run_discovery` test class and place the new test alongside it, inheriting the same base class (so `self.ticket_dir`/`self.repos_dir`/`self.auth` are available) — check the existing tests in that class for how they already mock `wait_for_sentinel`/`launch_specialist`/`jira_transition`/`jira_comment` and match that exact patch-target style (`"worker.jira_transition"` etc., since `test_worker.py` imports `worker` as a module, not `from worker import *`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v -k "confluence"`
Expected: FAIL — `build_qa_review_comment() takes 2 positional arguments but 3 were given`, and `AttributeError: module 'worker' has no attribute 'confluence_sync'`

- [ ] **Step 3: Import `confluence_sync` and update `build_qa_review_comment`**

In `worker.py`, add near the top (after the existing `from pathlib import Path`, before the `try: import requests` block is fine too — place it right after that try/except so any missing-dependency error surfaces the same way):

```python
import confluence_sync
```

Replace `build_qa_review_comment` (worker.py:282-307):

```python
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
    summary = extract_section(content, "Summary") or "(no summary found in discovery.md)"

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

    pointer = f"Full details: {confluence_url}" if confluence_url else "See discovery.md for full findings."
    return f"{action}\n\nSummary: {summary}\n\n{pointer}"
```

- [ ] **Step 4: Wire `push()` into `run_discovery` and `run_qa_review_gate`**

In `run_discovery` (worker.py:454-483), replace the tail (from the `BLOCKED` check onward):

```python
    if extract_status(artifact) == "BLOCKED":
        apply_blocked_routing(key, artifact, "ticket-discovery", auth)
        return

    confluence_url = confluence_sync.push(key, "discovery", artifact.read_text(), auth)
    jira_transition(key, "QA Review", auth)
    jira_comment(key, build_qa_review_comment(key, artifact, confluence_url), auth)
    log.info(f"{key}: waiting for QA review")
```

Replace `run_qa_review_gate` (worker.py:626-633):

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v`
Expected: PASS — all prior tests still pass (confirms the signature change didn't break other callers) plus the 3 new ones

- [ ] **Step 6: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/worker.py plugins/jira-express-ai/tests/test_worker.py
git commit -m "jexpress: sync discovery.md to confluence and link it from the QA Review comment"
```

---

### Task 7: `worker.py` — wire push into the implementation/review path

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/worker.py:310-333` (`build_in_review_comment`), `:501-544` (`_run_first_implementation_pass`), `:546-581` (`_run_review_pass`), `:636-648` (`run_in_review_gate`)
- Test: `plugins/jira-express-ai/tests/test_worker.py`

**Interfaces:**
- Consumes: `confluence_sync.push` (Task 3).
- Produces: `build_in_review_comment(key: str, notes: Path, review_context: Path, confluence_url: str | None) -> str` (signature change).

- [ ] **Step 1: Write the failing tests**

Find the existing test class(es) covering `build_in_review_comment`/`_run_first_implementation_pass`/`_run_review_pass` and add, following the same patch-target conventions already used there:

```python
    def test_in_review_comment_no_changes_needed_falls_back_without_url(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNothing to do.\n")
        review_context = self.ticket_dir / "review-context.md"
        comment = worker.build_in_review_comment("PDE-1234", notes, review_context, None)
        self.assertIn("See implementation-notes.md for full findings.", comment)

    def test_in_review_comment_includes_confluence_link(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNothing to do.\n")
        review_context = self.ticket_dir / "review-context.md"
        comment = worker.build_in_review_comment("PDE-1234", notes, review_context, "https://example/x")
        self.assertIn("Full details: https://example/x", comment)

    def test_in_review_comment_ready_for_review_includes_confluence_link(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** READY\n\n## PR Readiness\n\nGood to go.\n")
        review_context = self.ticket_dir / "review-context.md"
        review_context.write_text("**PR URL:** https://github.com/chghealthcare/repo/pull/1\n")
        comment = worker.build_in_review_comment("PDE-1234", notes, review_context, "https://example/x")
        self.assertIn("Full details: https://example/x", comment)

    @patch("worker.jira_comment")
    @patch("worker.jira_transition")
    @patch("worker.confluence_sync.push")
    @patch("worker.confluence_sync.pull")
    @patch("worker.extract_bold_field", return_value="")
    @patch("worker.wait_for_sentinel", return_value=True)
    @patch("worker.launch_specialist")
    def test_run_review_pass_pushes_to_confluence(self, mock_launch, mock_wait, mock_bold, mock_pull, mock_push, mock_transition, mock_comment) -> None:
        mock_pull.return_value = None
        mock_push.return_value = "https://example/review"
        notes = self.ticket_dir / "review-notes.md"
        notes.write_text("**Status:** RESOLVED\n\n## Comments Addressed\n\nNone.\n")
        review_context = self.ticket_dir / "review-context.md"
        review_context.write_text("**PR URL:** https://github.com/chghealthcare/repo/pull/1\n")

        def write_notes(*args, **kwargs):
            notes.write_text("**Status:** RESOLVED\n\n## Comments Addressed\n\nNone.\n")
        mock_launch.side_effect = write_notes

        worker._run_review_pass("PDE-1234", self.ticket_dir, self.repos_dir, review_context, self.auth)
        mock_push.assert_called_once_with("PDE-1234", "review", notes.read_text(), self.auth)
        args, _ = mock_comment.call_args
        self.assertIn("https://example/review", args[1])
```

Check the existing test file for how it already fakes a specialist "writing its artifact" during `launch_specialist` mocks (there's likely an existing helper/pattern for this in the current `_run_first_implementation_pass`/`_run_review_pass` tests) — reuse that pattern instead of the inline `write_notes` above if one already exists, to stay consistent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v -k "in_review or review_pass"`
Expected: FAIL — `build_in_review_comment() takes 3 positional arguments but 4 were given`, and `_run_review_pass` not calling `confluence_sync.push`

- [ ] **Step 3: Update `build_in_review_comment`**

Replace worker.py:310-333:

```python
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
        pointer = f"Full details: {confluence_url}" if confluence_url else "See implementation-notes.md for full findings."
        return (
            f"🤖 {key}: implementation found no code changes are needed — recommends closing this ticket.\n"
            f"• Approve: move to Done\n"
            f"• Reject: move back to In Progress with a comment explaining what to revisit\n\n"
            f"Summary: {summary}\n\n{pointer}"
        )

    pr_url = extract_bold_field(review_context.read_text(), "PR URL") if review_context.exists() else ""
    suffix = f": {pr_url}" if pr_url else ""
    pointer = f" Full details: {confluence_url}" if confluence_url else ""
    return (
        f"🤖 {key} is ready for review{suffix}. When approved, move to UAT Review "
        f"to trigger merge. If you have comments to address, move back to In Progress.{pointer}"
    )
```

- [ ] **Step 4: Wire `push()` into `_run_first_implementation_pass`, `_run_review_pass`, `run_in_review_gate`**

In `_run_first_implementation_pass` (worker.py:501-544), replace the two tail blocks:

```python
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
```

In `_run_review_pass` (worker.py:546-581), replace the tail:

```python
    pr_url = extract_bold_field(review_context.read_text(), "PR URL") if review_context.exists() else ""
    confluence_url = confluence_sync.push(key, "review", notes.read_text(), auth)
    pointer = f" Full details: {confluence_url}" if confluence_url else ""
    jira_transition(key, "In Review", auth)
    jira_comment(
        key,
        f"🤖 PR review pass complete for {key}. PR: {pr_url}\n"
        f"Comments addressed. Ready for approval — move to UAT Review when approved.\n"
        f"If you have more comments, move back to In Progress.{pointer}",
        auth,
    )
    log.info(f"{key}: waiting for PR approval")
```

Replace `run_in_review_gate` (worker.py:636-648):

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v`
Expected: PASS — all tests, including previously-passing ones

- [ ] **Step 6: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/worker.py plugins/jira-express-ai/tests/test_worker.py
git commit -m "jexpress: sync implementation/review notes to confluence and link them from comments"
```

---

### Task 8: `worker.py` — wire push into `apply_blocked_routing` and `run_merge` (the bypass gotcha)

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/worker.py:210-219` (`RESUME_STATUS_FOR_STAGE`, add `STAGE_TO_ARTIFACT_TYPE` alongside it), `:435-450` (`apply_blocked_routing`), `:584-623` (`run_merge`)
- Test: `plugins/jira-express-ai/tests/test_worker.py`

**Interfaces:**
- Consumes: `confluence_sync.push` (Task 3).
- Produces: `STAGE_TO_ARTIFACT_TYPE: dict[str, str]` (maps `"ticket-discovery"`/`"ticket-implementation"`/`"ticket-review"`/`"ticket-merge"` to `"discovery"`/`"implementation"`/`"review"`/`"merge"`).

This is the call site explicitly flagged in the spec as easy to miss: `run_merge` never calls `apply_blocked_routing` — it has its own independent inline handling for all three of its outcomes, so the sync call is added separately here, not "for free" via Task 6/7's shared-function change.

- [ ] **Step 1: Write the failing tests**

```python
    @patch("worker.jira_comment")
    @patch("worker.jira_transition")
    @patch("worker.confluence_sync.push")
    def test_apply_blocked_routing_pushes_to_confluence(self, mock_push, mock_transition, mock_comment) -> None:
        mock_push.return_value = "https://example/blocked"
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** BLOCKED\n\n## Blocker\n\nNeed access.\n")
        with self.assertRaises(SystemExit):
            worker.apply_blocked_routing("PDE-1234", artifact, "ticket-discovery", self.auth)
        mock_push.assert_called_once_with("PDE-1234", "discovery", artifact.read_text(), self.auth)
        args, _ = mock_comment.call_args
        self.assertIn("https://example/blocked", args[1])

    @patch("worker.jira_comment")
    @patch("worker.jira_transition")
    @patch("worker.confluence_sync.push")
    @patch("worker.wait_for_sentinel", return_value=True)
    @patch("worker.launch_specialist")
    def test_run_merge_success_pushes_to_confluence(self, mock_launch, mock_wait, mock_push, mock_transition, mock_comment) -> None:
        mock_push.return_value = "https://example/merge"
        notes = self.ticket_dir / "merge-notes.md"

        def write_notes(*args, **kwargs):
            notes.write_text("**Status:** SUCCESS\n\n**PR:** #1 (https://x)\n")
        mock_launch.side_effect = write_notes

        worker.run_merge("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)
        mock_push.assert_called_once_with("PDE-1234", "merge", notes.read_text(), self.auth)
        args, _ = mock_comment.call_args
        self.assertIn("https://example/merge", args[1])

    @patch("worker.jira_comment")
    @patch("worker.jira_transition")
    @patch("worker.confluence_sync.push")
    @patch("worker.wait_for_sentinel", return_value=True)
    @patch("worker.launch_specialist")
    def test_run_merge_blocked_pushes_to_confluence(self, mock_launch, mock_wait, mock_push, mock_transition, mock_comment) -> None:
        mock_push.return_value = "https://example/merge-blocked"
        notes = self.ticket_dir / "merge-notes.md"

        def write_notes(*args, **kwargs):
            notes.write_text("**Status:** BLOCKED\n\n## Blocker\n\nMerge conflict.\n")
        mock_launch.side_effect = write_notes

        worker.run_merge("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)
        mock_push.assert_called_once_with("PDE-1234", "merge", notes.read_text(), self.auth)
        args, _ = mock_comment.call_args
        self.assertIn("https://example/merge-blocked", args[1])
```

Check the existing `run_merge` test class for its established way of faking `launch_specialist` writing `merge-notes.md` (there is likely one already, given `run_merge` is already tested) and reuse that pattern rather than the inline `write_notes` closures above if a shared helper already exists.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v -k "blocked_routing or run_merge"`
Expected: FAIL — `mock_push` not called (0 calls)

- [ ] **Step 3: Add `STAGE_TO_ARTIFACT_TYPE` and wire `apply_blocked_routing`**

Add next to `RESUME_STATUS_FOR_STAGE` (worker.py:214-219):

```python
# Maps the same stage name used for `stage`/`skill` at every call site to
# the confluence_sync.ARTIFACT_TYPES key for that stage's artifact.
STAGE_TO_ARTIFACT_TYPE = {
    "ticket-discovery": "discovery",
    "ticket-implementation": "implementation",
    "ticket-review": "review",
    "ticket-merge": "merge",
}
```

Replace `apply_blocked_routing` (worker.py:435-450):

```python
def apply_blocked_routing(key: str, artifact_path: Path, stage: str, auth) -> None:
    """A specialist deliberately saying it can't proceed — always escalate
    immediately, on the first occurrence. Always exits."""
    content = artifact_path.read_text()
    blocker = extract_section(content, "Blocker") or "(no Blocker section found)"
    next_step = extract_section(content, "Suggested Next Step")

    confluence_url = confluence_sync.push(key, STAGE_TO_ARTIFACT_TYPE[stage], content, auth)
    pointer = f"\nFull details: {confluence_url}" if confluence_url else ""

    jira_transition(key, "Blocked", auth, fields=_blocked_reason_field(blocker))
    message = f"🤖 {stage} is blocked on {key}: {blocker}"
    if next_step:
        message += f"\n{next_step}"
    message += f"\nOnce resolved, move back to {RESUME_STATUS_FOR_STAGE[stage]} to continue.{pointer}"
    jira_comment(key, message, auth)

    log.warning(f"{key}: BLOCKED ({stage}) — {blocker}")
    sys.exit(1)
```

- [ ] **Step 4: Wire `run_merge`'s three independent branches**

Replace `run_merge` (worker.py:584-623):

```python
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
    pointer = f" Full details: {confluence_url}" if confluence_url else ""

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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v`
Expected: PASS — all tests

- [ ] **Step 6: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/worker.py plugins/jira-express-ai/tests/test_worker.py
git commit -m "jexpress: sync blocked and merge outcomes to confluence (run_merge bypasses apply_blocked_routing)"
```

---

### Task 9: `worker.py` — pull-back, `report_failure` integration, CLI flag, fresh-restart clear

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/worker.py:454-544` (`run_discovery`, `run_implementation`, `_run_first_implementation_pass`), `:684-720` (`main`)
- Test: `plugins/jira-express-ai/tests/test_worker.py`

**Interfaces:**
- Consumes: `confluence_sync.pull`, `confluence_sync.clear_all`, `confluence_sync.ConfluencePullError` (Tasks 4-5); `report_failure` (existing, worker.py:392).
- Produces: `run_discovery(key, ticket_dir, repos_dir, auth, skip_confluence_pull: bool = False)`, `run_implementation(key, ticket_dir, repos_dir, auth, skip_confluence_pull: bool = False)`, `_run_first_implementation_pass(key, ticket_dir, repos_dir, notes, review_context, auth, skip_confluence_pull: bool = False)` (all three signature changes — new optional trailing param, existing callers in this file must be updated to match).

- [ ] **Step 1: Write the failing tests**

```python
    @patch("worker.jira_comment")
    @patch("worker.jira_transition")
    @patch("worker.confluence_sync.push", return_value=None)
    @patch("worker.confluence_sync.pull")
    @patch("worker.wait_for_sentinel", return_value=True)
    @patch("worker.launch_specialist")
    def test_run_discovery_pulls_before_launching_specialist(self, mock_launch, mock_wait, mock_pull, mock_push, mock_transition, mock_comment) -> None:
        mock_pull.return_value = "## Summary\n\nHuman-edited findings.\n**Status:** READY\n"
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** READY\n\n## Summary\n\nOriginal AI findings.\n")

        launched_with_content = {}
        def capture_content(*args, **kwargs):
            launched_with_content["at_launch"] = artifact.read_text()
        mock_launch.side_effect = capture_content

        worker.run_discovery("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)
        mock_pull.assert_called_once_with("PDE-1234", "discovery", self.auth)
        self.assertIn("Human-edited findings.", launched_with_content["at_launch"])

    @patch("worker.confluence_sync.pull")
    @patch("worker.launch_specialist")
    def test_run_discovery_skips_pull_when_flag_set(self, mock_launch, mock_pull) -> None:
        mock_launch.side_effect = lambda *a, **k: None  # sentinel never appears; that's fine, we exit before checking
        with patch("worker.wait_for_sentinel", return_value=False), patch("worker.report_failure") as mock_report:
            mock_report.side_effect = SystemExit(1)
            with self.assertRaises(SystemExit):
                worker.run_discovery("PDE-1234", self.ticket_dir, self.repos_dir, self.auth, skip_confluence_pull=True)
        mock_pull.assert_not_called()

    @patch("worker.report_failure")
    @patch("worker.confluence_sync.pull")
    @patch("worker.launch_specialist")
    def test_run_discovery_routes_pull_error_through_report_failure(self, mock_launch, mock_pull, mock_report) -> None:
        mock_pull.side_effect = confluence_sync.ConfluencePullError("timeout")
        mock_report.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            worker.run_discovery("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)
        mock_launch.assert_not_called()
        args, kwargs = mock_report.call_args
        self.assertEqual(kwargs.get("stage") or args[3], "ticket-discovery")
        self.assertIn("Confluence", args[1])

    @patch("worker.confluence_sync.clear_all")
    @patch("worker.sanity_check_and_rewind", side_effect=lambda key, status, td, auth: status)
    @patch("worker.run_discovery")
    @patch("worker.read_status", return_value="To Do")
    @patch("worker.jira_transition")
    @patch("worker._auth")
    @patch("worker._normalize_agent_child_log_dir")
    @patch("worker._load_plugin_env")
    def test_main_clears_confluence_on_fresh_to_do(self, mock_env, mock_norm, mock_auth, mock_transition, mock_status, mock_run_discovery, mock_rewind, mock_clear) -> None:
        mock_auth.return_value = self.auth
        with patch("sys.argv", ["worker.py", str(self.repos_dir)]), patch("pathlib.Path.cwd", return_value=self.ticket_dir):
            worker.main()
        mock_clear.assert_called_once_with(self.ticket_dir.name, self.auth)

    @patch("worker._auth")
    @patch("worker._load_plugin_env")
    def test_main_parses_skip_confluence_pull_flag(self, mock_env, mock_auth) -> None:
        mock_auth.return_value = self.auth
        with patch("worker.read_status", return_value="Done"), \
             patch("worker._normalize_agent_child_log_dir"), \
             patch("sys.argv", ["worker.py", "--skip-confluence-pull", str(self.repos_dir)]), \
             patch("pathlib.Path.cwd", return_value=self.ticket_dir):
            worker.main()  # "Done" status just logs and returns — this test only
                            # confirms argv parsing doesn't error out on the flag
```

Add `import confluence_sync` to the top of `test_worker.py` (needed for `confluence_sync.ConfluencePullError` in the third test) — it's already importable since `sys.path` was extended for `worker`'s own directory at the top of the file.

Check the existing test file for how `main()` is already tested (if at all) — if there's an existing `TestMain` class, add these there and match its established mocking conventions instead of introducing a new style.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v -k "pull or skip_confluence or fresh_to_do"`
Expected: FAIL — `run_discovery() got an unexpected keyword argument 'skip_confluence_pull'`, `confluence_sync.clear_all` not called, `usage: worker.py <repos-dir>` error on the flag

- [ ] **Step 3: Update `run_discovery` to pull before launching**

Replace worker.py:454-483 in full:

```python
def run_discovery(key: str, ticket_dir: Path, repos_dir: Path, auth, skip_confluence_pull: bool = False) -> None:
    sentinel = ticket_dir / ".discovery-agent-done"
    artifact = ticket_dir / "discovery.md"

    # Unconditional, not resume-specific: on this ticket's very first-ever
    # discovery pass no Confluence page exists yet, so pull() returns None
    # and this is a no-op. Same code path handles "first run" and "revision
    # after a QA rejection" without needing to tell them apart.
    if not skip_confluence_pull:
        try:
            pulled = confluence_sync.pull(key, "discovery", auth)
        except confluence_sync.ConfluencePullError as e:
            report_failure(key, f"could not fetch prior discovery.md content from Confluence: {e}", auth, stage="ticket-discovery")
            return
        if pulled is not None:
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
```

- [ ] **Step 4: Update `run_implementation` and `_run_first_implementation_pass` to thread the flag and pull**

Replace worker.py:486-544 in full:

```python
def run_implementation(key: str, ticket_dir: Path, repos_dir: Path, auth, skip_confluence_pull: bool = False) -> None:
    notes = ticket_dir / "implementation-notes.md"
    review_context = ticket_dir / "review-context.md"

    # A prior NO_CHANGES_NEEDED pass has notes.exists() == True but no PR —
    # a human rejecting it back to In Progress means "redo implementation,"
    # not "address PR review comments" (_run_review_pass launches the PR
    # review agent, which has nothing to review here). Route it through the
    # same fresh-implementation path as a first attempt.
    if not notes.exists() or extract_status(notes) == "NO_CHANGES_NEEDED":
        _run_first_implementation_pass(key, ticket_dir, repos_dir, notes, review_context, auth, skip_confluence_pull)
    else:
        _run_review_pass(key, ticket_dir, repos_dir, review_context, auth)


def _run_first_implementation_pass(key: str, ticket_dir: Path, repos_dir: Path, notes: Path, review_context: Path, auth, skip_confluence_pull: bool = False) -> None:
    # Unconditional, not resume-specific — same reasoning as run_discovery's
    # pull: a genuine first attempt has no Confluence page yet, so pull()
    # returns None and this is a no-op.
    if not skip_confluence_pull:
        try:
            pulled = confluence_sync.pull(key, "implementation", auth)
        except confluence_sync.ConfluencePullError as e:
            report_failure(key, f"could not fetch prior implementation-notes.md content from Confluence: {e}", auth, stage="ticket-implementation")
            return
        if pulled is not None:
            notes.write_text(pulled)

    # Always run fresh — including the sentinel. A stale sentinel left over
    # from a prior attempt that reported done without producing
    # implementation-notes.md (a bug in that specialist, not something
    # expected in normal operation) must not skip a retry and just repeat
    # the same failure forever — see run_discovery()'s identical fix for
    # the full reasoning. notes.md itself is deliberately NOT unlinked here
    # — like discovery.md, a redo after a rejected NO_CHANGES_NEEDED reads
    # its own prior version for context (see run_implementation()'s dispatch,
    # which is the only way this function runs with notes already existing).
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
```

- [ ] **Step 5: Update `main()`: CLI flag parsing, threading it through, and the fresh-restart clear**

Replace `main()` (worker.py:684-720):

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd plugins/jira-express-ai && python3 -m pytest tests/test_worker.py -v`
Expected: PASS — all tests

- [ ] **Step 7: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/worker.py plugins/jira-express-ai/tests/test_worker.py
git commit -m "jexpress: pull confluence edits back before discovery/implementation re-runs; clear pages on fresh restart"
```

---

### Task 10: `ticket-worker/SKILL.md` — sandbox permission and the "continue anyway" judgment step

**Files:**
- Modify: `plugins/jira-express-ai/skills/ticket-worker/SKILL.md` (the "Sandbox — hard limits" section, and the "Role" numbered steps before it runs `worker.py`)

**Interfaces:**
- Consumes: nothing code-level — this is a prose/behavior change to the skill instructions, not to `worker.py`.
- Produces: the documented `--skip-confluence-pull` invocation path for `worker.py` (Task 9's flag), now actually reachable by the skill layer.

No automated tests — this is instructions read by an LLM session, not code. Verification is a self-review checklist at the end of this task.

- [ ] **Step 1: Widen the sandbox's Jira-only API restriction**

In `ticket-worker/SKILL.md`, find:

```
- Call the Jira REST API for `<KEY>` (read, transition, comment)

You may not:
- Access files outside `tickets/<KEY>/` (except reading `$REPOS_DIR` to pass to sub-agents)
- Call any API other than Jira
```

Replace with:

```
- Call the Jira REST API for `<KEY>` (read, transition, comment)
- Call the Confluence REST API for `<KEY>`'s pages under the configured
  space/parent folder only (read, create, update) — see
  `confluence_sync.py`; this is `worker.py`'s own concern, not something
  you call directly

You may not:
- Access files outside `tickets/<KEY>/` (except reading `$REPOS_DIR` to pass to sub-agents)
- Call any API other than Jira and Confluence, and only as described above
```

- [ ] **Step 2: Add the "continue anyway" judgment step before running `worker.py`**

In `ticket-worker/SKILL.md`'s "Role" section, the numbered list currently reads (steps 1-3, extract repos dir / run worker.py / report output). Insert a new step 2 (renumbering the current 2-3 to 3-4), between "extract repos directory" and "run worker.py":

```
2. Check whether this ticket is currently stalled on a Confluence pull
   failure: fetch the ticket's recent Jira comments and look for a
   `🤖 ⚠️` comment whose reason mentions "Confluence" that hasn't since been
   followed by a normal progress comment. If you find one, read the most
   recent human comment on the ticket. This is a judgment call, not a
   keyword match — decide whether that comment actually authorizes
   proceeding without the pending Confluence edit (e.g. "go ahead", "never
   mind, just continue", "that edit doesn't matter, keep going") as opposed
   to an unrelated comment that happens to appear after the failure. If it
   does, pass `--skip-confluence-pull` to `worker.py` in the next step. If
   you're not sure, don't pass it — the default (retry the pull) is always
   safe; skipping it is not.
```

Update the (now step 3, was step 2) `worker.py` invocation line to show the conditional flag:

```
python3 "$CLAUDE_PLUGIN_ROOT/skills/ticket-worker/worker.py" \
  ${SKIP_CONFLUENCE_PULL:+--skip-confluence-pull} "$REPOS_DIR"
```

- [ ] **Step 3: Self-review checklist**

Read through the full updated `ticket-worker/SKILL.md` and confirm:
- The "Sandbox" section's Confluence carve-out is scoped to "this key's pages under the configured space/parent folder" — not blanket Confluence access.
- The new step 2 is explicit that this is a judgment call, matching the reasoning already given in `ticket-worker/SKILL.md:54-58` for why this skill stays a real `claude -p` session rather than a bare script.
- Nothing here contradicts `JIRA_EXPRESS_AI_TRUST_CONTRACT.md`'s treatment of ticket content/comments as untrusted data — the new step reads a human's own Jira comment to decide on a narrow, reversible action (skip one pull, not "do what any comment says"), consistent with that contract rather than an exception to it. If `JIRA_EXPRESS_AI_TRUST_CONTRACT.md` needs a cross-reference added here, add one; otherwise leave it as is.

- [ ] **Step 4: Commit**

```bash
git add plugins/jira-express-ai/skills/ticket-worker/SKILL.md
git commit -m "jexpress: permit scoped Confluence access and add the confluence-pull-skip judgment step"
```

---

## Post-plan cleanup (not a task — do this yourself, not via a subagent)

While verifying the folder-as-parent assumption during planning, a temporary page titled `jexpress-confluence-sync-parent-test-DELETE-ME` (id `5148311571`) was created directly under the target folder (`5148311567`) in the live PDE space to confirm the API accepts a folder as a page parent (confirmed: `"parentType": "folder"`). Delete it manually from Confluence — no delete tool was available in this session to do it automatically.
