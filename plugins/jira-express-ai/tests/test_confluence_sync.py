import os
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "ticket-worker"
sys.path.insert(0, str(_SKILL_DIR))

import confluence_sync  # noqa: E402


def setUpModule() -> None:
    # Pre-empt _ensure_rendering_available()'s self-install attempt for
    # every test in this file except TestEnsureRenderingAvailable's own
    # (which explicitly resets this and always mocks subprocess.run). In an
    # environment where markdown/markdownify are genuinely missing, without
    # this a single test calling the real push()/pull()/clear_all() would
    # trigger one real, network-dependent `pip install` subprocess call as
    # a side effect of running the test suite. This is a no-op when the
    # real packages ARE present, since _RENDERING_AVAILABLE is already True.
    confluence_sync._SELF_INSTALL_ATTEMPTED = True


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


class TestRenderStorageBodyWellFormedness(unittest.TestCase):
    """Confluence's storage-format parser is strict XHTML: an unclosed
    pseudo-tag from literal `<KEY>`-style placeholder text is a 400 that
    push() swallows silently. Storage format allows several top-level
    elements, so well-formedness is checked with a single wrapper root."""

    TEMPLATE = (
        "# Implementation Notes: <KEY>\n\n"
        "**Status:** READY\n"
        "**Date:** <ISO date>\n\n"
        "## Changes Made\n\n"
        "| File | Change |\n"
        "|------|--------|\n"
        "| `<path>` | <description> |\n\n"
        "## PR Readiness\n\n"
        "<summary of whether changes are ready to merge, any caveats>\n\n"
        "## Notes\n\n"
        "Touched `$REPOS_DIR/<repo>/` and 2 < 4 here.\n\n"
        "```python\nif a < b and c > d: pass\n```\n"
    )

    def _parse(self, body: str) -> ET.Element:
        return ET.fromstring(f"<root>{body}</root>")

    def test_output_is_well_formed_xhtml(self) -> None:
        body = confluence_sync.render_storage_body("implementation", self.TEMPLATE)
        self._parse(body)  # raises ET.ParseError if not well-formed

    def test_pipe_table_becomes_a_real_table(self) -> None:
        body = confluence_sync.render_storage_body("implementation", self.TEMPLATE)
        root = self._parse(body)
        tables = root.findall("table")
        self.assertEqual(len(tables), 1)
        headers = [th.text for th in tables[0].iter("th")]
        self.assertEqual(headers, ["File", "Change"])
        cells = ["".join(td.itertext()) for td in tables[0].iter("td")]
        self.assertEqual(cells, ["<path>", "<description>"])

    def test_placeholder_text_survives_as_literal_text(self) -> None:
        body = confluence_sync.render_storage_body("implementation", self.TEMPLATE)
        text = "".join(self._parse(body).itertext())
        for placeholder in ("<KEY>", "<ISO date>", "<path>", "<description>",
                            "<repo>", "<summary of whether changes are ready to merge, any caveats>"):
            self.assertIn(placeholder, text)

    def test_headings_after_a_placeholder_still_render(self) -> None:
        # `<summary ...>` used to make python-markdown treat the rest of the
        # document as one raw-HTML block, so every later heading/table came
        # out as literal text.
        body = confluence_sync.render_storage_body("implementation", self.TEMPLATE)
        root = self._parse(body)
        self.assertIn("Notes", [h.text for h in root.iter("h2")])

    def test_code_block_and_span_contents_are_not_double_escaped(self) -> None:
        body = confluence_sync.render_storage_body("implementation", self.TEMPLATE)
        root = self._parse(body)
        code_text = "".join("".join(c.itertext()) for c in root.iter("code"))
        self.assertIn("if a < b and c > d: pass", code_text)
        self.assertIn("$REPOS_DIR/<repo>/", code_text)
        self.assertNotIn("&lt;", code_text)

    def test_link_still_renders_as_an_anchor(self) -> None:
        body = confluence_sync.render_storage_body(
            "discovery", "See [the PR](https://example.com/x?a=1&b=2).")
        anchors = list(self._parse(body).iter("a"))
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0].get("href"), "https://example.com/x?a=1&b=2")


class TestChildTitle(unittest.TestCase):
    """Confluence page titles are unique per space, not per parent — bare
    "Discovery"/"Implementation Notes" child titles could only ever be
    claimed by one ticket in the whole space."""

    def test_title_is_namespaced_by_ticket_key(self) -> None:
        self.assertEqual(confluence_sync._child_title("PDE-1234", "discovery"), "PDE-1234 — Discovery")
        self.assertEqual(
            confluence_sync._child_title("PDE-1234", "implementation"), "PDE-1234 — Implementation Notes")

    def test_two_tickets_never_share_a_title_for_the_same_artifact(self) -> None:
        for artifact_type in confluence_sync.ARTIFACT_TYPES:
            first = confluence_sync._child_title("PDE-1234", artifact_type)
            second = confluence_sync._child_title("PDE-9999", artifact_type)
            self.assertNotEqual(first, second)
            self.assertIn("PDE-1234", first)
            self.assertIn("PDE-9999", second)

    def test_all_titles_across_two_tickets_are_distinct(self) -> None:
        titles = [
            confluence_sync._child_title(key, artifact_type)
            for key in ("PDE-1234", "PDE-9999")
            for artifact_type in confluence_sync.ARTIFACT_TYPES
        ]
        self.assertEqual(len(titles), len(set(titles)))


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
        # Verify the HTTP request details
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], f"{confluence_sync.CONFLUENCE_V2_BASE}/spaces")
        self.assertEqual(kwargs["params"], {"keys": "PDE"})
        self.assertEqual(kwargs["auth"], ("e", "t"))
        self.assertEqual(kwargs["timeout"], 20)

    @patch("confluence_sync.requests.get")
    def test_raises_when_space_not_found(self, mock_get) -> None:
        mock_get.return_value = _mock_response({"results": []})
        with self.assertRaises(confluence_sync.ConfluencePullError):
            confluence_sync._space_id(("e", "t"))
        # Verify the HTTP request was made to the correct endpoint
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], f"{confluence_sync.CONFLUENCE_V2_BASE}/spaces")
        self.assertEqual(kwargs["params"], {"keys": "PDE"})


class TestFindPage(unittest.TestCase):
    @patch("confluence_sync.requests.get")
    def test_returns_none_when_not_found(self, mock_get) -> None:
        mock_get.return_value = _mock_response({"results": []})
        self.assertIsNone(confluence_sync._find_page(("e", "t"), "PDE-1234", "5148311567"))
        # Verify the HTTP request details
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], f"{confluence_sync.CONFLUENCE_V1_BASE}/content/search")
        self.assertEqual(kwargs["params"]["cql"], 'title = "PDE-1234" and ancestor = 5148311567')
        self.assertEqual(kwargs["params"]["expand"], "version")
        self.assertEqual(kwargs["auth"], ("e", "t"))
        self.assertEqual(kwargs["timeout"], 20)

    @patch("confluence_sync.requests.get")
    def test_returns_id_and_version_when_found(self, mock_get) -> None:
        mock_get.return_value = _mock_response(
            {"results": [{"id": "42", "version": {"number": 3}}]}
        )
        result = confluence_sync._find_page(("e", "t"), "PDE-1234", "5148311567")
        self.assertEqual(result, {"id": "42", "version": 3})
        # Verify the HTTP request details
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], f"{confluence_sync.CONFLUENCE_V1_BASE}/content/search")
        self.assertEqual(kwargs["params"]["cql"], 'title = "PDE-1234" and ancestor = 5148311567')
        self.assertEqual(kwargs["params"]["expand"], "version")


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
        # Verify the HTTP request details
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], f"{confluence_sync.CONFLUENCE_V2_BASE}/pages")
        self.assertEqual(kwargs["json"]["title"], "PDE-1234")
        self.assertEqual(kwargs["json"]["parentId"], "5148311567")
        self.assertEqual(kwargs["json"]["spaceId"], "929781")
        self.assertEqual(kwargs["json"]["status"], "current")
        self.assertEqual(kwargs["json"]["body"]["representation"], "storage")
        # Verify the body value contains the ticket key and link to Jira
        body_value = kwargs["json"]["body"]["value"]
        self.assertIn("PDE-1234", body_value)
        self.assertIn("https://chghealthcare.atlassian.net/browse/PDE-1234", body_value)
        self.assertEqual(kwargs["auth"], ("e", "t"))
        self.assertEqual(kwargs["timeout"], 20)


class TestPush(unittest.TestCase):
    @patch("confluence_sync._space_id")
    @patch("confluence_sync.requests.post")
    @patch("confluence_sync._find_page")
    @patch("confluence_sync._get_or_create_parent")
    def test_creates_child_page_when_missing(self, mock_parent, mock_find, mock_post, mock_space_id) -> None:
        mock_parent.return_value = "100"
        mock_find.return_value = None  # no existing "Discovery" child
        mock_space_id.return_value = "929781"
        mock_post.return_value = _mock_response({"id": "300"})
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertEqual(url, "https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300")

        # Verify the HTTP request details
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], f"{confluence_sync.CONFLUENCE_V2_BASE}/pages")
        self.assertEqual(kwargs["json"]["spaceId"], "929781")
        self.assertEqual(kwargs["json"]["title"], "PDE-1234 — Discovery")
        self.assertEqual(kwargs["json"]["parentId"], "100")
        self.assertEqual(kwargs["json"]["status"], "current")
        self.assertEqual(kwargs["json"]["body"]["representation"], "storage")
        self.assertIn("Findings", kwargs["json"]["body"]["value"])
        self.assertEqual(kwargs["auth"], ("e", "t"))
        self.assertEqual(kwargs["timeout"], 20)

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    @patch("confluence_sync._get_or_create_parent")
    def test_updates_existing_child_page_with_fresh_version(self, mock_parent, mock_find, mock_put) -> None:
        mock_parent.return_value = "100"
        mock_find.return_value = {"id": "300", "version": 5}
        mock_put.return_value = _mock_response({"id": "300"})
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings v2", ("e", "t"))
        self.assertEqual(url, "https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300")

        # Verify the HTTP request details
        args, kwargs = mock_put.call_args
        self.assertEqual(args[0], f"{confluence_sync.CONFLUENCE_V2_BASE}/pages/300")
        self.assertEqual(kwargs["json"]["id"], "300")
        self.assertEqual(kwargs["json"]["title"], "PDE-1234 — Discovery")
        self.assertEqual(kwargs["json"]["status"], "current")
        self.assertEqual(kwargs["json"]["body"]["representation"], "storage")
        self.assertIn("Findings v2", kwargs["json"]["body"]["value"])
        self.assertEqual(kwargs["json"]["version"]["number"], 6)
        self.assertEqual(kwargs["auth"], ("e", "t"))
        self.assertEqual(kwargs["timeout"], 20)

    @patch("confluence_sync._get_or_create_parent")
    def test_returns_none_on_any_failure(self, mock_parent) -> None:
        mock_parent.side_effect = RuntimeError("network down")
        with self.assertLogs(confluence_sync.log, level="WARNING") as logged:
            url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertIsNone(url)
        # Swallowed is not the same as untraceable — a permanent
        # misconfiguration must not look like a blip.
        self.assertIn("network down", "\n".join(logged.output))
        self.assertIn("PDE-1234", "\n".join(logged.output))

    @patch("confluence_sync._space_id")
    @patch("confluence_sync.requests.post")
    @patch("confluence_sync._find_page")
    @patch("confluence_sync._get_or_create_parent")
    def test_two_tickets_push_non_colliding_child_titles(
        self, mock_parent, mock_find, mock_post, mock_space_id
    ) -> None:
        """Smoke test for the per-space title-uniqueness constraint: two
        different tickets pushing the same artifact type must request two
        different page titles, or the second one 400s forever."""
        mock_find.return_value = None
        mock_space_id.return_value = "929781"
        mock_post.return_value = _mock_response({"id": "300"})

        mock_parent.return_value = "100"
        confluence_sync.push("PDE-1234", "implementation", "# A", ("e", "t"))
        mock_parent.return_value = "200"
        confluence_sync.push("PDE-9999", "implementation", "# B", ("e", "t"))

        titles = [kwargs["json"]["title"] for _, kwargs in mock_post.call_args_list]
        self.assertEqual(titles, ["PDE-1234 — Implementation Notes", "PDE-9999 — Implementation Notes"])
        self.assertEqual(len(set(titles)), 2)
        # The lookup that decides create-vs-update is namespaced the same way.
        searched = [call.args[1] for call in mock_find.call_args_list]
        self.assertEqual(searched, ["PDE-1234 — Implementation Notes", "PDE-9999 — Implementation Notes"])


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

    @patch("confluence_sync._find_page")
    def test_raises_pull_error_when_requests_raises(self, mock_find) -> None:
        """Verify that a real API error (requests exception) raises ConfluencePullError,
        not silently returns None."""
        mock_find.side_effect = [{"id": "100", "version": 1}, {"id": "300", "version": 2}]
        with patch("confluence_sync.requests.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("network failure")
            with self.assertRaises(confluence_sync.ConfluencePullError):
                confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))

    @patch("confluence_sync._find_page")
    def test_does_not_confuse_not_found_with_error(self, mock_find) -> None:
        """Verify that parent not found returns None (not error),
        but an API error in getting pages raises ConfluencePullError.
        This test ensures the critical distinction is clear."""
        # First call: parent not found -> should return None, never raise
        mock_find.return_value = None
        result = confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))
        self.assertIsNone(result)

        # Reset mock for second scenario
        mock_find.reset_mock()
        # Second call: child not found -> should return None, never raise
        mock_find.side_effect = [{"id": "100", "version": 1}, None]
        result = confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))
        self.assertIsNone(result)


class TestClearAll(unittest.TestCase):
    @patch("confluence_sync._find_page")
    def test_noop_when_no_parent_page(self, mock_find) -> None:
        mock_find.return_value = None
        confluence_sync.clear_all("PDE-1234", ("e", "t"))  # must not raise

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    def test_clears_only_existing_children(self, mock_find, mock_put) -> None:
        # Parent found; the Discovery (version 4) and Merge Notes (version 10)
        # children exist, while Implementation Notes and Review Notes don't.
        def find_side_effect(auth, title, ancestor_id):
            if title == "PDE-1234":
                return {"id": "100", "version": 1}
            if title == "PDE-1234 — Discovery":
                return {"id": "300", "version": 4}
            if title == "PDE-1234 — Merge Notes":
                return {"id": "400", "version": 10}
            return None
        mock_find.side_effect = find_side_effect
        mock_put.return_value = _mock_response({"id": "300"})
        confluence_sync.clear_all("PDE-1234", ("e", "t"))
        # Exactly two PUT calls for the two existing children
        self.assertEqual(mock_put.call_count, 2)

        # Inspect both PUT calls via call_args_list
        calls = mock_put.call_args_list
        # Extract the version numbers and titles from each call
        versions_and_titles = []
        for call in calls:
            _, kwargs = call
            version_num = kwargs["json"]["version"]["number"]
            title = kwargs["json"]["title"]
            versions_and_titles.append((title, version_num))

        # Verify both pages were updated with correct version increments
        self.assertIn(("PDE-1234 — Discovery", 5), versions_and_titles)
        self.assertIn(("PDE-1234 — Merge Notes", 11), versions_and_titles)

        # Verify placeholder text in at least one call
        for call in calls:
            _, kwargs = call
            if "Cleared" in kwargs["json"]["body"]["value"]:
                self.assertIn("Cleared", kwargs["json"]["body"]["value"])
                break
        else:
            self.fail("No call contained the 'Cleared' placeholder")

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    def test_swallows_errors(self, mock_find, mock_put) -> None:
        mock_find.side_effect = RuntimeError("network down")
        with self.assertLogs(confluence_sync.log, level="WARNING") as logged:
            confluence_sync.clear_all("PDE-1234", ("e", "t"))  # must not raise
        self.assertIn("network down", "\n".join(logged.output))

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    def test_one_failing_page_does_not_stop_the_others(self, mock_find, mock_put) -> None:
        """Per-page resilience: a single page's failure (e.g. a version
        conflict) must be logged and skipped, not abandon the whole clear."""
        def find_side_effect(auth, title, ancestor_id):
            if title == "PDE-1234":
                return {"id": "100", "version": 1}
            if title == "PDE-1234 — Discovery":
                raise RuntimeError("discovery lookup exploded")
            return {"id": "400", "version": 2}
        mock_find.side_effect = find_side_effect
        mock_put.return_value = _mock_response({"id": "400"})

        with self.assertLogs(confluence_sync.log, level="WARNING") as logged:
            confluence_sync.clear_all("PDE-1234", ("e", "t"))

        # The three non-exploding children were still cleared.
        self.assertEqual(mock_put.call_count, 3)
        self.assertIn("discovery lookup exploded", "\n".join(logged.output))


class TestRenderingDependencyUnavailable(unittest.TestCase):
    """A missing `markdown`/`markdownify` must behave like any other sync
    failure, not kill worker.py at import time. push()/clear_all() are
    best-effort no-ops; pull() escalates, because a human's Confluence-only
    edit would otherwise be silently discarded."""

    def setUp(self) -> None:
        # _SELF_INSTALL_ATTEMPTED=True simulates "already tried and failed
        # this process" — without it, _ensure_rendering_available() would
        # attempt a real subprocess.run(pip install ...) here, making these
        # tests slow and non-hermetic.
        patcher1 = patch.object(confluence_sync, "_RENDERING_AVAILABLE", False)
        patcher2 = patch.object(confluence_sync, "_SELF_INSTALL_ATTEMPTED", True)
        patcher1.start()
        patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

    @patch("confluence_sync.requests.post")
    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    def test_push_is_a_logged_no_op(self, mock_find, mock_put, mock_post) -> None:
        with self.assertLogs(confluence_sync.log, level="WARNING") as logged:
            url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertIsNone(url)
        self.assertIn("PDE-1234", "\n".join(logged.output))
        mock_find.assert_not_called()
        mock_put.assert_not_called()
        mock_post.assert_not_called()

    @patch("confluence_sync.requests.put")
    @patch("confluence_sync._find_page")
    def test_clear_all_is_a_logged_no_op(self, mock_find, mock_put) -> None:
        with self.assertLogs(confluence_sync.log, level="WARNING") as logged:
            confluence_sync.clear_all("PDE-1234", ("e", "t"))
        self.assertIn("PDE-1234", "\n".join(logged.output))
        mock_find.assert_not_called()
        mock_put.assert_not_called()

    @patch("confluence_sync._find_page")
    def test_pull_raises_rather_than_returning_none(self, mock_find) -> None:
        with self.assertRaises(confluence_sync.ConfluencePullError):
            confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))
        mock_find.assert_not_called()


class TestSyncEnabled(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("CONFLUENCE_SYNC_ENABLED", "CLAUDE_PLUGIN_OPTION_CONFLUENCE_SYNC_ENABLED")
        }

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_defaults_to_enabled(self) -> None:
        self.assertTrue(confluence_sync._sync_enabled())

    def test_explicit_false_disables(self) -> None:
        os.environ["CONFLUENCE_SYNC_ENABLED"] = "false"
        self.assertFalse(confluence_sync._sync_enabled())

    def test_explicit_true_stays_enabled(self) -> None:
        os.environ["CONFLUENCE_SYNC_ENABLED"] = "true"
        self.assertTrue(confluence_sync._sync_enabled())

    def test_plugin_option_fallback_disables(self) -> None:
        os.environ["CLAUDE_PLUGIN_OPTION_CONFLUENCE_SYNC_ENABLED"] = "false"
        self.assertFalse(confluence_sync._sync_enabled())

    def test_explicit_env_var_takes_precedence_over_plugin_option(self) -> None:
        os.environ["CONFLUENCE_SYNC_ENABLED"] = "true"
        os.environ["CLAUDE_PLUGIN_OPTION_CONFLUENCE_SYNC_ENABLED"] = "false"
        self.assertTrue(confluence_sync._sync_enabled())


class TestSyncDisabled(unittest.TestCase):
    """When a human explicitly turns Confluence sync off, push/pull/clear_all
    must be silent, unconditional no-ops — no self-install attempt, no
    warnings, no pull escalation. There's no human edit to protect if the
    feature was never turned on."""

    def setUp(self) -> None:
        patcher = patch.object(confluence_sync, "_sync_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("confluence_sync._ensure_rendering_available")
    @patch("confluence_sync._find_page")
    def test_push_is_a_silent_no_op(self, mock_find, mock_ensure) -> None:
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertIsNone(url)
        mock_find.assert_not_called()
        mock_ensure.assert_not_called()

    @patch("confluence_sync._ensure_rendering_available")
    @patch("confluence_sync._find_page")
    def test_pull_returns_none_instead_of_raising(self, mock_find, mock_ensure) -> None:
        result = confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))
        self.assertIsNone(result)
        mock_find.assert_not_called()
        mock_ensure.assert_not_called()

    @patch("confluence_sync._ensure_rendering_available")
    @patch("confluence_sync._find_page")
    def test_clear_all_is_a_silent_no_op(self, mock_find, mock_ensure) -> None:
        confluence_sync.clear_all("PDE-1234", ("e", "t"))
        mock_find.assert_not_called()
        mock_ensure.assert_not_called()


class TestBootstrapPip(unittest.TestCase):
    """get-pip.py self-heal for a Python with no pip module at all."""

    @patch("confluence_sync.subprocess.run")
    def test_successful_bootstrap(self, mock_run) -> None:
        curl_result = MagicMock(returncode=0, stdout=b"print('fake get-pip.py')")
        install_result = MagicMock(returncode=0, stderr=b"")
        mock_run.side_effect = [curl_result, install_result]

        self.assertTrue(confluence_sync._bootstrap_pip())
        self.assertEqual(mock_run.call_count, 2)
        curl_args = mock_run.call_args_list[0][0][0]
        self.assertEqual(curl_args[0], "curl")
        self.assertIn("https://bootstrap.pypa.io/get-pip.py", curl_args)
        getpip_args = mock_run.call_args_list[1][0][0]
        self.assertEqual(getpip_args[0], sys.executable)
        self.assertIn("--user", getpip_args)

    @patch("confluence_sync.subprocess.run")
    def test_curl_failure_returns_false(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        self.assertFalse(confluence_sync._bootstrap_pip())
        mock_run.assert_called_once()  # never got to running get-pip.py

    @patch("confluence_sync.subprocess.run")
    def test_get_pip_execution_failure_returns_false(self, mock_run) -> None:
        curl_result = MagicMock(returncode=0, stdout=b"print('fake get-pip.py')")
        install_result = MagicMock(returncode=1, stderr=b"permission denied")
        mock_run.side_effect = [curl_result, install_result]

        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._bootstrap_pip()
        self.assertFalse(result)

    @patch("confluence_sync.subprocess.run")
    def test_subprocess_exception_is_swallowed(self, mock_run) -> None:
        mock_run.side_effect = FileNotFoundError("curl not found")
        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._bootstrap_pip()
        self.assertFalse(result)


class TestEnsureRenderingAvailable(unittest.TestCase):
    """_ensure_rendering_available()'s self-install attempt: memoized (never
    a second subprocess call within the same process), best-effort (never
    raises), and correctly updates module state on success/partial success."""

    def setUp(self) -> None:
        self._saved = (
            confluence_sync._markdown_lib,
            confluence_sync._markdownify_lib,
            confluence_sync._RENDERING_AVAILABLE,
            confluence_sync._MISSING_RENDERING_DEPS,
            confluence_sync._SELF_INSTALL_ATTEMPTED,
        )

    def tearDown(self) -> None:
        (
            confluence_sync._markdown_lib,
            confluence_sync._markdownify_lib,
            confluence_sync._RENDERING_AVAILABLE,
            confluence_sync._MISSING_RENDERING_DEPS,
            confluence_sync._SELF_INSTALL_ATTEMPTED,
        ) = self._saved

    def test_does_not_install_while_another_process_holds_the_lock(self) -> None:
        """Simulates a concurrent ticket-worker process already installing:
        holds a real exclusive flock on the same lock file this process
        opens fresh (matching what a separate OS process would do), then
        confirms this call backs off instead of racing pip against it."""
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        import fcntl
        holder = open(confluence_sync._INSTALL_LOCK_PATH, "w")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("confluence_sync.subprocess.run") as mock_run:
                result = confluence_sync._ensure_rendering_available()
            self.assertFalse(result)
            mock_run.assert_not_called()
            # Not permanently given up — a later call (once the other
            # process releases the lock) should still be free to retry.
            self.assertFalse(confluence_sync._SELF_INSTALL_ATTEMPTED)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_returns_true_immediately_when_already_available(self) -> None:
        confluence_sync._RENDERING_AVAILABLE = True
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        with patch("confluence_sync.subprocess.run") as mock_run:
            self.assertTrue(confluence_sync._ensure_rendering_available())
        mock_run.assert_not_called()

    def test_does_not_retry_after_a_failed_attempt_in_the_same_process(self) -> None:
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = True
        with patch("confluence_sync.subprocess.run") as mock_run:
            self.assertFalse(confluence_sync._ensure_rendering_available())
        mock_run.assert_not_called()

    @patch("confluence_sync.importlib.import_module")
    @patch("confluence_sync.subprocess.run")
    def test_successful_self_install_makes_rendering_available(self, mock_run, mock_import) -> None:
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        confluence_sync._MISSING_RENDERING_DEPS = ["markdown", "markdownify"]
        confluence_sync._markdown_lib = None
        confluence_sync._markdownify_lib = None
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_import.side_effect = [MagicMock(), MagicMock()]

        self.assertTrue(confluence_sync._ensure_rendering_available())
        self.assertTrue(confluence_sync._RENDERING_AVAILABLE)
        self.assertEqual(confluence_sync._MISSING_RENDERING_DEPS, [])
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        self.assertIn("markdown", call_args)
        self.assertIn("markdownify", call_args)
        self.assertIn("--user", call_args)
        self.assertEqual(call_args[0], sys.executable)

    @patch("confluence_sync.subprocess.run")
    def test_failed_self_install_leaves_rendering_unavailable(self, mock_run) -> None:
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        confluence_sync._MISSING_RENDERING_DEPS = ["markdown", "markdownify"]
        mock_run.return_value = MagicMock(returncode=1, stderr="no network")

        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._ensure_rendering_available()
        self.assertFalse(result)
        self.assertFalse(confluence_sync._RENDERING_AVAILABLE)

    @patch("confluence_sync._bootstrap_pip")
    @patch("confluence_sync.subprocess.run")
    def test_missing_pip_bootstraps_then_retries_successfully(self, mock_run, mock_bootstrap) -> None:
        """The real production failure mode this was built for: pip itself
        isn't installed (not just the packages). One bootstrap attempt,
        then one retry of the original install command.

        Deliberately does NOT also mock importlib.import_module: doing so
        together with mocking _bootstrap_pip (a dotted target inside this
        same module) breaks unittest.mock's own target resolution, which
        itself calls importlib.import_module to resolve dotted patch
        targets — verified directly, this is a real mock quirk, not a bug
        in the code under test. Letting the real `markdown`/`markdownify`
        import here is safe: every other test in this file already
        requires those packages to be genuinely installed to run at all."""
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        confluence_sync._MISSING_RENDERING_DEPS = ["markdown", "markdownify"]
        confluence_sync._markdown_lib = None
        confluence_sync._markdownify_lib = None
        first_attempt = MagicMock(returncode=1, stderr="/usr/bin/python3: No module named pip")
        retry_attempt = MagicMock(returncode=0, stderr="")
        mock_run.side_effect = [first_attempt, retry_attempt]
        mock_bootstrap.return_value = True

        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._ensure_rendering_available()
        self.assertTrue(result)
        self.assertTrue(confluence_sync._RENDERING_AVAILABLE)
        self.assertEqual(confluence_sync._MISSING_RENDERING_DEPS, [])
        mock_bootstrap.assert_called_once()
        self.assertEqual(mock_run.call_count, 2)

    @patch("confluence_sync._bootstrap_pip")
    @patch("confluence_sync.subprocess.run")
    def test_missing_pip_bootstrap_failure_does_not_retry_forever(self, mock_run, mock_bootstrap) -> None:
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        confluence_sync._MISSING_RENDERING_DEPS = ["markdown", "markdownify"]
        mock_run.return_value = MagicMock(returncode=1, stderr="/usr/bin/python3: No module named pip")
        mock_bootstrap.return_value = False

        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._ensure_rendering_available()
        self.assertFalse(result)
        mock_bootstrap.assert_called_once()
        # Bootstrap failed, so the install command is never retried — only
        # the one initial attempt.
        mock_run.assert_called_once()

    @patch("confluence_sync._bootstrap_pip")
    @patch("confluence_sync.subprocess.run")
    def test_non_pip_missing_failure_never_attempts_bootstrap(self, mock_run, mock_bootstrap) -> None:
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        confluence_sync._MISSING_RENDERING_DEPS = ["markdown", "markdownify"]
        mock_run.return_value = MagicMock(returncode=1, stderr="connection timed out")

        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._ensure_rendering_available()
        self.assertFalse(result)
        mock_bootstrap.assert_not_called()

    @patch("confluence_sync.subprocess.run")
    def test_subprocess_exception_is_swallowed_not_raised(self, mock_run) -> None:
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        confluence_sync._MISSING_RENDERING_DEPS = ["markdown", "markdownify"]
        mock_run.side_effect = TimeoutError("pip timed out")

        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._ensure_rendering_available()
        self.assertFalse(result)

    @patch("confluence_sync.importlib.import_module")
    @patch("confluence_sync.subprocess.run")
    def test_partial_self_install_still_reports_unavailable(self, mock_run, mock_import) -> None:
        """pip exits 0 but one of the two packages still can't be imported
        (e.g. a version conflict) — must not be treated as a success."""
        confluence_sync._RENDERING_AVAILABLE = False
        confluence_sync._SELF_INSTALL_ATTEMPTED = False
        confluence_sync._MISSING_RENDERING_DEPS = ["markdown", "markdownify"]
        confluence_sync._markdown_lib = None
        confluence_sync._markdownify_lib = None
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        def import_side_effect(name):
            if name == "markdown":
                return MagicMock()
            raise ImportError("still missing")
        mock_import.side_effect = import_side_effect

        with self.assertLogs(confluence_sync.log, level="WARNING"):
            result = confluence_sync._ensure_rendering_available()
        self.assertFalse(result)
        self.assertEqual(confluence_sync._MISSING_RENDERING_DEPS, ["markdownify"])


if __name__ == "__main__":
    unittest.main()
