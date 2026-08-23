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
    must be silent, unconditional no-ops — no warnings, no pull escalation.
    There's no human edit to protect if the feature was never turned on."""

    def setUp(self) -> None:
        patcher = patch.object(confluence_sync, "_sync_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("confluence_sync._find_page")
    def test_push_is_a_silent_no_op(self, mock_find) -> None:
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertIsNone(url)
        mock_find.assert_not_called()

    @patch("confluence_sync._find_page")
    def test_pull_returns_none_instead_of_raising(self, mock_find) -> None:
        result = confluence_sync.pull("PDE-1234", "discovery", ("e", "t"))
        self.assertIsNone(result)
        mock_find.assert_not_called()

    @patch("confluence_sync._find_page")
    def test_clear_all_is_a_silent_no_op(self, mock_find) -> None:
        confluence_sync.clear_all("PDE-1234", ("e", "t"))
        mock_find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
