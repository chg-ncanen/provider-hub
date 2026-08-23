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


if __name__ == "__main__":
    unittest.main()
