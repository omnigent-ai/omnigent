"""Native pane identity capture and conditional cleanup against a stubbed tmux."""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native.tmux_terminal import (
    NativeTmuxTerminal,
    capture_tmux_terminal,
    stop_tmux_terminal,
)

_IDENTITY = "4242 1700000000 $3 %7 4343"
_TERMINAL = NativeTmuxTerminal("/tmp/t.sock", 4242, 1700000000, "$3", "%7", 4343)


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_capture_tmux_terminal_pins_the_resolved_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _completed(_IDENTITY + "\n")

    monkeypatch.setattr(subprocess, "run", run)

    assert capture_tmux_terminal("/tmp/t.sock", "main") == _TERMINAL
    (command,) = commands
    assert command[:7] == ["tmux", "-S", "/tmp/t.sock", "display-message", "-p", "-t", "main"]


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        pytest.param("", 0, id="unresolvable_target_prints_nothing"),
        pytest.param("4242 1700000000 $3 %7", 0, id="missing_field"),
        pytest.param("pid 1700000000 $3 %7 4343", 0, id="non_numeric_pid"),
        pytest.param("4242 1700000000 3 %7 4343", 0, id="session_id_without_prefix"),
        pytest.param("4242 1700000000 $3 7 4343", 0, id="pane_id_without_prefix"),
        pytest.param("0 1700000000 $3 %7 4343", 0, id="zero_pid"),
        pytest.param(_IDENTITY, 1, id="rejected_query"),
    ],
)
def test_capture_tmux_terminal_rejects_unusable_answers(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _completed(stdout, returncode))

    assert capture_tmux_terminal("/tmp/t.sock", "main") is None


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(cmd="tmux", timeout=10), OSError("tmux missing")],
    ids=["unanswered", "not_runnable"],
)
def test_capture_tmux_terminal_returns_none_when_tmux_does_not_answer(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(subprocess, "run", run)

    assert capture_tmux_terminal("/tmp/t.sock", "main") is None


def test_stop_tmux_terminal_checks_every_identity_field_in_the_kill_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    budgets: list[float] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        budgets.append(kwargs["timeout"])
        return _completed()

    monkeypatch.setattr(subprocess, "run", run)

    stop_tmux_terminal(_TERMINAL)

    (command,) = commands
    assert budgets == [10.0]
    assert command[:7] == ["tmux", "-S", "/tmp/t.sock", "if-shell", "-F", "-t", "%7"]
    condition, action = command[7:]
    assert action == "kill-pane -t %7"
    for comparison in (
        "#{==:#{pid},4242}",
        "#{==:#{start_time},1700000000}",
        "#{==:#{session_id},$3}",
        "#{==:#{pane_id},%7}",
        "#{==:#{pane_pid},4343}",
    ):
        assert comparison in condition


@pytest.mark.parametrize(
    "stderr",
    [
        "can't find pane: %7",
        "no server running on /tmp/t.sock",
        "error connecting to /tmp/t.sock (No such file or directory)",
        "error connecting to /tmp/t.sock (Datei oder Verzeichnis nicht gefunden)",
    ],
    ids=["pane_gone", "server_gone", "socket_gone", "socket_gone_localized"],
)
def test_stop_tmux_terminal_tolerates_a_pane_that_is_already_gone(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: _completed(returncode=1, stderr=stderr)
    )

    stop_tmux_terminal(_TERMINAL)


@pytest.mark.parametrize(
    "stderr",
    ["server exited unexpectedly", "open: No such file or directory"],
    ids=["unexpected_message", "bare_strerror_without_connect_prefix"],
)
def test_stop_tmux_terminal_reports_an_unexpected_tmux_failure(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: _completed(returncode=1, stderr=stderr)
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        stop_tmux_terminal(_TERMINAL)


def test_stop_tmux_terminal_reports_an_unanswered_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=1)

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="could not complete"):
        stop_tmux_terminal(_TERMINAL)


@pytest.mark.parametrize(
    "terminal",
    [
        replace(_TERMINAL, pane_pid=0),
        replace(_TERMINAL, session_id="3"),
        replace(_TERMINAL, pane_id="7"),
    ],
    ids=["zero_pane_pid", "session_id_without_prefix", "pane_id_without_prefix"],
)
def test_stop_tmux_terminal_refuses_an_invalid_identity(
    monkeypatch: pytest.MonkeyPatch, terminal: NativeTmuxTerminal
) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("tmux must not run for an identity that cannot be matched")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="Invalid native terminal identity"):
        stop_tmux_terminal(terminal)


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "unpinned"])
def test_capture_injection_terminal_warns_only_when_identity_cannot_be_pinned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pinned: bool
) -> None:
    monkeypatch.setattr(
        claude_bridge, "capture_tmux_terminal", lambda *_a, **_k: _TERMINAL if pinned else None
    )

    with (
        caplog.at_level(logging.WARNING, logger="omnigent.harnesses.claude_native.bridge"),
        claude_bridge.cancellable_injection(threading.Event()) as attempt,
    ):
        claude_bridge._capture_injection_terminal("/tmp/t.sock", "main")

    assert attempt.terminal == (_TERMINAL if pinned else None)
    warnings = [r for r in caplog.records if "could not pin the identity" in r.getMessage()]
    assert len(warnings) == (0 if pinned else 1)


@pytest.mark.parametrize("injector", ["message", "slash_command"])
def test_injectors_pin_the_terminal_before_touching_its_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, injector: str
) -> None:
    """Both delivery paths pin the pane right after tmux.json resolves, before any keystroke."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    steps: list[str] = []

    def capture(socket_path: str, target: str, **kwargs: Any) -> NativeTmuxTerminal:
        steps.append(f"capture {socket_path} {target} timeout={kwargs.get('timeout_s')}")
        return _TERMINAL

    def restore(*_args: Any, **_kwargs: Any) -> str:
        steps.append("restore input")
        return "a dialog"

    monkeypatch.setattr(
        claude_bridge,
        "_wait_for_tmux_info",
        lambda *_a, **_k: {"socket_path": "/tmp/t.sock", "tmux_target": "main"},
    )
    monkeypatch.setattr(claude_bridge, "capture_tmux_terminal", capture)
    monkeypatch.setattr(claude_bridge, "_restore_occupied_input", restore)
    monkeypatch.setattr(
        claude_bridge, "_run_tmux", lambda *_a, **_k: pytest.fail("no keystroke expected")
    )

    with (
        claude_bridge.cancellable_injection(threading.Event()) as attempt,
        pytest.raises(claude_bridge.ClaudeTerminalDialog),
    ):
        if injector == "message":
            claude_bridge.inject_user_message(bridge_dir, content="hello", timeout_s=0.1)
        else:
            claude_bridge.inject_slash_command(bridge_dir, command="/model sonnet", timeout_s=0.1)

    assert attempt.terminal == _TERMINAL
    assert steps == [
        f"capture /tmp/t.sock main timeout={claude_bridge._TMUX_SEND_TIMEOUT_S}",
        "restore input",
    ]
