import os
import sys
import unittest
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
        self.assertEqual(kwargs["json"]["title"], "Discovery")
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
        self.assertEqual(kwargs["json"]["title"], "Discovery")
        self.assertEqual(kwargs["json"]["status"], "current")
        self.assertEqual(kwargs["json"]["body"]["representation"], "storage")
        self.assertIn("Findings v2", kwargs["json"]["body"]["value"])
        self.assertEqual(kwargs["json"]["version"]["number"], 6)
        self.assertEqual(kwargs["auth"], ("e", "t"))
        self.assertEqual(kwargs["timeout"], 20)

    @patch("confluence_sync._get_or_create_parent")
    def test_returns_none_on_any_failure(self, mock_parent) -> None:
        mock_parent.side_effect = RuntimeError("network down")
        url = confluence_sync.push("PDE-1234", "discovery", "# Findings", ("e", "t"))
        self.assertIsNone(url)


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


if __name__ == "__main__":
    unittest.main()
