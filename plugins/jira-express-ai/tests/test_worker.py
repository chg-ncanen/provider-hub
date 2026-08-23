import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "ticket-worker"
sys.path.insert(0, str(_SKILL_DIR))

import worker  # noqa: E402


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ticket_dir = Path(self._tmp.name)
        self.repos_dir = self.ticket_dir.parent / "repos"
        self.repos_dir.mkdir(exist_ok=True)
        self.auth = ("email@example.com", "token")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestLoadPluginEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN")
        }

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_noop_when_env_file_missing(self) -> None:
        worker._load_plugin_env(self.tmp_path)  # must not raise
        self.assertNotIn("ATLASSIAN_EMAIL", os.environ)

    def test_loads_keys_from_env_file(self) -> None:
        (self.tmp_path / ".env").write_text("ATLASSIAN_EMAIL=from-file@example.com\nATLASSIAN_API_TOKEN=file-token\n")
        worker._load_plugin_env(self.tmp_path)
        self.assertEqual(os.environ["ATLASSIAN_EMAIL"], "from-file@example.com")
        self.assertEqual(os.environ["ATLASSIAN_API_TOKEN"], "file-token")

    def test_does_not_override_already_set_env_var(self) -> None:
        os.environ["ATLASSIAN_EMAIL"] = "already-set@example.com"
        (self.tmp_path / ".env").write_text("ATLASSIAN_EMAIL=from-file@example.com\n")
        worker._load_plugin_env(self.tmp_path)
        self.assertEqual(os.environ["ATLASSIAN_EMAIL"], "already-set@example.com")


class TestArtifactParsing(unittest.TestCase):
    def test_extract_status_found(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("# X\n\n**Status:** BLOCKED\n**Date:** 2026-01-01\n")
            path = Path(f.name)
        self.assertEqual(worker.extract_status(path), "BLOCKED")
        path.unlink()

    def test_extract_status_missing_defaults_to_unknown(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("# X\n\nnothing relevant here\n")
            path = Path(f.name)
        self.assertEqual(worker.extract_status(path), "UNKNOWN")
        path.unlink()

    def test_extract_bold_field(self) -> None:
        content = "**PR:** #42\n**PR URL:** https://x/42\n**Repo:** my-repo\n"
        self.assertEqual(worker.extract_bold_field(content, "PR URL"), "https://x/42")
        self.assertEqual(worker.extract_bold_field(content, "Repo"), "my-repo")
        self.assertEqual(worker.extract_bold_field(content, "Branch"), "")

    def test_extract_section(self) -> None:
        content = (
            "## Blocker\n\nNeed clarification on scope.\n\n"
            "## Suggested Next Step\n\nAsk the PM.\n"
        )
        self.assertEqual(worker.extract_section(content, "Blocker"), "Need clarification on scope.")
        self.assertEqual(worker.extract_section(content, "Suggested Next Step"), "Ask the PM.")
        self.assertEqual(worker.extract_section(content, "Reason"), "")

    def test_extract_section_last_heading_reads_to_end_of_file(self) -> None:
        content = "## Blocker\n\nMerge conflicts on main.\n"
        self.assertEqual(worker.extract_section(content, "Blocker"), "Merge conflicts on main.")

    def test_adf_text_flattens_paragraphs(self) -> None:
        adf = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "🤖 ⚠️ hello"}]}
        ]}
        self.assertEqual(worker._adf_text(adf), "🤖 ⚠️ hello")

    def test_adf_text_empty_body(self) -> None:
        self.assertEqual(worker._adf_text({}), "")


class TestJiraHttpCalls(unittest.TestCase):
    """These mock at the `requests` layer, not the wrapper functions
    themselves — everywhere else in this suite, jira_transition/jira_comment/
    read_status/get_recent_comments_text are mocked away entirely, so their
    actual request-building bodies (the TRANSITION_IDS lookup, the ADF JSON
    shape, the orderBy param) are otherwise never executed by any test."""

    def setUp(self) -> None:
        self.auth = ("email@example.com", "token")

    def test_read_status_parses_status_name_from_response(self) -> None:
        response = MagicMock()
        response.json.return_value = {"fields": {"status": {"name": "In Progress"}}}
        with patch.object(worker.requests, "get", return_value=response) as mock_get:
            status = worker.read_status("PDE-1", self.auth)
        self.assertEqual(status, "In Progress")
        url = mock_get.call_args.args[0]
        self.assertIn("/issue/PDE-1", url)
        self.assertEqual(mock_get.call_args.kwargs["params"], {"fields": "status"})
        response.raise_for_status.assert_called_once()

    def test_read_status_propagates_http_errors(self) -> None:
        response = MagicMock()
        response.raise_for_status.side_effect = worker.requests.HTTPError("500")
        with patch.object(worker.requests, "get", return_value=response):
            with self.assertRaises(worker.requests.HTTPError):
                worker.read_status("PDE-1", self.auth)

    def test_jira_transition_sends_looked_up_transition_id(self) -> None:
        response = MagicMock()
        with patch.object(worker.requests, "post", return_value=response) as mock_post:
            worker.jira_transition("PDE-1", "In Discovery", self.auth)
        url = mock_post.call_args.args[0]
        self.assertIn("/issue/PDE-1/transitions", url)
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {"transition": {"id": worker.TRANSITION_IDS["In Discovery"]}},
        )
        response.raise_for_status.assert_called_once()

    def test_jira_transition_includes_fields_when_given(self) -> None:
        response = MagicMock()
        with patch.object(worker.requests, "post", return_value=response) as mock_post:
            worker.jira_transition("PDE-1", "Blocked", self.auth, fields={"customfield_16637": "needs scope"})
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {
                "transition": {"id": worker.TRANSITION_IDS["Blocked"]},
                "fields": {"customfield_16637": "needs scope"},
            },
        )

    def test_blocked_reason_field_passes_short_text_through(self) -> None:
        self.assertEqual(
            worker._blocked_reason_field("needs scope"),
            {"customfield_16637": "needs scope"},
        )

    def test_blocked_reason_field_strips_whitespace(self) -> None:
        self.assertEqual(
            worker._blocked_reason_field("  needs scope  \n"),
            {"customfield_16637": "needs scope"},
        )

    def test_blocked_reason_field_truncates_over_255_chars(self) -> None:
        reason = "x" * 300
        result = worker._blocked_reason_field(reason)["customfield_16637"]
        self.assertEqual(len(result), 255)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(result[:-1], "x" * 254)

    def test_blocked_reason_field_exactly_255_chars_is_unchanged(self) -> None:
        reason = "x" * 255
        result = worker._blocked_reason_field(reason)["customfield_16637"]
        self.assertEqual(result, reason)

    def test_jira_transition_unknown_status_raises_key_error(self) -> None:
        # A typo'd status name must fail loudly, not silently transition to
        # the wrong (or no) state.
        with self.assertRaises(KeyError):
            worker.jira_transition("PDE-1", "Not A Real Status", self.auth)

    def test_jira_comment_builds_adf_body_with_given_text(self) -> None:
        response = MagicMock()
        with patch.object(worker.requests, "post", return_value=response) as mock_post:
            worker.jira_comment("PDE-1", "hello world", self.auth)
        url = mock_post.call_args.args[0]
        self.assertIn("/issue/PDE-1/comment", url)
        body = mock_post.call_args.kwargs["json"]["body"]
        self.assertEqual(body["content"][0]["content"][0]["text"], "hello world")
        response.raise_for_status.assert_called_once()

    def test_get_recent_comments_text_orders_by_most_recent_and_flattens_adf(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "comments": [
                {"body": {"type": "doc", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "🤖 ⚠️ newest failure"}]}
                ]}},
                {"body": {"type": "doc", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "🤖 older progress"}]}
                ]}},
            ]
        }
        with patch.object(worker.requests, "get", return_value=response) as mock_get:
            texts = worker.get_recent_comments_text("PDE-1", self.auth, limit=5)
        self.assertEqual(texts, ["🤖 ⚠️ newest failure", "🤖 older progress"])
        self.assertEqual(mock_get.call_args.kwargs["params"], {"orderBy": "-created", "maxResults": 5})

    def test_get_recent_comments_text_handles_no_comments(self) -> None:
        response = MagicMock()
        response.json.return_value = {}
        with patch.object(worker.requests, "get", return_value=response):
            self.assertEqual(worker.get_recent_comments_text("PDE-1", self.auth), [])


class TestCompletedStageCount(TempDirTestCase):
    def test_no_artifacts(self) -> None:
        self.assertEqual(worker.completed_stage_count(self.ticket_dir), 0)

    def test_discovery_only(self) -> None:
        (self.ticket_dir / "discovery.md").write_text("x")
        self.assertEqual(worker.completed_stage_count(self.ticket_dir), 1)

    def test_discovery_and_implementation(self) -> None:
        (self.ticket_dir / "discovery.md").write_text("x")
        (self.ticket_dir / "implementation-notes.md").write_text("x")
        self.assertEqual(worker.completed_stage_count(self.ticket_dir), 2)

    def test_implementation_without_discovery_does_not_count(self) -> None:
        # Stage order matters — a later artifact existing without the earlier
        # one present must not be read as "further along."
        (self.ticket_dir / "implementation-notes.md").write_text("x")
        self.assertEqual(worker.completed_stage_count(self.ticket_dir), 0)


class TestSanityCheckAndRewind(TempDirTestCase):
    def test_ungated_status_passes_through_untouched(self) -> None:
        with patch.object(worker, "jira_transition") as mock_transition:
            result = worker.sanity_check_and_rewind("PDE-1", "Done", self.ticket_dir, self.auth)
        self.assertEqual(result, "Done")
        mock_transition.assert_not_called()

    def test_in_discovery_requires_nothing(self) -> None:
        with patch.object(worker, "jira_transition") as mock_transition:
            result = worker.sanity_check_and_rewind("PDE-1", "In Discovery", self.ticket_dir, self.auth)
        self.assertEqual(result, "In Discovery")
        mock_transition.assert_not_called()

    def test_qa_review_with_discovery_done_is_not_rewound(self) -> None:
        (self.ticket_dir / "discovery.md").write_text("x")
        with patch.object(worker, "jira_transition") as mock_transition:
            result = worker.sanity_check_and_rewind("PDE-1", "QA Review", self.ticket_dir, self.auth)
        self.assertEqual(result, "QA Review")
        mock_transition.assert_not_called()

    def test_in_progress_without_discovery_rewinds_to_in_discovery(self) -> None:
        with patch.object(worker, "jira_transition") as mock_transition:
            result = worker.sanity_check_and_rewind("PDE-1", "In Progress", self.ticket_dir, self.auth)
        self.assertEqual(result, "In Discovery")
        mock_transition.assert_called_once_with("PDE-1", "In Discovery", self.auth)

    def test_uat_review_without_implementation_rewinds_to_in_progress(self) -> None:
        (self.ticket_dir / "discovery.md").write_text("x")
        with patch.object(worker, "jira_transition") as mock_transition:
            result = worker.sanity_check_and_rewind("PDE-1", "UAT Review", self.ticket_dir, self.auth)
        self.assertEqual(result, "In Progress")
        mock_transition.assert_called_once_with("PDE-1", "In Progress", self.auth)

    def test_uat_review_without_even_discovery_rewinds_to_in_discovery(self) -> None:
        with patch.object(worker, "jira_transition") as mock_transition:
            result = worker.sanity_check_and_rewind("PDE-1", "UAT Review", self.ticket_dir, self.auth)
        self.assertEqual(result, "In Discovery")
        mock_transition.assert_called_once_with("PDE-1", "In Discovery", self.auth)

    def test_in_review_with_both_stages_done_is_not_rewound(self) -> None:
        (self.ticket_dir / "discovery.md").write_text("x")
        (self.ticket_dir / "implementation-notes.md").write_text("x")
        with patch.object(worker, "jira_transition") as mock_transition:
            result = worker.sanity_check_and_rewind("PDE-1", "In Review", self.ticket_dir, self.auth)
        self.assertEqual(result, "In Review")
        mock_transition.assert_not_called()


class TestReportFailure(unittest.TestCase):
    def _run(self, recent_comments, stage="ticket-discovery"):
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "get_recent_comments_text", return_value=recent_comments):
            with self.assertRaises(SystemExit):
                worker.report_failure("PDE-1", "something broke", ("e", "t"), stage=stage)
        return mock_comment, mock_transition

    def test_first_failure_does_not_escalate(self) -> None:
        mock_comment, mock_transition = self._run(["🤖 ⚠️ PDE-1: something broke — will retry automatically."])
        mock_transition.assert_not_called()
        mock_comment.assert_called_once()
        self.assertTrue(mock_comment.call_args.args[1].startswith("🤖 ⚠️"))

    def test_second_failure_does_not_escalate(self) -> None:
        _, mock_transition = self._run([
            "🤖 ⚠️ PDE-1: something broke — will retry automatically.",
            "🤖 ⚠️ PDE-1: earlier failure — will retry automatically.",
        ])
        mock_transition.assert_not_called()

    def test_third_consecutive_failure_escalates_to_blocked(self) -> None:
        mock_comment, mock_transition = self._run([
            "🤖 ⚠️ PDE-1: something broke — will retry automatically.",
            "🤖 ⚠️ PDE-1: second failure — will retry automatically.",
            "🤖 ⚠️ PDE-1: first failure — will retry automatically.",
        ], stage="ticket-implementation")
        mock_transition.assert_called_once_with(
            "PDE-1", "Blocked", ("e", "t"), fields=worker._blocked_reason_field("something broke")
        )
        self.assertEqual(mock_comment.call_count, 2)
        escalation_message = mock_comment.call_args.args[1]
        self.assertIn("move back to In Progress", escalation_message)

    def test_stops_counting_at_first_non_failure_comment(self) -> None:
        _, mock_transition = self._run([
            "🤖 ⚠️ PDE-1: something broke — will retry automatically.",
            "🤖 ⚠️ PDE-1: second failure — will retry automatically.",
            "🤖 Discovery complete for PDE-1.",  # real progress in between — breaks the streak
            "🤖 ⚠️ PDE-1: an older, unrelated failure — will retry automatically.",
            "🤖 ⚠️ PDE-1: another older failure — will retry automatically.",
        ])
        mock_transition.assert_not_called()

    def test_still_exits_cleanly_when_posting_the_comment_itself_fails(self) -> None:
        # If Jira is what's unreachable, report_failure must not let that
        # unrelated exception propagate and obscure the original failure —
        # it should still exit non-zero so the next orchestrator run retries.
        with patch.object(worker, "jira_comment", side_effect=RuntimeError("jira is down")), \
             patch.object(worker, "get_recent_comments_text") as mock_recent:
            with self.assertLogs(worker.log, level="ERROR"):
                with self.assertRaises(SystemExit):
                    worker.report_failure("PDE-1", "something broke", ("e", "t"), stage="ticket-discovery")
        mock_recent.assert_not_called()  # never got past the failed comment post

    def test_still_exits_cleanly_when_fetching_recent_comments_fails(self) -> None:
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker, "get_recent_comments_text", side_effect=RuntimeError("jira is down")), \
             patch.object(worker, "jira_transition") as mock_transition:
            with self.assertLogs(worker.log, level="ERROR"):
                with self.assertRaises(SystemExit):
                    worker.report_failure("PDE-1", "something broke", ("e", "t"), stage="ticket-discovery")
        mock_comment.assert_called_once()  # the failure comment itself did get posted
        mock_transition.assert_not_called()  # never got far enough to decide on escalation


class TestApplyBlockedRouting(TempDirTestCase):
    def test_transitions_and_comments_with_blocker_details(self) -> None:
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text(
            "**Status:** BLOCKED\n\n## Blocker\n\nNeed clarification on scope.\n\n"
            "## Suggested Next Step\n\nAsk the PM.\n"
        )
        with patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment") as mock_comment:
            with self.assertRaises(SystemExit):
                worker.apply_blocked_routing("PDE-1", artifact, "ticket-discovery", self.auth)

        mock_transition.assert_called_once_with(
            "PDE-1", "Blocked", self.auth,
            fields=worker._blocked_reason_field("Need clarification on scope."),
        )
        message = mock_comment.call_args.args[1]
        self.assertIn("Need clarification on scope.", message)
        self.assertIn("Ask the PM.", message)
        self.assertIn("ticket-discovery", message)
        self.assertIn("move back to In Discovery", message)


class TestWaitForSentinel(TempDirTestCase):
    def test_returns_true_when_sentinel_already_exists(self) -> None:
        sentinel = self.ticket_dir / ".discovery-agent-done"
        sentinel.touch()
        self.assertTrue(worker.wait_for_sentinel("ticket-discovery", sentinel, timeout=0))

    def test_returns_false_on_timeout(self) -> None:
        sentinel = self.ticket_dir / ".discovery-agent-done"
        self.assertFalse(worker.wait_for_sentinel("ticket-discovery", sentinel, timeout=0))


class TestLaunchSpecialist(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved_log_dir = os.environ.pop("AGENT_CHILD_LOG_DIR", None)

    def tearDown(self) -> None:
        if self._saved_log_dir is not None:
            os.environ["AGENT_CHILD_LOG_DIR"] = self._saved_log_dir
        super().tearDown()

    def test_builds_expected_command(self) -> None:
        with patch.object(worker.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 4242
            worker.launch_specialist("ticket-discovery", self.ticket_dir, self.repos_dir, self.auth)

        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertIn(f"--add-dir={self.ticket_dir}", cmd)
        self.assertIn(f"--add-dir={self.repos_dir}", cmd)
        self.assertIn("-p", cmd)
        self.assertIn(f"/jexpress:ticket-discovery Repos directory: {self.repos_dir}", cmd)
        self.assertEqual(kwargs["cwd"], str(self.ticket_dir))

    def test_logs_to_ticket_dir_by_default(self) -> None:
        with patch.object(worker.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 4242
            worker.launch_specialist("ticket-discovery", self.ticket_dir, self.repos_dir, self.auth)
        log_file = mock_popen.call_args.kwargs["stdout"]
        expected_name = f"{self.ticket_dir.name}-ticket-discovery.log"
        self.assertEqual(Path(log_file.name), self.ticket_dir / expected_name)

    def test_logs_to_agent_child_log_dir_when_set(self) -> None:
        # Nested inside self.ticket_dir (the managed tempdir itself) so
        # tearDown's cleanup() removes it — self.ticket_dir.parent is the
        # shared system /tmp, not something this test should write into.
        external_dir = self.ticket_dir / "collected-logs"
        os.environ["AGENT_CHILD_LOG_DIR"] = str(external_dir)
        with patch.object(worker.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 4242
            worker.launch_specialist("ticket-discovery", self.ticket_dir, self.repos_dir, self.auth)
        log_file = mock_popen.call_args.kwargs["stdout"]
        expected_name = f"{self.ticket_dir.name}-ticket-discovery.log"
        self.assertEqual(Path(log_file.name), external_dir / expected_name)
        self.assertTrue(external_dir.is_dir())  # created if missing

    def test_relaunch_reuses_the_same_name_and_appends(self) -> None:
        # No timestamp — a relaunch of the same skill for the same ticket
        # must reuse the same session name and accumulate into the same
        # external log file, exactly like the local default already does.
        external_dir = self.ticket_dir / "collected-logs"
        os.environ["AGENT_CHILD_LOG_DIR"] = str(external_dir)
        with patch.object(worker.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 1
            worker.launch_specialist("ticket-discovery", self.ticket_dir, self.repos_dir, self.auth)
            first_name = mock_popen.call_args.args[0][1]  # "--name=..."
            mock_popen.return_value.pid = 2
            worker.launch_specialist("ticket-discovery", self.ticket_dir, self.repos_dir, self.auth)
            second_name = mock_popen.call_args.args[0][1]
        self.assertEqual(first_name, second_name)

    def test_logging_setup_failure_reports_failure_instead_of_crashing(self) -> None:
        # A misconfigured AGENT_CHILD_LOG_DIR (here: a path that already
        # exists as a plain file, so mkdir(exist_ok=True) still raises) must
        # not crash this session with a bare traceback and no Jira trail.
        blocked_path = self.ticket_dir / "blocked-log-dir"
        blocked_path.write_text("I'm a file, not a directory")
        os.environ["AGENT_CHILD_LOG_DIR"] = str(blocked_path)

        with patch.object(worker, "report_failure") as mock_fail, \
             patch.object(worker.subprocess, "Popen") as mock_popen:
            worker.launch_specialist("ticket-discovery", self.ticket_dir, self.repos_dir, self.auth)

        mock_fail.assert_called_once()
        self.assertEqual(mock_fail.call_args.args[0], self.ticket_dir.name)
        self.assertIn("ticket-discovery", mock_fail.call_args.args[1])
        self.assertEqual(mock_fail.call_args.kwargs["stage"], "ticket-discovery")
        mock_popen.assert_not_called()  # never got far enough to actually launch anything


class TestNormalizeAgentChildLogDir(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop("AGENT_CHILD_LOG_DIR", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["AGENT_CHILD_LOG_DIR"] = self._saved
        else:
            os.environ.pop("AGENT_CHILD_LOG_DIR", None)

    def test_noop_when_unset(self) -> None:
        worker._normalize_agent_child_log_dir()
        self.assertNotIn("AGENT_CHILD_LOG_DIR", os.environ)

    def test_resolves_relative_path_to_absolute(self) -> None:
        os.environ["AGENT_CHILD_LOG_DIR"] = "some-relative-dir"
        worker._normalize_agent_child_log_dir()
        self.assertTrue(Path(os.environ["AGENT_CHILD_LOG_DIR"]).is_absolute())

    def test_idempotent_on_already_absolute_path(self) -> None:
        os.environ["AGENT_CHILD_LOG_DIR"] = "/tmp/already-absolute"
        worker._normalize_agent_child_log_dir()
        self.assertEqual(os.environ["AGENT_CHILD_LOG_DIR"], "/tmp/already-absolute")

    def test_expands_tilde(self) -> None:
        os.environ["AGENT_CHILD_LOG_DIR"] = "~/logs"
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = "/tmp/fake-home"
        try:
            worker._normalize_agent_child_log_dir()
            self.assertEqual(os.environ["AGENT_CHILD_LOG_DIR"], "/tmp/fake-home/logs")
        finally:
            if saved_home is not None:
                os.environ["HOME"] = saved_home
            else:
                os.environ.pop("HOME", None)


class TestBuildQaReviewComment(TempDirTestCase):
    def test_includes_confluence_link_when_push_succeeded(self) -> None:
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** READY\n\n## Summary\n\nFound the bug.\n")
        comment = worker.build_qa_review_comment(
            "PDE-1234", artifact, "https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300"
        )
        self.assertIn("Full details: https://chghealthcare.atlassian.net/wiki/spaces/PDE/pages/300", comment)
        self.assertNotIn("See discovery.md", comment)

    def test_falls_back_to_local_filename_when_no_confluence_url(self) -> None:
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** READY\n\n## Summary\n\nFound the bug.\n")
        comment = worker.build_qa_review_comment("PDE-1234", artifact, None)
        self.assertIn("See discovery.md for full findings.", comment)


class TestBuildInReviewComment(TempDirTestCase):
    def test_no_changes_needed_falls_back_without_url(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNothing to do.\n")
        review_context = self.ticket_dir / "review-context.md"
        comment = worker.build_in_review_comment("PDE-1234", notes, review_context, None)
        self.assertIn("See implementation-notes.md for full findings.", comment)

    def test_no_changes_needed_includes_confluence_link(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNothing to do.\n")
        review_context = self.ticket_dir / "review-context.md"
        comment = worker.build_in_review_comment("PDE-1234", notes, review_context, "https://example/x")
        self.assertIn("Full details: https://example/x", comment)

    def test_ready_for_review_includes_confluence_link(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** READY\n\n## PR Readiness\n\nGood to go.\n")
        review_context = self.ticket_dir / "review-context.md"
        review_context.write_text("**PR URL:** https://github.com/chghealthcare/repo/pull/1\n")
        comment = worker.build_in_review_comment("PDE-1234", notes, review_context, "https://example/x")
        self.assertIn("Full details: https://example/x", comment)

    def test_ready_for_review_falls_back_without_url(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** READY\n\n## PR Readiness\n\nGood to go.\n")
        review_context = self.ticket_dir / "review-context.md"
        review_context.write_text("**PR URL:** https://github.com/chghealthcare/repo/pull/1\n")
        comment = worker.build_in_review_comment("PDE-1234", notes, review_context, None)
        self.assertNotIn("Full details:", comment)
        self.assertIn("ready for review", comment)


class TestRunDiscovery(TempDirTestCase):
    def test_launches_when_sentinel_missing_and_transitions_on_success(self) -> None:
        artifact = self.ticket_dir / "discovery.md"

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            artifact.write_text("**Status:** OK\n**Date:** 2026-01-01\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch) as mock_launch, \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_discovery("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_launch.assert_called_once()
        mock_transition.assert_called_once_with("PDE-1", "QA Review", self.auth)
        mock_comment.assert_called_once()

    def test_always_relaunches_even_when_sentinel_and_artifact_already_exist(self) -> None:
        # A stale sentinel left over from a prior discovery pass (e.g. a
        # human rejected the ticket back from QA Review to In Discovery)
        # must not skip a redo — see run_discovery()'s comment for why.
        (self.ticket_dir / ".discovery-agent-done").touch()
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** OK\n(prior version)\n")

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            artifact.write_text("**Status:** OK\n(revised)\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch) as mock_launch, \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment"), \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_discovery("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_launch.assert_called_once()
        mock_transition.assert_called_once_with("PDE-1", "QA Review", self.auth)

    def test_does_not_delete_prior_artifact_before_relaunch(self) -> None:
        # Deliberately NOT deleted before relaunch (unlike review/merge's
        # artifacts) — the specialist reads its own prior discovery.md to
        # know this is a revision, not a first pass; see
        # ticket-discovery/SKILL.md.
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** OK\n(prior version)\n")
        seen_before_launch = {}

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            seen_before_launch["content"] = artifact.read_text() if artifact.exists() else None
            artifact.write_text("**Status:** OK\n(revised)\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition"), \
             patch.object(worker, "jira_comment"), \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_discovery("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        self.assertEqual(seen_before_launch["content"], "**Status:** OK\n(prior version)\n")

    def test_pushes_to_confluence_and_links_it_in_the_qa_review_comment(self) -> None:
        artifact = self.ticket_dir / "discovery.md"

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            artifact.write_text("**Status:** READY\n\n## Summary\n\nFound it.\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition"), \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value="https://example.atlassian.net/wiki/x") as mock_push:
            worker.run_discovery("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)

        mock_push.assert_called_once_with("PDE-1234", "discovery", artifact.read_text(), self.auth)
        self.assertIn("https://example.atlassian.net/wiki/x", mock_comment.call_args.args[1])

    def test_blocked_artifact_applies_blocked_routing(self) -> None:
        artifact = self.ticket_dir / "discovery.md"

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            artifact.write_text("**Status:** BLOCKED\n\n## Blocker\n\nneed input\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "apply_blocked_routing") as mock_blocked, \
             patch.object(worker, "jira_transition") as mock_transition:
            worker.run_discovery("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_blocked.assert_called_once()
        self.assertEqual(mock_blocked.call_args.args[2], "ticket-discovery")
        mock_transition.assert_not_called()

    def test_timeout_reports_failure(self) -> None:
        with patch.object(worker, "launch_specialist"), \
             patch.object(worker, "wait_for_sentinel", return_value=False), \
             patch.object(worker, "report_failure") as mock_fail, \
             patch.object(worker, "jira_transition") as mock_transition:
            worker.run_discovery("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_fail.assert_called_once()
        mock_transition.assert_not_called()

    def test_missing_artifact_after_done_reports_failure(self) -> None:
        with patch.object(worker, "launch_specialist"), \
             patch.object(worker, "wait_for_sentinel", return_value=True), \
             patch.object(worker, "report_failure") as mock_fail:
            worker.run_discovery("PDE-1", self.ticket_dir, self.repos_dir, self.auth)
        mock_fail.assert_called_once()


class TestRunImplementation(TempDirTestCase):
    """implementation-notes.md must NOT exist before the call in any of these
    cases — its existence is exactly what run_implementation() checks to pick
    the first-pass-vs-review-pass branch, so pre-creating it as fixture setup
    (rather than having the mocked specialist write it during the call) would
    silently route every one of these tests into the wrong branch."""

    def test_first_pass_success_transitions_to_in_review_with_pr_url(self) -> None:
        (self.ticket_dir / "review-context.md").write_text("**PR:** #7\n**PR URL:** https://x/7\n")

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            self.assertEqual(skill, "ticket-implementation")
            (ticket_dir / "implementation-notes.md").write_text("**Status:** OK\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_transition.assert_called_once_with("PDE-1", "In Review", self.auth)
        self.assertIn("https://x/7", mock_comment.call_args.args[1])

    def test_first_pass_pushes_to_confluence_and_links_it_in_the_comment(self) -> None:
        (self.ticket_dir / "review-context.md").write_text("**PR:** #7\n**PR URL:** https://x/7\n")
        notes = self.ticket_dir / "implementation-notes.md"

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            notes.write_text("**Status:** OK\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition"), \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value="https://example.atlassian.net/wiki/z") as mock_push:
            worker.run_implementation("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)

        mock_push.assert_called_once_with("PDE-1234", "implementation", notes.read_text(), self.auth)
        self.assertIn("https://example.atlassian.net/wiki/z", mock_comment.call_args.args[1])

    def test_first_pass_missing_review_context_reports_failure(self) -> None:
        # No review-context.md written — the specialist claims done but
        # didn't produce the artifact run_implementation actually needs.
        def fake_launch(skill, ticket_dir, repos_dir, auth):
            (ticket_dir / "implementation-notes.md").write_text("**Status:** OK\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "report_failure") as mock_fail, \
             patch.object(worker, "jira_transition") as mock_transition:
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_fail.assert_called_once()
        self.assertIn("review-context.md", mock_fail.call_args.args[1])
        mock_transition.assert_not_called()

    def test_first_pass_blocked(self) -> None:
        def fake_launch(skill, ticket_dir, repos_dir, auth):
            (ticket_dir / "implementation-notes.md").write_text(
                "**Status:** BLOCKED\n\n## Blocker\n\nno access\n"
            )

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "apply_blocked_routing") as mock_blocked:
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)
        mock_blocked.assert_called_once()
        self.assertEqual(mock_blocked.call_args.args[2], "ticket-implementation")

    def test_first_pass_no_changes_needed_skips_review_context_requirement(self) -> None:
        # No review-context.md written — deliberately, since NO_CHANGES_NEEDED
        # means no PR was ever opened. Must not be treated as the specialist
        # failing to produce a required artifact.
        def fake_launch(skill, ticket_dir, repos_dir, auth):
            (ticket_dir / "implementation-notes.md").write_text(
                "**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNo change needed.\n"
            )

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "report_failure") as mock_fail, \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_fail.assert_not_called()
        mock_transition.assert_called_once_with("PDE-1", "In Review", self.auth)
        self.assertIn("recommends closing", mock_comment.call_args.args[1])

    def test_first_pass_no_changes_needed_pushes_to_confluence_and_links_it(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            notes.write_text("**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNo change needed.\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition"), \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value="https://example.atlassian.net/wiki/nc") as mock_push:
            worker.run_implementation("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)

        mock_push.assert_called_once_with("PDE-1234", "implementation", notes.read_text(), self.auth)
        self.assertIn("https://example.atlassian.net/wiki/nc", mock_comment.call_args.args[1])

    def test_rejected_no_changes_needed_reruns_implementation_not_review(self) -> None:
        # implementation-notes.md already exists (a prior NO_CHANGES_NEEDED
        # pass) and a human moved the ticket back to In Progress to ask for
        # another look. There's no PR to review, so this must redo
        # implementation, not launch ticket-review.
        (self.ticket_dir / "implementation-notes.md").write_text(
            "**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNo change needed.\n"
        )

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            self.assertEqual(skill, "ticket-implementation")
            (ticket_dir / "implementation-notes.md").write_text("**Status:** OK\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch) as mock_launch, \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "report_failure") as mock_fail:
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_launch.assert_called_once()
        # Falls through to the "review-context.md missing" failure since the
        # fake relaunch above doesn't write one — proves this took the
        # first-pass branch (which requires it), not the review-pass branch
        # (which launches ticket-review instead).
        mock_fail.assert_called_once()
        self.assertIn("review-context.md", mock_fail.call_args.args[1])

    def test_first_pass_relaunches_even_with_stale_sentinel_from_a_broken_prior_attempt(self) -> None:
        # A prior attempt that reported done without producing
        # implementation-notes.md (a bug in that specialist, not something
        # expected in normal operation) must not make a retry skip
        # relaunching and just repeat the same failure forever.
        (self.ticket_dir / ".implementation-agent-done").touch()
        (self.ticket_dir / "review-context.md").write_text("**PR URL:** https://x/7\n")

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            (ticket_dir / "implementation-notes.md").write_text("**Status:** OK\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch) as mock_launch, \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment"), \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_launch.assert_called_once()
        mock_transition.assert_called_once_with("PDE-1", "In Review", self.auth)

    def test_review_pass_runs_when_implementation_notes_already_exist(self) -> None:
        (self.ticket_dir / "implementation-notes.md").write_text("**Status:** OK\n")
        (self.ticket_dir / "review-context.md").write_text("**PR:** #7\n**PR URL:** https://x/7\n")
        # Stale leftovers from a prior review pass — must be cleared before relaunching.
        (self.ticket_dir / "review-notes.md").write_text("stale")
        (self.ticket_dir / ".review-agent-done").touch()

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            self.assertEqual(skill, "ticket-review")
            (ticket_dir / "review-notes.md").write_text("**Status:** OK\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch) as mock_launch, \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_launch.assert_called_once()
        mock_transition.assert_called_once_with("PDE-1", "In Review", self.auth)
        self.assertIn("https://x/7", mock_comment.call_args.args[1])

    def test_review_pass_pushes_to_confluence_and_links_it_in_the_comment(self) -> None:
        (self.ticket_dir / "implementation-notes.md").write_text("**Status:** OK\n")
        review_context = self.ticket_dir / "review-context.md"
        review_context.write_text("**PR:** #7\n**PR URL:** https://x/7\n")
        notes = self.ticket_dir / "review-notes.md"

        def fake_launch(skill, ticket_dir, repos_dir, auth):
            self.assertEqual(skill, "ticket-review")
            notes.write_text("**Status:** RESOLVED\n\n## Comments Addressed\n\nNone.\n")

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch) as mock_launch, \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition"), \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value="https://example.atlassian.net/wiki/review") as mock_push:
            worker.run_implementation("PDE-1234", self.ticket_dir, self.repos_dir, self.auth)

        mock_launch.assert_called_once()
        mock_push.assert_called_once_with("PDE-1234", "review", notes.read_text(), self.auth)
        self.assertIn("https://example.atlassian.net/wiki/review", mock_comment.call_args.args[1])

    def test_first_pass_timeout_reports_failure(self) -> None:
        with patch.object(worker, "launch_specialist"), \
             patch.object(worker, "wait_for_sentinel", return_value=False), \
             patch.object(worker, "report_failure") as mock_fail, \
             patch.object(worker, "jira_transition") as mock_transition:
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_fail.assert_called_once()
        self.assertIn("ticket-implementation did not complete within 900s", mock_fail.call_args.args[1])
        mock_transition.assert_not_called()

    def test_review_pass_timeout_reports_failure(self) -> None:
        (self.ticket_dir / "implementation-notes.md").write_text("**Status:** OK\n")
        (self.ticket_dir / "review-context.md").write_text("**PR:** #7\n**PR URL:** https://x/7\n")

        with patch.object(worker, "launch_specialist"), \
             patch.object(worker, "wait_for_sentinel", return_value=False), \
             patch.object(worker, "report_failure") as mock_fail, \
             patch.object(worker, "jira_transition") as mock_transition:
            worker.run_implementation("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_fail.assert_called_once()
        self.assertIn("ticket-review did not complete within 900s", mock_fail.call_args.args[1])
        mock_transition.assert_not_called()


class TestRunMerge(TempDirTestCase):
    def _run(self, notes_content):
        def fake_launch(skill, ticket_dir, repos_dir, auth):
            (ticket_dir / "merge-notes.md").write_text(notes_content)

        def fake_wait(skill, sentinel_path, timeout=worker.SENTINEL_TIMEOUT):
            sentinel_path.touch()
            return True

        with patch.object(worker, "launch_specialist", side_effect=fake_launch), \
             patch.object(worker, "wait_for_sentinel", side_effect=fake_wait), \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker, "report_failure") as mock_fail:
            worker.run_merge("PDE-1", self.ticket_dir, self.repos_dir, self.auth)
        return mock_transition, mock_comment, mock_fail

    def test_success_transitions_to_done(self) -> None:
        mock_transition, mock_comment, mock_fail = self._run("**Status:** SUCCESS\n")
        mock_transition.assert_called_once_with("PDE-1", "Done", self.auth)
        mock_comment.assert_called_once()
        mock_fail.assert_not_called()

    def test_blocked_transitions_to_blocked_with_reason(self) -> None:
        mock_transition, mock_comment, mock_fail = self._run(
            "**Status:** BLOCKED\n\n## Blocker\n\nmerge conflicts\n"
        )
        mock_transition.assert_called_once_with(
            "PDE-1", "Blocked", self.auth, fields=worker._blocked_reason_field("merge conflicts")
        )
        self.assertIn("merge conflicts", mock_comment.call_args.args[1])
        self.assertIn("move back to UAT Review", mock_comment.call_args.args[1])
        mock_fail.assert_not_called()

    def test_pending_does_not_transition_or_comment(self) -> None:
        mock_transition, mock_comment, mock_fail = self._run(
            "**Status:** PENDING\n\n## Reason\n\nCI still running\n"
        )
        mock_transition.assert_not_called()
        mock_comment.assert_not_called()
        mock_fail.assert_not_called()

    def test_unrecognized_status_reports_failure(self) -> None:
        mock_transition, mock_comment, mock_fail = self._run("**Status:** WEIRD\n")
        mock_fail.assert_called_once()
        mock_transition.assert_not_called()

    def test_timeout_reports_failure(self) -> None:
        with patch.object(worker, "launch_specialist"), \
             patch.object(worker, "wait_for_sentinel", return_value=False), \
             patch.object(worker, "report_failure") as mock_fail, \
             patch.object(worker, "jira_transition") as mock_transition:
            worker.run_merge("PDE-1", self.ticket_dir, self.repos_dir, self.auth)

        mock_fail.assert_called_once()
        self.assertIn("ticket-merge did not complete within 900s", mock_fail.call_args.args[1])
        mock_transition.assert_not_called()

    def test_clears_stale_sentinel_and_notes_before_relaunch(self) -> None:
        (self.ticket_dir / "merge-notes.md").write_text("stale")
        (self.ticket_dir / ".merge-agent-done").touch()
        mock_transition, mock_comment, mock_fail = self._run("**Status:** SUCCESS\n")
        mock_transition.assert_called_once_with("PDE-1", "Done", self.auth)


class TestHumanGates(TempDirTestCase):
    def test_qa_review_gate_reposts_handoff_comment(self) -> None:
        (self.ticket_dir / "discovery.md").write_text(
            "**Status:** OK\n\n## Summary\n\nDiscovery complete, ready for review.\n"
        )
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_qa_review_gate("PDE-1", self.ticket_dir, self.auth)
        self.assertIn("Discovery complete", mock_comment.call_args.args[1])

    def test_qa_review_gate_resyncs_to_confluence_and_links_it(self) -> None:
        artifact = self.ticket_dir / "discovery.md"
        artifact.write_text("**Status:** OK\n\n## Summary\n\nDiscovery complete, ready for review.\n")
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value="https://example.atlassian.net/wiki/y") as mock_push:
            worker.run_qa_review_gate("PDE-1", self.ticket_dir, self.auth)
        mock_push.assert_called_once_with("PDE-1", "discovery", artifact.read_text(), self.auth)
        self.assertIn("https://example.atlassian.net/wiki/y", mock_comment.call_args.args[1])

    def test_in_review_gate_includes_pr_url_when_available(self) -> None:
        (self.ticket_dir / "implementation-notes.md").write_text("**Status:** READY\n")
        (self.ticket_dir / "review-context.md").write_text("**PR URL:** https://x/7\n")
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_in_review_gate("PDE-1", self.ticket_dir, self.auth)
        self.assertIn("https://x/7", mock_comment.call_args.args[1])

    def test_in_review_gate_without_review_context(self) -> None:
        (self.ticket_dir / "implementation-notes.md").write_text("**Status:** READY\n")
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_in_review_gate("PDE-1", self.ticket_dir, self.auth)
        self.assertIn("ready for review", mock_comment.call_args.args[1])

    def test_in_review_gate_no_changes_needed(self) -> None:
        (self.ticket_dir / "implementation-notes.md").write_text(
            "**Status:** NO_CHANGES_NEEDED\n\n## PR Readiness\n\nNo change needed.\n"
        )
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value=None):
            worker.run_in_review_gate("PDE-1", self.ticket_dir, self.auth)
        self.assertIn("recommends closing", mock_comment.call_args.args[1])

    def test_in_review_gate_resyncs_to_confluence_and_links_it(self) -> None:
        notes = self.ticket_dir / "implementation-notes.md"
        notes.write_text("**Status:** READY\n")
        (self.ticket_dir / "review-context.md").write_text("**PR URL:** https://x/7\n")
        with patch.object(worker, "jira_comment") as mock_comment, \
             patch.object(worker.confluence_sync, "push", return_value="https://example.atlassian.net/wiki/ir") as mock_push:
            worker.run_in_review_gate("PDE-1234", self.ticket_dir, self.auth)
        mock_push.assert_called_once_with("PDE-1234", "implementation", notes.read_text(), self.auth)
        self.assertIn("https://example.atlassian.net/wiki/ir", mock_comment.call_args.args[1])


class TestMainDispatch(TempDirTestCase):
    def _run_main(self, jira_status, rewound_status=None):
        # main() reassigns "To Do" to "In Discovery" itself before calling
        # sanity_check_and_rewind() — that's the status the mock actually
        # sees and must default to returning unchanged, not the raw
        # jira_status passed in for this case.
        effective_status = "In Discovery" if jira_status == "To Do" else jira_status
        with patch.object(sys, "argv", ["worker.py", str(self.repos_dir)]), \
             patch.object(worker.Path, "cwd", return_value=self.ticket_dir), \
             patch.object(worker, "_auth", return_value=self.auth), \
             patch.object(worker, "read_status", return_value=jira_status) as mock_read, \
             patch.object(worker, "jira_transition") as mock_transition, \
             patch.object(worker, "sanity_check_and_rewind", return_value=rewound_status or effective_status) as mock_rewind, \
             patch.object(worker, "run_discovery") as mock_discovery, \
             patch.object(worker, "run_qa_review_gate") as mock_qa, \
             patch.object(worker, "run_implementation") as mock_impl, \
             patch.object(worker, "run_in_review_gate") as mock_in_review, \
             patch.object(worker, "run_merge") as mock_merge:
            worker.main()
        return {
            "read_status": mock_read,
            "transition": mock_transition,
            "rewind": mock_rewind,
            "discovery": mock_discovery,
            "qa": mock_qa,
            "impl": mock_impl,
            "in_review": mock_in_review,
            "merge": mock_merge,
        }

    def test_to_do_transitions_then_routes_to_discovery(self) -> None:
        mocks = self._run_main("To Do")
        mocks["transition"].assert_called_once_with(self.ticket_dir.name, "In Discovery", self.auth)
        mocks["rewind"].assert_called_once_with(self.ticket_dir.name, "In Discovery", self.ticket_dir, self.auth)
        mocks["discovery"].assert_called_once()

    def test_in_discovery_routes_to_discovery(self) -> None:
        mocks = self._run_main("In Discovery")
        mocks["discovery"].assert_called_once()
        mocks["transition"].assert_not_called()

    def test_qa_review_routes_to_gate(self) -> None:
        mocks = self._run_main("QA Review")
        mocks["qa"].assert_called_once()

    def test_in_progress_routes_to_implementation(self) -> None:
        mocks = self._run_main("In Progress")
        mocks["impl"].assert_called_once()

    def test_in_review_routes_to_gate(self) -> None:
        mocks = self._run_main("In Review")
        mocks["in_review"].assert_called_once()

    def test_uat_review_routes_to_merge(self) -> None:
        mocks = self._run_main("UAT Review")
        mocks["merge"].assert_called_once()

    def test_rewind_result_is_what_gets_routed(self) -> None:
        # Jira says UAT Review, but sanity_check_and_rewind decides In Discovery
        # — main() must route on the corrected status, not the original one.
        mocks = self._run_main("UAT Review", rewound_status="In Discovery")
        mocks["discovery"].assert_called_once()
        mocks["merge"].assert_not_called()

    def test_terminal_statuses_do_nothing(self) -> None:
        for status in ("Done", "Backlog", "Cancelled", "Released"):
            mocks = self._run_main(status)
            for key in ("discovery", "qa", "impl", "in_review", "merge"):
                mocks[key].assert_not_called()

    def test_unrecognized_status_does_nothing(self) -> None:
        mocks = self._run_main("Some New Status Nobody Has Seen")
        for key in ("discovery", "qa", "impl", "in_review", "merge"):
            mocks[key].assert_not_called()


if __name__ == "__main__":
    unittest.main()
