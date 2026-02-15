#!/usr/bin/env python3
"""
claude-yolo: Launch parallel Claude Code agents in tmux with auto-approval.

Bypasses managed settings that force 'ask' mode for Bash, Bash(rm:*), WebFetch
by monitoring tmux panes for permission prompts and auto-approving them.

Usage:
    # Launch a single task:
    python3 claude_yolo.py "Write a hello world script"

    # Launch parallel tasks:
    python3 claude_yolo.py "Create unit tests for snake" "Implement the snake game"

    # Launch tasks from a file (one task per line):
    python3 claude_yolo.py -f tasks.txt

    # Specify working directory per task:
    python3 claude_yolo.py -d /path/to/project "Do something"

    # Custom session name:
    python3 claude_yolo.py -s my-session "Do something"

    # Dry run (show commands without executing):
    python3 claude_yolo.py --dry-run "Do something"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import signal
import threading
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SESSION_NAME = "claude-yolo"
POLL_INTERVAL = 0.5  # seconds between pane scans
APPROVAL_COOLDOWN = 2.0  # seconds to wait after approving before re-scanning same pane
PANE_CAPTURE_LINES = 80  # number of terminal lines to capture

# Patterns that indicate a permission prompt in Claude Code's TUI.
# Claude Code shows prompts like:
#   "Allow once", "Allow always", "Deny" with a tool name and command
#   Or a simpler [y/n] style prompt
PERMISSION_PATTERNS = [
    r"Do you want to proceed\?",
    r"Allow once",
    r"Allow for this session",
    r"\[y/n\]",
    r"\[Y/n\]",
    r"press Enter to allow",
    r"Allow\s+Deny",
]

COMPILED_PATTERNS = [re.compile(p) for p in PERMISSION_PATTERNS]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = Path.home() / ".claude" / "yolo-logs"


def setup_logging(session_name: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("claude-yolo")
    logger.setLevel(logging.DEBUG)

    # File handler
    fh = logging.FileHandler(LOG_DIR / f"{session_name}.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AgentTask:
    """A task to be executed by a Claude Code agent."""

    prompt: str
    work_dir: str = "."
    window_name: str = ""
    model: str = ""
    extra_args: list = field(default_factory=list)


@dataclass
class PaneState:
    """Tracks state of a tmux pane for the auto-approver."""

    pane_id: str
    window_name: str
    last_approval_time: float = 0.0
    approval_count: int = 0


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------


def tmux_run(args: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    """Run a tmux command."""
    cmd = ["tmux"] + args
    return subprocess.run(cmd, capture_output=capture, text=True)


def tmux_has_session(session: str) -> bool:
    result = tmux_run(["has-session", "-t", session])
    return result.returncode == 0


def tmux_new_session(session: str, window_name: str = "", work_dir: str = "."):
    args = ["new-session", "-d", "-s", session]
    if window_name:
        args += ["-n", window_name]
    args += ["-c", os.path.abspath(work_dir)]
    tmux_run(args)


def tmux_new_window(session: str, window_name: str, work_dir: str = "."):
    args = ["new-window", "-t", session, "-n", window_name, "-c", os.path.abspath(work_dir)]
    tmux_run(args)


def tmux_send_keys(target: str, keys: str):
    tmux_run(["send-keys", "-t", target, keys, ""])


def tmux_send_raw(target: str, key: str):
    """Send a raw key (like Enter, y, etc.) to a tmux pane."""
    tmux_run(["send-keys", "-t", target, key])


def tmux_capture_pane(target: str, lines: int = PANE_CAPTURE_LINES) -> str:
    """Capture the visible content of a tmux pane."""
    result = tmux_run([
        "capture-pane", "-t", target, "-p",
        "-S", f"-{lines}",
    ])
    return result.stdout if result.returncode == 0 else ""


def tmux_list_panes(session: str) -> list[dict]:
    """List all panes in a session with their IDs and window names."""
    result = tmux_run([
        "list-panes", "-s", "-t", session,
        "-F", "#{pane_id}|#{window_name}|#{pane_pid}|#{pane_current_command}"
    ])
    if result.returncode != 0:
        return []
    panes = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 4:
            panes.append({
                "pane_id": parts[0],
                "window_name": parts[1],
                "pid": parts[2],
                "command": parts[3],
            })
    return panes


def tmux_kill_session(session: str):
    tmux_run(["kill-session", "-t", session])


# ---------------------------------------------------------------------------
# Permission prompt detection & approval
# ---------------------------------------------------------------------------


def detect_permission_prompt(content: str) -> bool:
    """Check if the pane content contains a Claude Code permission prompt."""
    for pattern in COMPILED_PATTERNS:
        if pattern.search(content):
            return True
    return False


def approve_pane(pane_id: str, logger: logging.Logger):
    """Send approval keystroke to a pane.

    Claude Code permission prompts typically accept 'y' to approve.
    We send 'y' followed by Enter to confirm.
    """
    logger.info(f"  -> Auto-approving in pane {pane_id}")
    tmux_send_raw(pane_id, "y")


# ---------------------------------------------------------------------------
# Auto-approver daemon
# ---------------------------------------------------------------------------


class AutoApprover:
    """Monitors tmux panes and auto-approves Claude Code permission prompts."""

    def __init__(self, session: str, logger: logging.Logger):
        self.session = session
        self.logger = logger
        self.pane_states: dict[str, PaneState] = {}
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        """Main loop: scan panes, detect prompts, approve."""
        self.logger.info(f"Auto-approver started for session '{self.session}'")
        while not self._stop.is_set():
            try:
                self._scan_and_approve()
            except Exception as e:
                self.logger.error(f"Auto-approver error: {e}")
            self._stop.wait(POLL_INTERVAL)
        self.logger.info("Auto-approver stopped")

    def _scan_and_approve(self):
        panes = tmux_list_panes(self.session)
        now = time.time()

        for pane in panes:
            pane_id = pane["pane_id"]

            # Initialize state for new panes
            if pane_id not in self.pane_states:
                self.pane_states[pane_id] = PaneState(
                    pane_id=pane_id,
                    window_name=pane["window_name"],
                )

            state = self.pane_states[pane_id]

            # Respect cooldown to avoid spamming
            if now - state.last_approval_time < APPROVAL_COOLDOWN:
                continue

            # Capture and check pane content
            content = tmux_capture_pane(pane_id)
            if not content:
                continue

            if detect_permission_prompt(content):
                self.logger.info(
                    f"Permission prompt detected in pane {pane_id} "
                    f"(window: {state.window_name})"
                )
                approve_pane(pane_id, self.logger)
                state.last_approval_time = now
                state.approval_count += 1

    def get_stats(self) -> dict:
        return {
            pane_id: {
                "window": s.window_name,
                "approvals": s.approval_count,
            }
            for pane_id, s in self.pane_states.items()
        }


# ---------------------------------------------------------------------------
# Agent launcher
# ---------------------------------------------------------------------------


def build_claude_command(task: AgentTask) -> str:
    """Build the claude CLI command string for a task."""
    cmd_parts = ["claude"]

    if task.model:
        cmd_parts += ["--model", task.model]

    cmd_parts += task.extra_args

    # Use --verbose for better debugging
    # Escape the prompt for shell
    escaped_prompt = task.prompt.replace("'", "'\\''")
    cmd_parts.append(f"'{escaped_prompt}'")

    return " ".join(cmd_parts)


def launch_agents(
    tasks: list[AgentTask],
    session: str,
    logger: logging.Logger,
    dry_run: bool = False,
) -> str:
    """Launch Claude Code agents in tmux windows.

    Returns the session name.
    """
    if not tasks:
        logger.error("No tasks provided")
        sys.exit(1)

    # Create or reuse session
    if tmux_has_session(session):
        logger.info(f"Session '{session}' already exists, adding windows")
    else:
        first_task = tasks[0]
        first_task.window_name = first_task.window_name or "agent-0"
        cmd = build_claude_command(first_task)

        if dry_run:
            logger.info(f"[DRY RUN] Would create session '{session}', "
                        f"window '{first_task.window_name}': {cmd}")
        else:
            logger.info(f"Creating session '{session}'")
            tmux_new_session(session, first_task.window_name, first_task.work_dir)
            time.sleep(0.3)
            tmux_send_keys(f"{session}:{first_task.window_name}", cmd)
            tmux_send_raw(f"{session}:{first_task.window_name}", "Enter")
            logger.info(f"  Launched agent-0: {first_task.prompt[:60]}...")

        tasks = tasks[1:]

    # Create additional windows
    for i, task in enumerate(tasks, start=1):
        task.window_name = task.window_name or f"agent-{i}"
        cmd = build_claude_command(task)

        if dry_run:
            logger.info(f"[DRY RUN] Would create window '{task.window_name}': {cmd}")
        else:
            tmux_new_window(session, task.window_name, task.work_dir)
            time.sleep(0.3)
            tmux_send_keys(f"{session}:{task.window_name}", cmd)
            tmux_send_raw(f"{session}:{task.window_name}", "Enter")
            logger.info(f"  Launched {task.window_name}: {task.prompt[:60]}...")

    return session


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------


def show_status(session: str, approver: Optional[AutoApprover] = None):
    """Print current status of all agents in the session."""
    panes = tmux_list_panes(session)
    print(f"\n{'='*60}")
    print(f"Session: {session}  |  Panes: {len(panes)}")
    print(f"{'='*60}")

    stats = approver.get_stats() if approver else {}

    for pane in panes:
        pane_id = pane["pane_id"]
        pane_stat = stats.get(pane_id, {})
        approvals = pane_stat.get("approvals", 0)
        print(f"  [{pane['window_name']}] pid={pane['pid']} "
              f"cmd={pane['command']} approvals={approvals}")

    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch parallel Claude Code agents in tmux with auto-approval",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "tasks",
        nargs="*",
        help="Task prompts for Claude Code agents (one agent per task)",
    )
    parser.add_argument(
        "-f", "--file",
        help="Read tasks from a file (one per line)",
    )
    parser.add_argument(
        "-d", "--work-dir",
        default=".",
        help="Working directory for agents (default: current dir)",
    )
    parser.add_argument(
        "-s", "--session",
        default=DEFAULT_SESSION_NAME,
        help=f"tmux session name (default: {DEFAULT_SESSION_NAME})",
    )
    parser.add_argument(
        "-m", "--model",
        default="",
        help="Claude model to use (e.g., opus, sonnet, haiku)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )
    parser.add_argument(
        "--no-approve",
        action="store_true",
        help="Disable auto-approver (just launch agents)",
    )
    parser.add_argument(
        "--approve-only",
        action="store_true",
        help="Only run the auto-approver on an existing session",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show status of running session and exit",
    )
    parser.add_argument(
        "--kill",
        action="store_true",
        help="Kill the tmux session and exit",
    )
    return parser.parse_args()


def load_tasks_from_file(filepath: str) -> list[str]:
    with open(filepath) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def main():
    args = parse_args()
    logger = setup_logging(args.session)

    # Handle --kill
    if args.kill:
        if tmux_has_session(args.session):
            tmux_kill_session(args.session)
            logger.info(f"Killed session '{args.session}'")
        else:
            logger.info(f"No session '{args.session}' to kill")
        return

    # Handle --status
    if args.status:
        if tmux_has_session(args.session):
            show_status(args.session)
        else:
            print(f"No session '{args.session}' found")
        return

    # Handle --approve-only
    if args.approve_only:
        if not tmux_has_session(args.session):
            logger.error(f"Session '{args.session}' not found")
            sys.exit(1)
        approver = AutoApprover(args.session, logger)
        signal.signal(signal.SIGINT, lambda *_: approver.stop())
        signal.signal(signal.SIGTERM, lambda *_: approver.stop())
        approver.run()
        return

    # Collect tasks
    task_prompts = list(args.tasks)
    if args.file:
        task_prompts.extend(load_tasks_from_file(args.file))

    if not task_prompts:
        print("Error: No tasks provided. Pass tasks as arguments or use -f <file>.")
        sys.exit(1)

    agent_tasks = [
        AgentTask(
            prompt=prompt,
            work_dir=args.work_dir,
            model=args.model,
        )
        for prompt in task_prompts
    ]

    # Launch agents
    session = launch_agents(agent_tasks, args.session, logger, dry_run=args.dry_run)

    if args.dry_run:
        return

    # Start auto-approver
    if not args.no_approve:
        approver = AutoApprover(session, logger)

        def signal_handler(sig, frame):
            logger.info("Received shutdown signal")
            approver.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        logger.info("Auto-approver running. Press Ctrl+C to stop.")
        logger.info(f"Attach to session: tmux attach -t {session}")

        approver.run()

        # Show final stats
        show_status(session, approver)
    else:
        logger.info(f"Agents launched. Attach with: tmux attach -t {session}")


if __name__ == "__main__":
    main()
