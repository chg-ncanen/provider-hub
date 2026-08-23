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
