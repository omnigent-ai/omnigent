"""A live composer another process drew over is repainted, not reaped."""

from __future__ import annotations

import contextlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.harnesses.claude_native import bridge, delivery_diagnostics

_RULE = "─" * 40
# Claude Code draws its composer glyph followed by a no-break space.
_READY_PANE = f"transcript\n{_RULE}\n❯ \n{_RULE}\n  status line\n"
# Another process's lines landed on both rules; Claude Code redrew only the
# glyph cell it changed, so the frame that marks the live box is gone.
_OVERDRAWN_PANE = (
    "transcript\n"
    "background refresh failed: certificate expired\n"
    "❯ see the troubleshooting steps\n"
    "1. renew the certificate, then send your message again\n"
    "  status line\n"
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_claude_pane_state", lambda *_: bridge._ClaudePaneState(True))
    return clock


def test_overdrawn_composer_is_not_a_ready_prompt() -> None:
    assert bridge._claude_prompt_rendered(_READY_PANE)
    assert not bridge._claude_prompt_rendered(_OVERDRAWN_PANE)


def test_prompt_wait_repaints_an_overdrawn_composer(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    repainted_at: list[float] = []

    def repaint(socket_path: str, tmux_target: str, bridge_dir: Path | None = None) -> int:
        repainted_at.append(clock.now)
        return 1

    monkeypatch.setattr(
        bridge, "_capture_pane", lambda *_: _READY_PANE if repainted_at else _OVERDRAWN_PANE
    )
    monkeypatch.setattr(bridge, "_request_claude_repaint", repaint)

    bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=30.0)

    assert len(repainted_at) == 1
    assert repainted_at[0] == pytest.approx(5.0, abs=0.2)


def test_prompt_wait_leaves_a_ready_composer_alone(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    repaints: list[str] = []
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _READY_PANE)
    monkeypatch.setattr(
        bridge, "_request_claude_repaint", lambda *args: repaints.append(args[1]) or 1
    )

    bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=30.0)

    assert repaints == []


def test_repaint_requests_repeat_until_the_wait_gives_up(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    repainted_at: list[float] = []
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 60.0)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _OVERDRAWN_PANE)
    monkeypatch.setattr(
        bridge, "_request_claude_repaint", lambda *_: repainted_at.append(clock.now) or 1
    )

    with pytest.raises(bridge.ClaudePromptTimeout, match="3 repaint requests"):
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=30.0)

    assert [round(at) for at in repainted_at] == [5, 25, 45]


def test_unanswered_repaint_is_not_counted(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[float] = []
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 30.0)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _OVERDRAWN_PANE)
    monkeypatch.setattr(
        bridge, "_request_claude_repaint", lambda *_: attempts.append(clock.now) or 0
    )

    with pytest.raises(bridge.ClaudePromptTimeout, match="0 repaint requests"):
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=30.0)

    assert len(attempts) == 2


def test_repaint_requests_reach_the_delivery_summary(
    clock: _Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repaints: list[float] = []
    monkeypatch.setattr(
        bridge, "_capture_pane", lambda *_: _READY_PANE if repaints else _OVERDRAWN_PANE
    )
    monkeypatch.setattr(bridge, "_request_claude_repaint", lambda *_: repaints.append(0.0) or 1)

    @delivery_diagnostics.trace_delivery(
        session_id_reader=lambda _: "owned-session",
        cancelled_error=bridge.ClaudeInjectionCancelled,
    )
    def deliver(bridge_dir: Path, *, content: str) -> None:
        bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "main", timeout_s=30.0)

    with caplog.at_level("INFO", logger=delivery_diagnostics.__name__):
        deliver(tmp_path, content="hello")
    [record] = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "claude_native_delivery_finished"
    ]
    attributes = record_to_row(record, source="runner")["attributes"]
    assert attributes["prompt_repaint_requests"] == "1"


def test_repaint_request_without_a_pane_signals_nothing(tmp_path: Path) -> None:
    assert bridge._request_claude_repaint(str(tmp_path / "missing.sock"), "main") == 0


# Redraws its whole frame at start and on SIGCONT, as Claude Code's resume
# handler does, and never otherwise: a differential renderer has no reason to.
_REPAINTING_TUI = r"""
import signal
import sys
import time
from pathlib import Path

marker = Path(sys.argv[1])
rule = "─" * 40
pending = True


def resume(signum, frame):
    global pending
    pending = True


signal.signal(signal.SIGCONT, resume)
while True:
    if pending:
        pending = False
        with marker.open("a") as draws:
            draws.write("drawn\n")
        sys.stdout.write(
            "\x1b[H\x1b[2Jtranscript\r\n" + rule + "\r\n❯ \r\n" + rule + "\r\n"
            "  status line\r\n"
        )
        sys.stdout.flush()
    time.sleep(0.02)
"""


def _wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 5.0
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.02)


@pytest.fixture
def overdrawn_terminal(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    script = tmp_path / "tui.py"
    script.write_text(_REPAINTING_TUI)
    marker = tmp_path / "draws"
    with tempfile.TemporaryDirectory(prefix="claude-overdraw-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "tmux.sock")
        tmux = ["tmux", "-S", socket_path]
        # The shell stays the pane's process, so the TUI runs as its child the
        # way a launcher runs Claude Code.
        inner = f"{shlex.join([sys.executable, str(script), str(marker)])}; exit"
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
                    "-x",
                    "80",
                    "-y",
                    "24",
                    shlex.join(["sh", "-c", inner]),
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            _wait_until(
                lambda: bridge._claude_prompt_rendered(bridge._capture_pane(socket_path, "main")),
                "test TUI never drew its input box",
            )
            pane_tty = subprocess.run(
                [*tmux, "display-message", "-p", "-t", "main", "#{pane_tty}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            # Another process sharing the terminal writes over the input box's
            # frame, leaving the glyph the TUI last drew.
            with open(pane_tty, "w") as terminal:
                terminal.write(
                    "\x1b[2;1Hbackground refresh failed: certificate expired\r\n"
                    "\x1b[3;3Hsee the troubleshooting steps\r\n"
                    "1. renew the certificate, then send your message again\r\n"
                )
            _wait_until(
                lambda: (
                    not bridge._claude_prompt_rendered(bridge._capture_pane(socket_path, "main"))
                ),
                "foreign output did not reach the pane",
            )
            yield socket_path, marker
        finally:
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.timeout(30)
def test_real_overdrawn_composer_is_repainted_before_the_wait_gives_up(
    overdrawn_terminal: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path, marker = overdrawn_terminal
    monkeypatch.setattr(bridge, "_CLAUDE_REPAINT_AFTER_S", 0.3)
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 8.0)

    bridge._wait_for_claude_prompt_ready(socket_path, "main", timeout_s=1.0)

    assert marker.read_text().splitlines() == ["drawn", "drawn"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.timeout(30)
def test_repaint_reaches_a_claude_outside_the_pane_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The repaint reaches a Claude Code that a launcher runs outside the pane's tree.

    isaac's fast launch leaves only a relay in the pane while Claude Code runs on a
    pre-started worker's terminal; its session registry still names the process.
    """
    script = tmp_path / "tui.py"
    script.write_text(_REPAINTING_TUI)
    marker = tmp_path / "draws"
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "state.json").write_text('{"claude_session_id": "sid-1"}')
    with tempfile.TemporaryDirectory(prefix="claude-relay-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "tmux.sock")
        tmux = ["tmux", "-S", socket_path]
        tui: subprocess.Popen[bytes] | None = None
        try:
            subprocess.run(
                [*tmux, "-f", "/dev/null", "new-session", "-d", "-s", "main", "sleep 600"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            pane_tty = subprocess.run(
                [*tmux, "display-message", "-p", "-t", "main", "#{pane_tty}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            with open(pane_tty, "wb") as terminal:
                tui = subprocess.Popen(
                    [sys.executable, str(script), str(marker)],
                    stdin=subprocess.DEVNULL,
                    stdout=terminal,
                )
            (config_dir / "sessions").mkdir(parents=True)
            (config_dir / "sessions" / f"{tui.pid}.json").write_text(
                '{"sessionId": "sid-1", "kind": "interactive"}'
            )
            _wait_until(lambda: marker.exists(), "test TUI never drew")

            # The pane's own tree is only the idle relay stand-in.
            assert bridge._request_claude_repaint(socket_path, "main") == 1
            assert bridge._request_claude_repaint(socket_path, "main", bridge_dir) == 2
            _wait_until(
                lambda: marker.read_text().splitlines() == ["drawn", "drawn"],
                "the registry-named process was not repainted",
            )
        finally:
            if tui is not None:
                tui.kill()
                tui.wait(timeout=5)
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)
