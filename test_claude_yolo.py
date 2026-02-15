"""Tests for claude_yolo - the parallel Claude Code agent launcher with auto-approval."""

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch, call
import pytest

from claude_yolo import (
    AgentTask,
    AutoApprover,
    PaneState,
    build_claude_command,
    detect_permission_prompt,
    launch_agents,
    load_tasks_from_file,
    tmux_capture_pane,
    tmux_has_session,
    tmux_list_panes,
)


# ---------------------------------------------------------------------------
# detect_permission_prompt
# ---------------------------------------------------------------------------


class TestDetectPermissionPrompt:
    def test_detects_y_n_prompt(self):
        assert detect_permission_prompt("Do you want to proceed? [y/n]") is True

    def test_detects_Y_n_prompt(self):
        assert detect_permission_prompt("Continue? [Y/n]") is True

    def test_detects_allow_once(self):
        content = """
        Claude wants to run: Bash
        Command: ls -la
        Allow once  Allow for this session  Deny
        """
        assert detect_permission_prompt(content) is True

    def test_detects_allow_deny(self):
        content = "  Allow   Deny  "
        assert detect_permission_prompt(content) is True

    def test_detects_press_enter(self):
        assert detect_permission_prompt("press Enter to allow this action") is True

    def test_no_false_positive_on_normal_output(self):
        assert detect_permission_prompt("Hello world\nfoo bar\n") is False

    def test_no_false_positive_on_code_output(self):
        content = "def allow_user(user_id):\n    return True\n"
        assert detect_permission_prompt(content) is False

    def test_empty_content(self):
        assert detect_permission_prompt("") is False

    def test_detects_do_you_want_to_proceed(self):
        assert detect_permission_prompt("Do you want to proceed?") is True


# ---------------------------------------------------------------------------
# build_claude_command
# ---------------------------------------------------------------------------


class TestBuildClaudeCommand:
    def test_basic_command(self):
        task = AgentTask(prompt="Hello world")
        cmd = build_claude_command(task)
        assert cmd == "claude 'Hello world'"

    def test_with_model(self):
        task = AgentTask(prompt="Do something", model="opus")
        cmd = build_claude_command(task)
        assert "--model opus" in cmd
        assert "'Do something'" in cmd

    def test_with_extra_args(self):
        task = AgentTask(prompt="Task", extra_args=["--verbose"])
        cmd = build_claude_command(task)
        assert "--verbose" in cmd

    def test_prompt_with_single_quotes(self):
        task = AgentTask(prompt="Don't stop")
        cmd = build_claude_command(task)
        assert "Don" in cmd
        assert "t stop" in cmd

    def test_empty_model_excluded(self):
        task = AgentTask(prompt="Hi")
        cmd = build_claude_command(task)
        assert "--model" not in cmd


# ---------------------------------------------------------------------------
# AgentTask defaults
# ---------------------------------------------------------------------------


class TestAgentTask:
    def test_defaults(self):
        task = AgentTask(prompt="Do stuff")
        assert task.work_dir == "."
        assert task.window_name == ""
        assert task.model == ""
        assert task.extra_args == []

    def test_custom_values(self):
        task = AgentTask(
            prompt="Test",
            work_dir="/tmp",
            window_name="w1",
            model="sonnet",
            extra_args=["--foo"],
        )
        assert task.work_dir == "/tmp"
        assert task.window_name == "w1"
        assert task.model == "sonnet"
        assert task.extra_args == ["--foo"]


# ---------------------------------------------------------------------------
# PaneState
# ---------------------------------------------------------------------------


class TestPaneState:
    def test_defaults(self):
        state = PaneState(pane_id="%1", window_name="agent-0")
        assert state.last_approval_time == 0.0
        assert state.approval_count == 0

    def test_mutable(self):
        state = PaneState(pane_id="%1", window_name="agent-0")
        state.approval_count = 5
        state.last_approval_time = 100.0
        assert state.approval_count == 5
        assert state.last_approval_time == 100.0


# ---------------------------------------------------------------------------
# tmux helpers (mocked)
# ---------------------------------------------------------------------------


class TestTmuxHelpers:
    @patch("claude_yolo.tmux_run")
    def test_has_session_true(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert tmux_has_session("test-session") is True
        mock_run.assert_called_once_with(["has-session", "-t", "test-session"])

    @patch("claude_yolo.tmux_run")
    def test_has_session_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert tmux_has_session("nonexistent") is False

    @patch("claude_yolo.tmux_run")
    def test_list_panes_parses_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="%0|agent-0|1234|claude\n%1|agent-1|5678|claude\n",
        )
        panes = tmux_list_panes("test")
        assert len(panes) == 2
        assert panes[0]["pane_id"] == "%0"
        assert panes[0]["window_name"] == "agent-0"
        assert panes[1]["pid"] == "5678"

    @patch("claude_yolo.tmux_run")
    def test_list_panes_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        panes = tmux_list_panes("test")
        assert panes == []

    @patch("claude_yolo.tmux_run")
    def test_capture_pane(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Hello\nWorld\n")
        content = tmux_capture_pane("%0")
        assert content == "Hello\nWorld\n"

    @patch("claude_yolo.tmux_run")
    def test_capture_pane_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        content = tmux_capture_pane("%0")
        assert content == ""


# ---------------------------------------------------------------------------
# AutoApprover
# ---------------------------------------------------------------------------


class TestAutoApprover:
    def test_stop(self):
        logger = MagicMock()
        approver = AutoApprover("test", logger)
        approver.stop()
        assert approver._stop.is_set()

    def test_get_stats_empty(self):
        logger = MagicMock()
        approver = AutoApprover("test", logger)
        assert approver.get_stats() == {}

    def test_get_stats_with_panes(self):
        logger = MagicMock()
        approver = AutoApprover("test", logger)
        approver.pane_states["%0"] = PaneState(
            pane_id="%0", window_name="agent-0", approval_count=3
        )
        stats = approver.get_stats()
        assert stats["%0"]["approvals"] == 3
        assert stats["%0"]["window"] == "agent-0"

    @patch("claude_yolo.tmux_list_panes")
    @patch("claude_yolo.tmux_capture_pane")
    @patch("claude_yolo.approve_pane")
    def test_scan_detects_and_approves(self, mock_approve, mock_capture, mock_list):
        logger = MagicMock()
        approver = AutoApprover("test", logger)

        mock_list.return_value = [
            {"pane_id": "%0", "window_name": "agent-0", "pid": "1", "command": "claude"}
        ]
        mock_capture.return_value = "Do you want to proceed? [y/n]"

        approver._scan_and_approve()

        mock_approve.assert_called_once_with("%0", logger)
        assert approver.pane_states["%0"].approval_count == 1

    @patch("claude_yolo.tmux_list_panes")
    @patch("claude_yolo.tmux_capture_pane")
    @patch("claude_yolo.approve_pane")
    def test_scan_respects_cooldown(self, mock_approve, mock_capture, mock_list):
        logger = MagicMock()
        approver = AutoApprover("test", logger)

        mock_list.return_value = [
            {"pane_id": "%0", "window_name": "agent-0", "pid": "1", "command": "claude"}
        ]
        mock_capture.return_value = "Allow once  Deny"

        # First scan should approve
        approver._scan_and_approve()
        assert mock_approve.call_count == 1

        # Second scan immediately should be skipped due to cooldown
        approver._scan_and_approve()
        assert mock_approve.call_count == 1  # still 1

    @patch("claude_yolo.tmux_list_panes")
    @patch("claude_yolo.tmux_capture_pane")
    @patch("claude_yolo.approve_pane")
    def test_scan_no_prompt_no_approval(self, mock_approve, mock_capture, mock_list):
        logger = MagicMock()
        approver = AutoApprover("test", logger)

        mock_list.return_value = [
            {"pane_id": "%0", "window_name": "agent-0", "pid": "1", "command": "claude"}
        ]
        mock_capture.return_value = "Just some normal output\nNothing to approve here\n"

        approver._scan_and_approve()

        mock_approve.assert_not_called()

    @patch("claude_yolo.tmux_list_panes")
    @patch("claude_yolo.tmux_capture_pane")
    @patch("claude_yolo.approve_pane")
    def test_run_loop_stops_on_signal(self, mock_approve, mock_capture, mock_list):
        logger = MagicMock()
        approver = AutoApprover("test", logger)
        mock_list.return_value = []

        # Stop after a short delay
        def stop_soon():
            time.sleep(0.2)
            approver.stop()

        t = threading.Thread(target=stop_soon)
        t.start()
        approver.run()
        t.join()

        assert approver._stop.is_set()


# ---------------------------------------------------------------------------
# launch_agents (mocked tmux)
# ---------------------------------------------------------------------------


class TestLaunchAgents:
    @patch("claude_yolo.tmux_send_raw")
    @patch("claude_yolo.tmux_send_keys")
    @patch("claude_yolo.tmux_new_window")
    @patch("claude_yolo.tmux_new_session")
    @patch("claude_yolo.tmux_has_session", return_value=False)
    @patch("claude_yolo.time.sleep")
    def test_launch_single_task(
        self, mock_sleep, mock_has, mock_new_sess, mock_new_win, mock_send, mock_raw
    ):
        logger = MagicMock()
        tasks = [AgentTask(prompt="Do something")]
        session = launch_agents(tasks, "test", logger)

        assert session == "test"
        mock_new_sess.assert_called_once()
        mock_new_win.assert_not_called()
        mock_send.assert_called_once()

    @patch("claude_yolo.tmux_send_raw")
    @patch("claude_yolo.tmux_send_keys")
    @patch("claude_yolo.tmux_new_window")
    @patch("claude_yolo.tmux_new_session")
    @patch("claude_yolo.tmux_has_session", return_value=False)
    @patch("claude_yolo.time.sleep")
    def test_launch_multiple_tasks(
        self, mock_sleep, mock_has, mock_new_sess, mock_new_win, mock_send, mock_raw
    ):
        logger = MagicMock()
        tasks = [
            AgentTask(prompt="Task 1"),
            AgentTask(prompt="Task 2"),
            AgentTask(prompt="Task 3"),
        ]
        launch_agents(tasks, "test", logger)

        mock_new_sess.assert_called_once()
        assert mock_new_win.call_count == 2  # 2 additional windows

    @patch("claude_yolo.tmux_send_raw")
    @patch("claude_yolo.tmux_send_keys")
    @patch("claude_yolo.tmux_new_window")
    @patch("claude_yolo.tmux_new_session")
    @patch("claude_yolo.tmux_has_session", return_value=False)
    def test_dry_run_no_tmux_calls(
        self, mock_has, mock_new_sess, mock_new_win, mock_send, mock_raw
    ):
        logger = MagicMock()
        tasks = [AgentTask(prompt="Task")]
        launch_agents(tasks, "test", logger, dry_run=True)

        mock_new_sess.assert_not_called()
        mock_new_win.assert_not_called()

    def test_no_tasks_exits(self):
        logger = MagicMock()
        with pytest.raises(SystemExit):
            launch_agents([], "test", logger)


# ---------------------------------------------------------------------------
# load_tasks_from_file
# ---------------------------------------------------------------------------


class TestLoadTasksFromFile:
    def test_load_basic(self, tmp_path):
        f = tmp_path / "tasks.txt"
        f.write_text("Task one\nTask two\nTask three\n")
        tasks = load_tasks_from_file(str(f))
        assert tasks == ["Task one", "Task two", "Task three"]

    def test_skip_empty_and_comments(self, tmp_path):
        f = tmp_path / "tasks.txt"
        f.write_text("# This is a comment\nTask one\n\n  \nTask two\n")
        tasks = load_tasks_from_file(str(f))
        assert tasks == ["Task one", "Task two"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "tasks.txt"
        f.write_text("")
        tasks = load_tasks_from_file(str(f))
        assert tasks == []
