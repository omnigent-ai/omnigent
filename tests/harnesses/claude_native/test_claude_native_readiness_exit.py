"""Readiness waits notice exited processes even with a full startup budget."""

from __future__ import annotations

import contextlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("timeout_s", [30.0, 180.0, 240.0])
def test_dead_pane_is_checked_before_the_startup_budget(
    monkeypatch: pytest.MonkeyPatch, timeout_s: float
) -> None:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: "Synthetic startup failure")
    monkeypatch.setattr(
        bridge,
        "_claude_pane_state",
        lambda *_: bridge._ClaudePaneState(False, exited=True, exit_status="1"),
    )

    with pytest.raises(bridge.ClaudeTerminalExited, match=r"exited \(status 1\)"):
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=timeout_s)

    assert clock.now <= bridge._CLAUDE_LIVENESS_POLL_INTERVAL_S + 0.15


@pytest.mark.parametrize("timeout_s", [30.0, 180.0])
def test_vanished_pane_is_reported_as_exited_not_as_unready(
    monkeypatch: pytest.MonkeyPatch, timeout_s: float
) -> None:
    """A pane tmux stops reporting ends the wait as an exit, not a readiness timeout.

    The terminal is gone (its tmux server or target vanished, so no exit status
    is known). Waiting out the startup budget and then saying the prompt never
    rendered sends the person to debug a box that never existed; the exit is
    what they need to hear, and a relaunch is the fix.
    """
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: "")
    monkeypatch.setattr(bridge, "_claude_pane_state", lambda *_: bridge._ClaudePaneState(False))

    with pytest.raises(bridge.ClaudeTerminalExited) as raised:
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=timeout_s)

    assert raised.value.exit_status is None
    assert "is no longer running (exit status unknown)" in str(raised.value)
    assert "did not become ready" not in str(raised.value)
    assert clock.now <= bridge._CLAUDE_LIVENESS_POLL_INTERVAL_S + 0.15


def test_dead_pane_at_deadline_retains_exit_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: "Synthetic startup failure")
    monkeypatch.setattr(
        bridge,
        "_claude_pane_state",
        lambda *_: bridge._ClaudePaneState(False, exited=True, exit_status="137"),
    )

    with pytest.raises(bridge.ClaudeTerminalExited) as raised:
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=0.3)

    assert raised.value.exit_status == "137"
    assert "Synthetic startup failure" in str(raised.value)
    assert clock.now == pytest.approx(0.3)


@pytest.fixture
def dead_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    gate = tmp_path / "exit-gate"
    terminal = tmp_path / "terminal.py"
    terminal.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(0.01)\n"
        "print('Synthetic startup failure\\n', flush=True)\n"
        "raise SystemExit(1)\n"
    )
    with tempfile.TemporaryDirectory(prefix="claude-exit-", dir="/tmp") as directory:
        socket_path = Path(directory) / "tmux.sock"
        tmux = ["tmux", "-S", str(socket_path)]
        try:
            subprocess.run(
                [
                    *tmux,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-s",
                    "main",
                    shlex.join([sys.executable, str(terminal), str(gate)]),
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                [*tmux, "set-option", "-t", "main", "remain-on-exit", "on"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            gate.touch()
            deadline = time.monotonic() + 5
            while not bridge._claude_pane_state(str(socket_path), "main").exited:
                assert time.monotonic() < deadline, "Test terminal did not exit"
                time.sleep(0.02)
            bridge_dir = tmp_path / "bridge"
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
            yield bridge_dir
        finally:
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize("timeout_s", [0.4, 3.0])
@pytest.mark.timeout(15)
def test_message_to_real_dead_terminal_reports_exit(
    dead_terminal: Path, monkeypatch: pytest.MonkeyPatch, timeout_s: float
) -> None:
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", timeout_s)
    started = time.monotonic()

    with pytest.raises(bridge.ClaudeTerminalExited) as raised:
        bridge.inject_user_message(dead_terminal, content="MUST_NOT_DELIVER", timeout_s=timeout_s)

    assert time.monotonic() - started < 2.5
    assert raised.value.exit_status in {None, "1"}
    assert "Last terminal output:" in str(raised.value)
    assert any(
        marker in str(raised.value) for marker in ("Synthetic startup failure", "Pane is dead")
    )
    assert "MUST_NOT_DELIVER" not in str(raised.value)


_BUSY_PANE = "● Bash(python3 -c 'import time; time.sleep(60)')\n  ✻ Working…  (esc to interrupt)"
_IDLE_PANE = "────────\n❯ \n────────\n  ? for shortcuts"


def test_interrupt_retries_escape_until_busy_footer_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(
        bridge, "_wait_for_tmux_info", lambda *_, **__: {"socket_path": "s", "tmux_target": "main"}
    )
    sent: list[str] = []
    monkeypatch.setattr(bridge, "_run_tmux", lambda _socket, *args, **__: sent.append(args[-1]))
    # The first Escape's confirm window (about 8 polls) still shows the turn; the second stops it.
    panes = iter([_BUSY_PANE] * 12 + [_IDLE_PANE])
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_, **__: next(panes))

    assert bridge.inject_interrupt(Path("/unused"), timeout_s=1.0) is True
    assert sent == ["Escape", "Escape"]


def test_interrupt_reports_unconfirmed_when_turn_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(
        bridge, "_wait_for_tmux_info", lambda *_, **__: {"socket_path": "s", "tmux_target": "main"}
    )
    sent: list[str] = []
    monkeypatch.setattr(bridge, "_run_tmux", lambda _socket, *args, **__: sent.append(args[-1]))
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_, **__: _BUSY_PANE)

    assert bridge.inject_interrupt(Path("/unused"), timeout_s=1.0) is False
    # Bounded: one Escape per attempt, never an unbounded retry loop.
    assert sent == ["Escape"] * bridge._INTERRUPT_ESCAPE_ATTEMPTS


def test_interrupt_treats_empty_capture_as_unknown_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(
        bridge, "_wait_for_tmux_info", lambda *_, **__: {"socket_path": "s", "tmux_target": "main"}
    )
    monkeypatch.setattr(bridge, "_run_tmux", lambda *_, **__: None)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_, **__: "")

    assert bridge.inject_interrupt(Path("/unused"), timeout_s=1.0) is False


def test_readiness_timeout_while_turn_runs_reports_busy_not_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _BUSY_PANE)
    monkeypatch.setattr(bridge, "_claude_pane_state", lambda *_: bridge._ClaudePaneState(True))

    with pytest.raises(bridge.ClaudeTerminalBusy) as raised:
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=30.0)

    assert not isinstance(raised.value, bridge.ClaudePromptTimeout)
    assert "did not become ready" not in str(raised.value)
    assert "still working" in str(raised.value)


def test_readiness_timeout_on_idle_pane_stays_unready(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: "Synthetic boot crash")
    monkeypatch.setattr(bridge, "_claude_pane_state", lambda *_: bridge._ClaudePaneState(True))

    with pytest.raises(bridge.ClaudePromptTimeout) as raised:
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=30.0)

    assert not isinstance(raised.value, bridge.ClaudeTerminalBusy)
