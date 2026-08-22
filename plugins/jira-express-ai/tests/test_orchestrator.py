import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "ticket-orchestrator"
sys.path.insert(0, str(_SKILL_DIR))

import orchestrator  # noqa: E402


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestAuth(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.pop(k, None)
            for k in (
                "ATLASSIAN_EMAIL",
                "ATLASSIAN_API_TOKEN",
                "CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL",
                "CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN",
            )
        }

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_prefers_bare_env_vars(self) -> None:
        os.environ["ATLASSIAN_EMAIL"] = "bare@example.com"
        os.environ["ATLASSIAN_API_TOKEN"] = "bare-token"
        os.environ["CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL"] = "plugin@example.com"
        os.environ["CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN"] = "plugin-token"
        self.assertEqual(orchestrator._auth(), ("bare@example.com", "bare-token"))

    def test_falls_back_to_plugin_option_env_vars(self) -> None:
        os.environ["CLAUDE_PLUGIN_OPTION_ATLASSIAN_EMAIL"] = "plugin@example.com"
        os.environ["CLAUDE_PLUGIN_OPTION_ATLASSIAN_API_TOKEN"] = "plugin-token"
        self.assertEqual(orchestrator._auth(), ("plugin@example.com", "plugin-token"))

    def test_missing_credentials_exits(self) -> None:
        with self.assertRaises(SystemExit):
            orchestrator._auth()


class TestLoadPluginEnv(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
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
        super().tearDown()

    def test_noop_when_env_file_missing(self) -> None:
        orchestrator._load_plugin_env(self.tmp_path)  # must not raise
        self.assertNotIn("ATLASSIAN_EMAIL", os.environ)

    def test_loads_keys_from_env_file(self) -> None:
        (self.tmp_path / ".env").write_text("ATLASSIAN_EMAIL=from-file@example.com\nATLASSIAN_API_TOKEN=file-token\n")
        orchestrator._load_plugin_env(self.tmp_path)
        self.assertEqual(os.environ["ATLASSIAN_EMAIL"], "from-file@example.com")
        self.assertEqual(os.environ["ATLASSIAN_API_TOKEN"], "file-token")

    def test_does_not_override_already_set_env_var(self) -> None:
        os.environ["ATLASSIAN_EMAIL"] = "already-set@example.com"
        (self.tmp_path / ".env").write_text("ATLASSIAN_EMAIL=from-file@example.com\n")
        orchestrator._load_plugin_env(self.tmp_path)
        self.assertEqual(os.environ["ATLASSIAN_EMAIL"], "already-set@example.com")

    def test_ignores_blank_lines_and_comments(self) -> None:
        (self.tmp_path / ".env").write_text("\n# a comment\nATLASSIAN_EMAIL=from-file@example.com\n")
        orchestrator._load_plugin_env(self.tmp_path)
        self.assertEqual(os.environ["ATLASSIAN_EMAIL"], "from-file@example.com")


class TestReposDir(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("REPOS_DIR", "CLAUDE_PLUGIN_OPTION_REPOS_DIR")
        }
        self.default = self.tmp_path / "default"

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        super().tearDown()

    def test_falls_back_to_default_when_unset(self) -> None:
        self.assertEqual(orchestrator._repos_dir(self.default), self.default)

    def test_prefers_bare_env_var(self) -> None:
        bare = self.tmp_path / "bare"
        plugin_opt = self.tmp_path / "plugin-opt"
        os.environ["REPOS_DIR"] = str(bare)
        os.environ["CLAUDE_PLUGIN_OPTION_REPOS_DIR"] = str(plugin_opt)
        self.assertEqual(orchestrator._repos_dir(self.default), bare)

    def test_falls_back_to_plugin_option_env_var(self) -> None:
        plugin_opt = self.tmp_path / "plugin-opt"
        os.environ["CLAUDE_PLUGIN_OPTION_REPOS_DIR"] = str(plugin_opt)
        self.assertEqual(orchestrator._repos_dir(self.default), plugin_opt)

    def test_expands_tilde(self) -> None:
        # A bare Path(...).resolve() does NOT expand ~ — it resolves it as a
        # literal directory named "~" relative to cwd instead of the home
        # directory, silently pointing repos_dir at a nonexistent path.
        os.environ["REPOS_DIR"] = "~/devtemp"
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp_path)
        try:
            self.assertEqual(orchestrator._repos_dir(self.default), self.tmp_path / "devtemp")
        finally:
            if saved_home is not None:
                os.environ["HOME"] = saved_home
            else:
                os.environ.pop("HOME", None)


class TestWorkerLock(TempDirTestCase):
    def test_is_locked_false_when_no_lock_file(self) -> None:
        self.assertFalse(orchestrator.is_locked(self.tmp_path))

    def test_is_locked_false_when_free(self) -> None:
        (self.tmp_path / orchestrator.LOCK_FILENAME).touch()
        self.assertFalse(orchestrator.is_locked(self.tmp_path))

    def test_is_locked_true_when_held(self) -> None:
        lock_path = self.tmp_path / orchestrator.LOCK_FILENAME
        holder = open(lock_path, "a+")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertTrue(orchestrator.is_locked(self.tmp_path))
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_try_acquire_lock_succeeds_when_free(self) -> None:
        fh = orchestrator.try_acquire_lock(self.tmp_path)
        self.assertIsNotNone(fh)
        fh.close()

    def test_try_acquire_lock_fails_when_held(self) -> None:
        holder = orchestrator.try_acquire_lock(self.tmp_path)
        self.assertIsNotNone(holder)
        try:
            self.assertIsNone(orchestrator.try_acquire_lock(self.tmp_path))
        finally:
            holder.close()

    def test_try_repo_lock_succeeds_when_free(self) -> None:
        fh = orchestrator.try_repo_lock(self.tmp_path, "some-repo")
        self.assertIsNotNone(fh)
        # Must actually be a held OS lock, not a truthy wrapper object
        # (regression guard for the @contextlib.contextmanager bug: calling
        # this directly used to return an always-truthy _GeneratorContextManager
        # instead of ever running flock() at all).
        second = orchestrator.try_repo_lock(self.tmp_path, "some-repo")
        self.assertIsNone(second)
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    def test_try_repo_lock_fails_when_held(self) -> None:
        holder = orchestrator.try_repo_lock(self.tmp_path, "some-repo")
        self.assertIsNotNone(holder)
        try:
            self.assertIsNone(orchestrator.try_repo_lock(self.tmp_path, "some-repo"))
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_try_repo_lock_is_per_repo(self) -> None:
        holder = orchestrator.try_repo_lock(self.tmp_path, "repo-a")
        self.assertIsNotNone(holder)
        try:
            other = orchestrator.try_repo_lock(self.tmp_path, "repo-b")
            self.assertIsNotNone(other)
            fcntl.flock(other, fcntl.LOCK_UN)
            other.close()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


class TestRemoveWorktrees(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket_dir = self.tmp_path / "tickets" / "PDE-1"
        self.repos_dir = self.tmp_path / "repos"
        self.ticket_dir.mkdir(parents=True)
        self.repos_dir.mkdir(parents=True)

    def _make_worktree(self, repo: str) -> Path:
        entry = self.ticket_dir / repo
        entry.mkdir()
        (entry / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
        (self.repos_dir / repo).mkdir()
        return entry

    def test_missing_ticket_dir_is_a_noop(self) -> None:
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            orchestrator.remove_worktrees("PDE-1", self.tmp_path / "does-not-exist", self.repos_dir)
        mock_run.assert_not_called()

    def test_skips_non_worktree_directories(self) -> None:
        real_clone = self.ticket_dir / "not-a-worktree"
        real_clone.mkdir()
        (real_clone / ".git").mkdir()  # a real clone's .git is a directory, not a file
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            orchestrator.remove_worktrees("PDE-1", self.ticket_dir, self.repos_dir)
        mock_run.assert_not_called()

    def test_skips_when_main_clone_missing(self) -> None:
        entry = self.ticket_dir / "orphan-repo"
        entry.mkdir()
        (entry / ".git").write_text("gitdir: ...\n")
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            orchestrator.remove_worktrees("PDE-1", self.ticket_dir, self.repos_dir)
        mock_run.assert_not_called()

    def test_removes_worktree_when_lock_is_free(self) -> None:
        entry = self._make_worktree("my-repo")
        with patch.object(orchestrator.subprocess, "run") as mock_run:
            orchestrator.remove_worktrees("PDE-1", self.ticket_dir, self.repos_dir)
        self.assertEqual(mock_run.call_count, 2)
        remove_call, prune_call = mock_run.call_args_list
        self.assertIn("worktree", remove_call.args[0])
        self.assertIn("remove", remove_call.args[0])
        self.assertIn(str(entry), remove_call.args[0])
        self.assertIn("prune", prune_call.args[0])

    def test_skips_when_repo_lock_is_held(self) -> None:
        self._make_worktree("locked-repo")
        held = orchestrator.try_repo_lock(self.repos_dir, "locked-repo")
        self.assertIsNotNone(held)
        try:
            with patch.object(orchestrator.subprocess, "run") as mock_run:
                orchestrator.remove_worktrees("PDE-1", self.ticket_dir, self.repos_dir)
            mock_run.assert_not_called()
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

    def test_releases_lock_even_when_git_command_fails(self) -> None:
        # The repo lock must be released via the `finally` block regardless
        # of whether `git worktree remove`/`prune` actually succeeds — a
        # crash here must not leave the repo permanently stuck for every
        # future cleanup pass.
        self._make_worktree("flaky-repo")
        with patch.object(orchestrator.subprocess, "run", side_effect=RuntimeError("boom")):
            with self.assertLogs(orchestrator.log, level="WARNING"):
                orchestrator.remove_worktrees("PDE-1", self.ticket_dir, self.repos_dir)

        # Behavioral check, not an implementation-detail one: if the lock
        # weren't released, this re-acquire would fail.
        fh = orchestrator.try_repo_lock(self.repos_dir, "flaky-repo")
        self.assertIsNotNone(fh, "repo lock was not released after the git command raised")
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


class TestCloseRemoteArtifacts(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket_dir = self.tmp_path / "tickets" / "PDE-1"
        self.repos_dir = self.tmp_path / "repos"
        self.ticket_dir.mkdir(parents=True)
        self.repos_dir.mkdir(parents=True)

    def test_always_removes_worktrees_even_with_no_review_context(self) -> None:
        with patch.object(orchestrator, "remove_worktrees") as mock_remove:
            orchestrator.close_remote_artifacts("PDE-1", self.ticket_dir, self.repos_dir)
        mock_remove.assert_called_once_with("PDE-1", self.ticket_dir, self.repos_dir)

    def test_warns_and_stops_when_review_context_missing_fields(self) -> None:
        (self.ticket_dir / "review-context.md").write_text("# Review Context\n\nnothing useful here\n")
        with patch.object(orchestrator, "remove_worktrees"), \
             patch.object(orchestrator.subprocess, "run") as mock_run:
            orchestrator.close_remote_artifacts("PDE-1", self.ticket_dir, self.repos_dir)
        mock_run.assert_not_called()

    def test_closes_open_pr_and_deletes_branch(self) -> None:
        (self.ticket_dir / "review-context.md").write_text(
            "**PR:** #42\n**PR URL:** https://x/42\n**Repo:** my-repo\n**Branch:** feature/x\n"
        )

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gh", "pr", "view"]:
                result = MagicMock()
                result.stdout = "OPEN"
                return result
            return MagicMock()

        with patch.object(orchestrator, "remove_worktrees"), \
             patch.object(orchestrator.subprocess, "run", side_effect=fake_run) as mock_run:
            orchestrator.close_remote_artifacts("PDE-1", self.ticket_dir, self.repos_dir)

        calls = [c.args[0] for c in mock_run.call_args_list]
        self.assertTrue(any(c[:3] == ["gh", "pr", "close"] for c in calls))
        self.assertTrue(any("git/refs/heads/feature/x" in " ".join(c) for c in calls))


class TestClaudeProjectDir(TempDirTestCase):
    def test_encodes_path_by_replacing_slashes_with_dashes(self) -> None:
        claude_projects_dir = self.tmp_path / "projects"
        cwd = Path("/home/x/tickets/PDE-1")
        self.assertEqual(
            orchestrator._claude_project_dir(cwd, claude_projects_dir),
            claude_projects_dir / "-home-x-tickets-PDE-1",
        )

    def test_encodes_underscores_as_dashes_too(self) -> None:
        # Not just "/" — verified directly against a real ~/.claude/projects/
        # bucket that a cwd containing "_" (e.g. .../orch_run/tickets/PDE-1)
        # is stored as .../orch-run-tickets-PDE-1, underscore included.
        claude_projects_dir = self.tmp_path / "projects"
        cwd = Path("/home/x/orch_run/tickets/PDE-1")
        self.assertEqual(
            orchestrator._claude_project_dir(cwd, claude_projects_dir),
            claude_projects_dir / "-home-x-orch-run-tickets-PDE-1",
        )


class TestArchiveClaudeSessions(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session_dir = self.tmp_path / "session-dir"
        self.dest = self.tmp_path / "dest"
        self.dest.mkdir()

    def test_noop_when_session_dir_missing(self) -> None:
        orchestrator._archive_claude_sessions(self.session_dir, self.dest)  # must not raise
        self.assertFalse((self.dest / "claude-sessions").exists())

    def test_copies_transcripts_and_removes_original(self) -> None:
        self.session_dir.mkdir()
        (self.session_dir / "abc123.jsonl").write_text("transcript content")

        orchestrator._archive_claude_sessions(self.session_dir, self.dest)

        self.assertEqual((self.dest / "claude-sessions" / "abc123.jsonl").read_text(), "transcript content")
        self.assertFalse(self.session_dir.exists())

    def test_swallows_errors_and_leaves_original_in_place(self) -> None:
        self.session_dir.mkdir()
        (self.session_dir / "abc123.jsonl").write_text("transcript content")

        with patch.object(orchestrator.shutil, "copytree", side_effect=OSError("disk full")):
            with self.assertLogs(orchestrator.log, level="WARNING"):
                orchestrator._archive_claude_sessions(self.session_dir, self.dest)  # must not raise

        self.assertTrue(self.session_dir.exists())  # not deleted since the copy never succeeded


class TestArchiveTicket(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved_log_dir = os.environ.pop("AGENT_CHILD_LOG_DIR", None)

    def tearDown(self) -> None:
        if self._saved_log_dir is not None:
            os.environ["AGENT_CHILD_LOG_DIR"] = self._saved_log_dir
        else:
            os.environ.pop("AGENT_CHILD_LOG_DIR", None)
        super().tearDown()

    def test_moves_directory_and_avoids_collisions(self) -> None:
        ticket_dir = self.tmp_path / "tickets" / "PDE-1"
        ticket_dir.mkdir(parents=True)
        archive_dir = self.tmp_path / "tickets" / "archive"
        archive_dir.mkdir(parents=True)

        # Pre-occupy today's expected destination to exercise the suffix logic.
        from datetime import date
        (archive_dir / f"PDE-1-{date.today()}").mkdir()

        with patch.object(orchestrator, "close_remote_artifacts") as mock_close:
            orchestrator.archive_ticket("PDE-1", ticket_dir, archive_dir, self.tmp_path / "repos")

        mock_close.assert_called_once()
        self.assertFalse(ticket_dir.exists())
        self.assertTrue((archive_dir / f"PDE-1-{date.today()}-1").exists())

    def test_moves_external_logs_into_the_archive_folder_too(self) -> None:
        ticket_dir = self.tmp_path / "tickets" / "PDE-1"
        ticket_dir.mkdir(parents=True)
        archive_dir = self.tmp_path / "tickets" / "archive"
        archive_dir.mkdir(parents=True)
        log_dir = self.tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "PDE-1.log").write_text("worker log")
        (log_dir / "PDE-1-ticket-discovery.log").write_text("specialist log")
        os.environ["AGENT_CHILD_LOG_DIR"] = str(log_dir)

        with patch.object(orchestrator, "close_remote_artifacts"):
            orchestrator.archive_ticket("PDE-1", ticket_dir, archive_dir, self.tmp_path / "repos")

        from datetime import date
        dest = archive_dir / f"PDE-1-{date.today()}"
        # Moved, not deleted — they persist until this archive folder itself
        # is purged (see cleanup_pass), same lifetime as every other artifact.
        self.assertEqual((dest / "PDE-1.log").read_text(), "worker log")
        self.assertEqual((dest / "PDE-1-ticket-discovery.log").read_text(), "specialist log")
        self.assertFalse((log_dir / "PDE-1.log").exists())

    def test_archives_claude_session_history_and_clears_it_from_live_storage(self) -> None:
        ticket_dir = self.tmp_path / "tickets" / "PDE-1"
        ticket_dir.mkdir(parents=True)
        archive_dir = self.tmp_path / "tickets" / "archive"
        archive_dir.mkdir(parents=True)
        claude_projects_dir = self.tmp_path / "claude-projects"
        session_dir = orchestrator._claude_project_dir(ticket_dir, claude_projects_dir)
        session_dir.mkdir(parents=True)
        (session_dir / "abc123.jsonl").write_text("transcript content")

        with patch.object(orchestrator, "close_remote_artifacts"):
            orchestrator.archive_ticket(
                "PDE-1", ticket_dir, archive_dir, self.tmp_path / "repos",
                claude_projects_dir=claude_projects_dir,
            )

        from datetime import date
        dest = archive_dir / f"PDE-1-{date.today()}"
        self.assertEqual((dest / "claude-sessions" / "abc123.jsonl").read_text(), "transcript content")
        self.assertFalse(session_dir.exists())  # cleared from live storage, not left to collide with a future run


class TestMoveExternalLogs(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved_log_dir = os.environ.pop("AGENT_CHILD_LOG_DIR", None)
        self.dest = self.tmp_path / "dest"
        self.dest.mkdir()

    def tearDown(self) -> None:
        if self._saved_log_dir is not None:
            os.environ["AGENT_CHILD_LOG_DIR"] = self._saved_log_dir
        else:
            os.environ.pop("AGENT_CHILD_LOG_DIR", None)
        super().tearDown()

    def test_noop_when_env_var_unset(self) -> None:
        orchestrator.move_external_logs("PDE-1", self.dest)  # must not raise
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_noop_when_log_dir_does_not_exist(self) -> None:
        os.environ["AGENT_CHILD_LOG_DIR"] = str(self.tmp_path / "does-not-exist")
        orchestrator.move_external_logs("PDE-1", self.dest)  # must not raise

    def test_moves_worker_and_specialist_logs(self) -> None:
        log_dir = self.tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "PDE-1.log").write_text("worker log")
        (log_dir / "PDE-1-ticket-discovery.log").write_text("specialist log")
        os.environ["AGENT_CHILD_LOG_DIR"] = str(log_dir)

        orchestrator.move_external_logs("PDE-1", self.dest)

        self.assertEqual((self.dest / "PDE-1.log").read_text(), "worker log")
        self.assertEqual((self.dest / "PDE-1-ticket-discovery.log").read_text(), "specialist log")
        self.assertFalse((log_dir / "PDE-1.log").exists())
        self.assertFalse((log_dir / "PDE-1-ticket-discovery.log").exists())

    def test_does_not_touch_other_tickets_with_a_shared_prefix(self) -> None:
        # "PDE-1" is a literal string prefix of "PDE-10"/"PDE-100" — moving
        # PDE-1's logs must not sweep up either of theirs.
        log_dir = self.tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "PDE-1.log").write_text("PDE-1")
        (log_dir / "PDE-10.log").write_text("PDE-10")
        (log_dir / "PDE-10-ticket-discovery.log").write_text("PDE-10 specialist")
        os.environ["AGENT_CHILD_LOG_DIR"] = str(log_dir)

        orchestrator.move_external_logs("PDE-1", self.dest)

        self.assertFalse((self.dest / "PDE-10.log").exists())
        self.assertFalse((self.dest / "PDE-10-ticket-discovery.log").exists())
        self.assertTrue((log_dir / "PDE-10.log").exists())
        self.assertTrue((log_dir / "PDE-10-ticket-discovery.log").exists())


class TestNormalizeAgentChildLogDir(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop("AGENT_CHILD_LOG_DIR", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["AGENT_CHILD_LOG_DIR"] = self._saved
        else:
            os.environ.pop("AGENT_CHILD_LOG_DIR", None)

    def test_noop_when_unset(self) -> None:
        orchestrator._normalize_agent_child_log_dir()
        self.assertNotIn("AGENT_CHILD_LOG_DIR", os.environ)

    def test_resolves_relative_path_to_absolute(self) -> None:
        os.environ["AGENT_CHILD_LOG_DIR"] = "some-relative-dir"
        orchestrator._normalize_agent_child_log_dir()
        self.assertTrue(Path(os.environ["AGENT_CHILD_LOG_DIR"]).is_absolute())

    def test_idempotent_on_already_absolute_path(self) -> None:
        os.environ["AGENT_CHILD_LOG_DIR"] = "/tmp/already-absolute"
        orchestrator._normalize_agent_child_log_dir()
        self.assertEqual(os.environ["AGENT_CHILD_LOG_DIR"], "/tmp/already-absolute")

    def test_expands_tilde(self) -> None:
        os.environ["AGENT_CHILD_LOG_DIR"] = "~/logs"
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = "/tmp/fake-home"
        try:
            orchestrator._normalize_agent_child_log_dir()
            self.assertEqual(os.environ["AGENT_CHILD_LOG_DIR"], "/tmp/fake-home/logs")
        finally:
            if saved_home is not None:
                os.environ["HOME"] = saved_home
            else:
                os.environ.pop("HOME", None)


class TestCleanupPass(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tickets_dir = self.tmp_path / "tickets"
        self.archive_dir = self.tickets_dir / "archive"
        self.repos_dir = self.tmp_path / "repos"
        self.tickets_dir.mkdir(parents=True)
        self.archive_dir.mkdir(parents=True)
        self.repos_dir.mkdir(parents=True)

    def test_archives_inactive_ticket(self) -> None:
        (self.tickets_dir / "PDE-1").mkdir()
        (self.tickets_dir / "PDE-2").mkdir()
        with patch.object(orchestrator, "archive_ticket") as mock_archive:
            orchestrator.cleanup_pass(self.tickets_dir, self.archive_dir, {"PDE-2"}, self.repos_dir)
        mock_archive.assert_called_once_with("PDE-1", self.tickets_dir / "PDE-1", self.archive_dir, self.repos_dir)

    def test_leaves_inactive_but_locked_ticket_alone(self) -> None:
        folder = self.tickets_dir / "PDE-1"
        folder.mkdir()
        held = orchestrator.try_acquire_lock(folder)
        try:
            with patch.object(orchestrator, "archive_ticket") as mock_archive:
                orchestrator.cleanup_pass(self.tickets_dir, self.archive_dir, set(), self.repos_dir)
            mock_archive.assert_not_called()
        finally:
            held.close()

    def test_purges_old_archives_but_not_recent_ones(self) -> None:
        old = self.archive_dir / "PDE-OLD-2020-01-01"
        recent = self.archive_dir / "PDE-RECENT-2099-01-01"
        old.mkdir()
        recent.mkdir()
        old_time = orchestrator.time.time() - (orchestrator.ARCHIVE_MAX_DAYS + 1) * 86400
        os.utime(old, (old_time, old_time))

        with patch.object(orchestrator, "remove_worktrees"):
            orchestrator.cleanup_pass(self.tickets_dir, self.archive_dir, set(), self.repos_dir)

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_archive_failure_for_one_ticket_does_not_stop_the_others(self) -> None:
        (self.tickets_dir / "PDE-1").mkdir()
        (self.tickets_dir / "PDE-2").mkdir()

        def fake_archive(key, folder, archive_dir, repos_dir):
            if key == "PDE-1":
                raise RuntimeError("boom")

        with patch.object(orchestrator, "archive_ticket", side_effect=fake_archive) as mock_archive:
            with self.assertLogs(orchestrator.log, level="ERROR"):
                orchestrator.cleanup_pass(self.tickets_dir, self.archive_dir, set(), self.repos_dir)

        self.assertEqual(mock_archive.call_count, 2)  # PDE-2 still attempted despite PDE-1 failing

    def test_purge_failure_for_one_archive_does_not_stop_the_others(self) -> None:
        bad = self.archive_dir / "PDE-BAD-2020-01-01"
        good = self.archive_dir / "PDE-GOOD-2020-01-01"
        bad.mkdir()
        good.mkdir()
        old_time = orchestrator.time.time() - (orchestrator.ARCHIVE_MAX_DAYS + 1) * 86400
        os.utime(bad, (old_time, old_time))
        os.utime(good, (old_time, old_time))

        def fake_remove_worktrees(label, folder, repos_dir):
            if "BAD" in label:
                raise RuntimeError("boom")

        with patch.object(orchestrator, "remove_worktrees", side_effect=fake_remove_worktrees):
            with self.assertLogs(orchestrator.log, level="ERROR"):
                orchestrator.cleanup_pass(self.tickets_dir, self.archive_dir, set(), self.repos_dir)

        self.assertTrue(bad.exists())  # failed to purge — left in place, not half-deleted
        self.assertFalse(good.exists())  # still purged despite the other one failing


class TestProcessTicket(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cwd = self.tmp_path
        self.repos_dir = self.tmp_path / "repos"
        self.repos_dir.mkdir(parents=True)

    def test_skips_when_already_locked(self) -> None:
        ticket_dir = self.cwd / "tickets" / "PDE-1"
        ticket_dir.mkdir(parents=True)
        held = orchestrator.try_acquire_lock(ticket_dir)
        try:
            with patch.object(orchestrator, "launch_session") as mock_launch:
                result = orchestrator.process_ticket("PDE-1", "In Progress", self.cwd, self.repos_dir)
            self.assertIsNone(result)
            mock_launch.assert_not_called()
        finally:
            held.close()

    def test_fresh_to_do_launch_archives_prior_dir_and_renames_session(self) -> None:
        ticket_dir = self.cwd / "tickets" / "PDE-1"
        ticket_dir.mkdir(parents=True)
        with patch.object(orchestrator, "rename_stale_session") as mock_rename, \
             patch.object(orchestrator, "archive_ticket") as mock_archive, \
             patch.object(orchestrator, "launch_session", return_value=1234) as mock_launch:
            result = orchestrator.process_ticket("PDE-1", "To Do", self.cwd, self.repos_dir)

        mock_rename.assert_called_once_with("PDE-1")
        mock_archive.assert_called_once()
        mock_launch.assert_called_once()
        self.assertEqual(mock_launch.call_args.args[3], True)  # is_new
        self.assertEqual(result, ticket_dir)

    def test_resume_does_not_rename_or_archive(self) -> None:
        ticket_dir = self.cwd / "tickets" / "PDE-1"
        ticket_dir.mkdir(parents=True)
        with patch.object(orchestrator, "rename_stale_session") as mock_rename, \
             patch.object(orchestrator, "archive_ticket") as mock_archive, \
             patch.object(orchestrator, "launch_session", return_value=1234) as mock_launch:
            result = orchestrator.process_ticket("PDE-1", "In Progress", self.cwd, self.repos_dir)

        mock_rename.assert_not_called()
        mock_archive.assert_not_called()
        mock_launch.assert_called_once()
        self.assertEqual(mock_launch.call_args.args[3], False)  # is_new
        self.assertEqual(result, ticket_dir)

    def test_brand_new_ticket_directory_is_a_fresh_launch(self) -> None:
        # No prior directory at all, and status isn't "To Do" — still counts
        # as fresh since fresh-vs-resume is decided by directory existence,
        # not by status alone (see process_ticket's own docstring).
        with patch.object(orchestrator, "rename_stale_session") as mock_rename, \
             patch.object(orchestrator, "archive_ticket") as mock_archive, \
             patch.object(orchestrator, "launch_session", return_value=1234) as mock_launch:
            orchestrator.process_ticket("PDE-1", "In Progress", self.cwd, self.repos_dir)

        mock_rename.assert_called_once_with("PDE-1")
        mock_archive.assert_not_called()  # nothing to archive — there was no prior directory
        self.assertEqual(mock_launch.call_args.args[3], True)  # is_new

    def test_lock_race_at_launch_time_is_handled_safely(self) -> None:
        with patch.object(orchestrator, "try_acquire_lock", return_value=None), \
             patch.object(orchestrator, "launch_session") as mock_launch:
            result = orchestrator.process_ticket("PDE-1", "In Progress", self.cwd, self.repos_dir)
        self.assertIsNone(result)
        mock_launch.assert_not_called()


class TestLaunchSession(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket_dir = self.tmp_path / "tickets" / "PDE-1"
        self.repos_dir = self.tmp_path / "repos"
        self.ticket_dir.mkdir(parents=True)
        self.repos_dir.mkdir(parents=True)
        self.lock_fh = orchestrator.try_acquire_lock(self.ticket_dir)
        self.assertIsNotNone(self.lock_fh)
        self._saved_log_dir = os.environ.pop("AGENT_CHILD_LOG_DIR", None)

    def tearDown(self) -> None:
        if self._saved_log_dir is not None:
            os.environ["AGENT_CHILD_LOG_DIR"] = self._saved_log_dir
        super().tearDown()

    def test_fresh_launch_uses_name_not_resume(self) -> None:
        with patch.object(orchestrator.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 999
            orchestrator.launch_session("PDE-1", self.ticket_dir, self.repos_dir, True, self.lock_fh)
        cmd = mock_popen.call_args.args[0]
        self.assertIn("--name=PDE-1", cmd)
        self.assertNotIn("--resume=PDE-1", cmd)

    def test_resume_launch_uses_resume_not_name(self) -> None:
        with patch.object(orchestrator.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 999
            orchestrator.launch_session("PDE-1", self.ticket_dir, self.repos_dir, False, self.lock_fh)
        cmd = mock_popen.call_args.args[0]
        self.assertIn("--resume=PDE-1", cmd)
        self.assertNotIn("--name=PDE-1", cmd)

    def test_sets_cwd_and_invokes_ticket_worker_with_repos_dir(self) -> None:
        with patch.object(orchestrator.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 999
            orchestrator.launch_session("PDE-1", self.ticket_dir, self.repos_dir, True, self.lock_fh)
        cmd, kwargs = mock_popen.call_args.args[0], mock_popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(self.ticket_dir))
        self.assertIn("-p", cmd)
        self.assertIn(f"/jexpress:ticket-worker Repos directory: {self.repos_dir}", cmd)

    def test_hands_off_lock_fd_and_closes_own_reference(self) -> None:
        lock_fd = self.lock_fh.fileno()
        with patch.object(orchestrator.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 999
            orchestrator.launch_session("PDE-1", self.ticket_dir, self.repos_dir, True, self.lock_fh)

        self.assertEqual(mock_popen.call_args.kwargs["pass_fds"], (lock_fd,))
        # The orchestrator's own reference must be closed after handing the
        # fd to the child — otherwise the lock would outlive the child that's
        # actually supposed to be holding it.
        self.assertTrue(self.lock_fh.closed)

    def test_returns_child_pid(self) -> None:
        with patch.object(orchestrator.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 42424
            pid = orchestrator.launch_session("PDE-1", self.ticket_dir, self.repos_dir, True, self.lock_fh)
        self.assertEqual(pid, 42424)

    def test_logs_to_ticket_dir_by_default(self) -> None:
        with patch.object(orchestrator.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 999
            orchestrator.launch_session("PDE-1", self.ticket_dir, self.repos_dir, True, self.lock_fh)
        log_file = mock_popen.call_args.kwargs["stdout"]
        self.assertEqual(Path(log_file.name), self.ticket_dir / "PDE-1.log")

    def test_logs_to_agent_child_log_dir_when_set(self) -> None:
        external_dir = self.tmp_path / "collected-logs"
        os.environ["AGENT_CHILD_LOG_DIR"] = str(external_dir)
        with patch.object(orchestrator.subprocess, "Popen") as mock_popen:
            mock_popen.return_value.pid = 999
            orchestrator.launch_session("PDE-1", self.ticket_dir, self.repos_dir, True, self.lock_fh)
        log_file = mock_popen.call_args.kwargs["stdout"]
        self.assertEqual(Path(log_file.name), external_dir / "PDE-1.log")
        self.assertTrue(external_dir.is_dir())  # created if missing


class TestMainDispatch(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cwd = self.tmp_path
        # main() defaults repos_dir to cwd only when neither REPOS_DIR nor its
        # userConfig equivalent is set — make sure a real environment's value
        # can't leak into these tests.
        self._saved_repos_dir = {
            k: os.environ.pop(k, None)
            for k in ("REPOS_DIR", "CLAUDE_PLUGIN_OPTION_REPOS_DIR")
        }

    def tearDown(self) -> None:
        for k, v in self._saved_repos_dir.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        super().tearDown()

    def _issue(self, key="PDE-1", status="In Progress", assignee_id="user-1"):
        return {
            "key": key,
            "fields": {
                "status": {"name": status},
                "assignee": {"accountId": assignee_id, "displayName": "Someone"} if assignee_id else None,
            },
        }

    def _run_main(self, issues, current_user_id="user-1", get_current_user_error=None):
        def fake_get_current_user(auth):
            if get_current_user_error:
                raise get_current_user_error
            return {"accountId": current_user_id} if current_user_id else {}

        with patch.object(orchestrator.Path, "cwd", return_value=self.cwd), \
             patch.object(orchestrator, "_auth", return_value=("e", "t")), \
             patch.object(orchestrator, "search_tickets", return_value=issues), \
             patch.object(orchestrator, "cleanup_pass") as mock_cleanup, \
             patch.object(orchestrator, "get_current_user", side_effect=fake_get_current_user), \
             patch.object(orchestrator, "process_ticket") as mock_process:
            orchestrator.main()
        return mock_cleanup, mock_process

    def test_refuses_to_dispatch_when_get_current_user_fails(self) -> None:
        mock_cleanup, mock_process = self._run_main(
            [self._issue()], get_current_user_error=RuntimeError("jira down")
        )
        mock_process.assert_not_called()
        mock_cleanup.assert_called_once()  # cleanup still runs — only dispatch is refused

    def test_refuses_to_dispatch_when_no_account_id_returned(self) -> None:
        _, mock_process = self._run_main([self._issue()], current_user_id=None)
        mock_process.assert_not_called()

    def test_skips_ticket_assigned_to_someone_else(self) -> None:
        _, mock_process = self._run_main(
            [self._issue(key="PDE-1", assignee_id="someone-else")],
            current_user_id="user-1",
        )
        mock_process.assert_not_called()

    def test_processes_actionable_ticket_assigned_to_current_user(self) -> None:
        _, mock_process = self._run_main(
            [self._issue(key="PDE-1", status="In Progress", assignee_id="user-1")],
            current_user_id="user-1",
        )
        mock_process.assert_called_once_with("PDE-1", "In Progress", self.cwd, self.cwd)

    def test_skips_non_actionable_status_even_if_assigned(self) -> None:
        _, mock_process = self._run_main(
            [self._issue(key="PDE-1", status="QA Review", assignee_id="user-1")],
            current_user_id="user-1",
        )
        mock_process.assert_not_called()

    def test_skips_non_pde_key_as_a_hard_safety_check(self) -> None:
        mock_cleanup, mock_process = self._run_main(
            [self._issue(key="OTHER-1", status="In Progress", assignee_id="user-1")],
            current_user_id="user-1",
        )
        mock_process.assert_not_called()
        # Also must never make it into cleanup's active_keys set.
        active_keys_arg = mock_cleanup.call_args.args[2]
        self.assertNotIn("OTHER-1", active_keys_arg)

    def test_no_issues_still_runs_cleanup_with_empty_active_keys(self) -> None:
        mock_cleanup, mock_process = self._run_main([])
        mock_cleanup.assert_called_once()
        self.assertEqual(mock_cleanup.call_args.args[2], set())
        mock_process.assert_not_called()

    def test_one_ticket_erroring_does_not_stop_the_rest(self) -> None:
        def fake_process(key, status, cwd, repos_dir):
            if key == "PDE-1":
                raise RuntimeError("boom")

        with patch.object(orchestrator.Path, "cwd", return_value=self.cwd), \
             patch.object(orchestrator, "_auth", return_value=("e", "t")), \
             patch.object(orchestrator, "search_tickets", return_value=[
                 self._issue(key="PDE-1", assignee_id="user-1"),
                 self._issue(key="PDE-2", assignee_id="user-1"),
             ]), \
             patch.object(orchestrator, "cleanup_pass"), \
             patch.object(orchestrator, "get_current_user", return_value={"accountId": "user-1"}), \
             patch.object(orchestrator, "process_ticket", side_effect=fake_process) as mock_process:
            with self.assertLogs(orchestrator.log, level="ERROR"):
                orchestrator.main()

        self.assertEqual(mock_process.call_count, 2)  # PDE-2 still attempted despite PDE-1 failing


class TestSearchTickets(unittest.TestCase):
    def test_sends_expected_jql_and_returns_issues(self) -> None:
        fake_response = MagicMock()
        fake_response.json.return_value = {"issues": [{"key": "PDE-1"}], "isLast": True}
        with patch.object(orchestrator.requests, "post", return_value=fake_response) as mock_post:
            issues = orchestrator.search_tickets(("email", "token"))
        self.assertEqual(issues, [{"key": "PDE-1"}])
        sent_json = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_json["jql"], orchestrator.JQL)

    def test_warns_when_hitting_the_per_run_cap(self) -> None:
        many_issues = [{"key": f"PDE-{i}"} for i in range(orchestrator.MAX_TICKETS_PER_RUN)]
        fake_response = MagicMock()
        fake_response.json.return_value = {"issues": many_issues, "isLast": False}
        with patch.object(orchestrator.requests, "post", return_value=fake_response):
            with self.assertLogs(orchestrator.log, level="WARNING"):
                orchestrator.search_tickets(("email", "token"))


if __name__ == "__main__":
    unittest.main()
