"""Tests for the host-side worktree lifecycle hook runner.

Exercises ``omnigent.host.git_worktree.run_worktree_hook`` against real
subprocesses in a temp directory: exit codes, timeout kill, output capture and
truncation, the exported env vars, and the platform shell selection. These are
the commands a project configures in its settings, so a regression in argv
construction or the timeout kill fails loud here.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from omnigent.host.git_worktree import (
    DEFAULT_HOOK_TIMEOUT_S,
    HOOK_OUTPUT_TAIL_BYTES,
    MAX_HOOK_TIMEOUT_S,
    MIN_HOOK_TIMEOUT_S,
    WorktreeError,
    _hook_script_argv,
    clamp_hook_timeout,
    run_worktree_hook,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shell fixtures below are POSIX (`sh -c`)",
)


def _process_is_dead(pid: int, timeout_s: float = 3.0) -> bool:
    """Wait for a pid to be gone (or an un-reaped zombie).

    A SIGKILL'd process lingers as a zombie until someone ``wait()``s it, and
    whether that happens depends on who inherited it (pytest may be running as
    a child subreaper). Either state means the kill landed, which is what the
    caller cares about.

    :param pid: The process id to poll.
    :param timeout_s: How long to wait before giving up.
    :returns: ``True`` once the process is gone or a zombie.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True
        # `stat` field 3 is the state code; `Z` is an un-reaped zombie.
        if stat.rpartition(")")[2].split()[0] == "Z":
            return True
        time.sleep(0.05)
    return False


def test_hook_success_reports_exit_zero_and_output(tmp_path: Path) -> None:
    """A succeeding command reports exit 0 with its combined output."""
    result = run_worktree_hook(
        command="echo hello; echo oops >&2",
        worktree_path=str(tmp_path),
        hook="post_create",
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hello" in result.output_tail
    # stderr is merged into the same capture, so a failing install's error is
    # never lost just because it went to fd 2.
    assert "oops" in result.output_tail


def test_hook_failure_reports_exit_code(tmp_path: Path) -> None:
    """A non-zero exit is reported, not raised — both hooks are fail-open."""
    result = run_worktree_hook(
        command="exit 7",
        worktree_path=str(tmp_path),
        hook="pre_delete",
    )
    assert result.exit_code == 7
    assert result.timed_out is False


def test_hook_runs_in_the_worktree_directory(tmp_path: Path) -> None:
    """The command's cwd is the worktree, so relative paths mean what they say."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result = run_worktree_hook(
        command="touch marker",
        worktree_path=str(worktree),
        hook="post_create",
    )
    assert result.exit_code == 0
    assert (worktree / "marker").exists()


def test_hook_exports_context_env_vars(tmp_path: Path) -> None:
    """The documented ``OMNIGENT_*`` context vars reach the command."""
    result = run_worktree_hook(
        command=(
            "echo $OMNIGENT_HOOK $OMNIGENT_BRANCH $OMNIGENT_BASE_BRANCH; "
            "echo $OMNIGENT_WORKTREE_PATH; echo $OMNIGENT_REPO_PATH"
        ),
        worktree_path=str(tmp_path),
        repo_path="/repos/myrepo",
        branch="feature/login",
        base_branch="main",
        hook="post_create",
    )
    assert result.exit_code == 0
    assert "post_create feature/login main" in result.output_tail
    assert str(tmp_path) in result.output_tail
    assert "/repos/myrepo" in result.output_tail


def test_hook_inherits_the_daemon_environment(tmp_path: Path, monkeypatch) -> None:
    """The host daemon's own env is passed through (PATH, tool config, …)."""
    monkeypatch.setenv("OMNI_TEST_INHERITED", "yes")
    result = run_worktree_hook(
        command="echo $OMNI_TEST_INHERITED",
        worktree_path=str(tmp_path),
        hook="post_create",
    )
    assert "yes" in result.output_tail


def test_hook_timeout_kills_the_process_group(tmp_path: Path) -> None:
    """A hook past its timeout is killed and reported as timed out.

    The command backgrounds a long sleep and exits its own shell, so only a
    process-GROUP kill reaps the grandchild — a bare ``proc.kill()`` would
    leave it running past the test.
    """
    pidfile = tmp_path / "child.pid"
    result = run_worktree_hook(
        # `sh -c` exec's a single trailing command, so keep the sleep in the
        # foreground of a subshell whose pid we record.
        command=f"sh -c 'echo $$ > {pidfile}; sleep 30' & wait",
        worktree_path=str(tmp_path),
        hook="post_create",
        timeout_seconds=1,
    )
    assert result.timed_out is True
    assert result.exit_code is None
    child_pid = int(pidfile.read_text().strip())
    assert _process_is_dead(child_pid), (
        f"pid {child_pid} survived the hook timeout; only the shell was killed"
    )


def test_multi_line_script_runs_every_line(tmp_path: Path) -> None:
    """A multi-line script runs as a program, not just its first line."""
    result = run_worktree_hook(
        command="echo one\necho two\ntouch marker\necho three",
        worktree_path=str(tmp_path),
        hook="post_create",
    )
    assert result.exit_code == 0
    assert "one" in result.output_tail
    assert "two" in result.output_tail
    assert "three" in result.output_tail
    assert (tmp_path / "marker").exists()


def test_script_with_a_bash_shebang_gets_bash(tmp_path: Path) -> None:
    """A ``#!`` line selects the interpreter, so bash-only syntax works.

    ``[[ ... ]]`` and arrays are not POSIX ``sh``; on a dash-is-sh host
    this only passes because the shebang is honored.
    """
    if shutil.which("bash") is None:  # pragma: no cover — bash is present in CI
        pytest.skip("bash is not installed")
    result = run_worktree_hook(
        command=(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "arr=(a b c)\n"
            'if [[ "${#arr[@]}" == 3 ]]; then echo BASH_OK; fi\n'
        ),
        worktree_path=str(tmp_path),
        hook="post_create",
    )
    assert result.exit_code == 0
    assert "BASH_OK" in result.output_tail


def test_script_reports_the_failing_line_exit_code(tmp_path: Path) -> None:
    """``set -e`` in a script surfaces as that line's non-zero exit."""
    result = run_worktree_hook(
        command="#!/bin/sh\nset -e\necho starting\nexit 3\necho unreachable\n",
        worktree_path=str(tmp_path),
        hook="post_create",
    )
    assert result.exit_code == 3
    assert "starting" in result.output_tail
    assert "unreachable" not in result.output_tail


def test_script_file_is_cleaned_up_and_not_left_in_the_worktree(tmp_path: Path) -> None:
    """The temp script is removed, and never written into the worktree.

    A script file inside the worktree would show up as an untracked file
    in the user's repo — and in the agent's very first `git status`.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result = run_worktree_hook(
        command="echo $0 > /dev/null; echo done",
        worktree_path=str(worktree),
        hook="post_create",
    )
    assert result.exit_code == 0
    assert list(worktree.iterdir()) == []
    # No omnigent-hook-* temp dirs survive the run.
    assert not list(Path(tempfile.gettempdir()).glob("omnigent-hook-*"))


def test_hook_output_is_truncated_to_the_tail(tmp_path: Path) -> None:
    """Only the last 10 KB is kept, and it's the END of the output."""
    # 200 KB of 'a' lines, then a unique marker last.
    result = run_worktree_hook(
        command="head -c 204800 /dev/zero | tr '\\0' 'a'; echo; echo LAST_LINE_MARKER",
        worktree_path=str(tmp_path),
        hook="post_create",
    )
    assert result.exit_code == 0
    assert len(result.output_tail.encode()) <= HOOK_OUTPUT_TAIL_BYTES
    assert "LAST_LINE_MARKER" in result.output_tail


def test_hook_rejects_a_blank_command(tmp_path: Path) -> None:
    """A blank command is a caller bug (blank config means "unset")."""
    with pytest.raises(WorktreeError):
        run_worktree_hook(command="   ", worktree_path=str(tmp_path), hook="post_create")


def test_hook_rejects_a_missing_worktree(tmp_path: Path) -> None:
    """A worktree that's already gone fails loud instead of running elsewhere."""
    with pytest.raises(WorktreeError):
        run_worktree_hook(
            command="true",
            worktree_path=str(tmp_path / "nope"),
            hook="pre_delete",
        )


def test_hook_script_argv_selects_the_platform_interpreter(tmp_path: Path, monkeypatch) -> None:
    """POSIX feeds the script to ``sh``; Windows runs it via ``COMSPEC /c``."""
    monkeypatch.setattr(sys, "platform", "linux")
    argv = _hook_script_argv("bun install", tmp_path)
    assert argv == ["/bin/sh", str(tmp_path / "omnigent-hook")]
    # The script itself lands on disk, newline-terminated. Read BYTES: a text
    # read would normalize the line endings this test is about.
    assert (tmp_path / "omnigent-hook").read_bytes() == b"bun install\n"

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\cmd.exe")
    win_argv = _hook_script_argv("bun install\nbun test", tmp_path)
    assert win_argv == [r"C:\Windows\cmd.exe", "/c", str(tmp_path / "omnigent-hook.cmd")]
    # cmd.exe is CRLF-only — a bare \n batch file misparses.
    body = (tmp_path / "omnigent-hook.cmd").read_bytes()
    assert body == b"@echo off\r\nbun install\r\nbun test\r\n"


def test_hook_script_argv_honors_a_shebang(tmp_path: Path, monkeypatch) -> None:
    """A shebang makes the script executable and exec'd directly.

    Under ``sh -c`` (or ``sh <file>``) a ``#!`` line is only a comment, so
    a bash-only script would break on a dash-is-sh host. Exec'ing the file
    is what hands it to the interpreter the author asked for.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    script = "#!/usr/bin/env bash\necho hi\n"
    argv = _hook_script_argv(script, tmp_path)
    path = tmp_path / "omnigent-hook"
    assert argv == [str(path)]
    assert path.read_text() == script
    assert path.stat().st_mode & 0o100, "shebang script must be executable"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_HOOK_TIMEOUT_S),
        (0, DEFAULT_HOOK_TIMEOUT_S),
        (-5, DEFAULT_HOOK_TIMEOUT_S),
        ("nonsense", DEFAULT_HOOK_TIMEOUT_S),
        (0.5, MIN_HOOK_TIMEOUT_S),
        (600, 600.0),
        (10_000, MAX_HOOK_TIMEOUT_S),
    ],
)
def test_clamp_hook_timeout(raw: object, expected: float) -> None:
    """Configured timeouts clamp into the supported range."""
    assert clamp_hook_timeout(raw) == expected  # type: ignore[arg-type]
