"""Exercise slow-start delivery through a real tmux pane without a Claude account."""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.harnesses.claude_native import bridge

_TERMINAL = r"""
import json
import os
import re
import signal
import sys
import time
import tty
from pathlib import Path

sys.path.insert(0, sys.argv[4])
from tests.harnesses.claude_native.test_claude_native_shell_mode_draft import _InputBox

gate_started, delivered = map(Path, sys.argv[1:3])
startup = sys.argv[3]
tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?2004h")
sys.stdout.flush()
while not gate_started.exists():
    time.sleep(0.01)
if startup in {"dead", "dead-no-status"}:
    gate_started.with_suffix(".exited").touch()
    print("Startup failed", flush=True)
    raise SystemExit(1)
print("Starting Claude Code...", flush=True)
time.sleep(1.0)
draft = "\n".join(f"OLD {index}" for index in range(8)) if startup.startswith("stale-") else ""
box = _InputBox("bash" if startup == "stale-shell-frame" else "prompt", draft)


def render(*_args):
    screen = box.pane()
    if startup.startswith("stale-"):
        screen = "─" * 30 + "\n❯ previous message\n" + screen
        screen = "\n".join(screen.splitlines()[:os.get_terminal_size().lines])
    sys.stdout.write("\x1b[H\x1b[2J" + screen.replace("\n", "\r\n"))
    sys.stdout.flush()


signal.signal(signal.SIGWINCH, render)
render()
pending = b""
while True:
    pending += os.read(sys.stdin.fileno(), 65536)
    while pending:
        if pending.startswith(b"\x1b[200~"):
            if b"\x1b[201~" not in pending:
                break
            pasted, pending = pending[6:].split(b"\x1b[201~", 1)
            text = pasted.decode().replace("\r", "\n")
            box.draft = box.draft[:box.cursor] + text + box.draft[box.cursor:]
            box.cursor += len(text)
        else:
            match = re.match(rb"\x1b\[\d+;5u", pending)
            if match:
                token = match[0].decode()
                pending = pending[len(match[0]):]
            elif pending.startswith(b"\x1b["):
                break
            else:
                token = chr(pending[0])
                pending = pending[1:]
            if token == "\r":
                box.key("Enter")
                delivered.write_text(json.dumps(box.submitted))
            elif token == "\x15" or token.startswith("\x1b["):
                box.key("C-u" if token == "\x15" else token)
            elif token.isprintable():
                box.draft = box.draft[:box.cursor] + token + box.draft[box.cursor:]
                box.cursor += len(token)
        render()
"""


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize(
    "startup",
    [
        "slow",
        "dead",
        "dead-no-status",
        "base-budget-only",
        "exit-after-ready",
        "stale-frame",
        "stale-shell-frame",
    ],
)
@pytest.mark.timeout(20)
def test_first_message_survives_slow_terminal_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Hold startup until the real readiness gate starts, then exceed its base budget."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(
        bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 0.3 if startup == "base-budget-only" else 5.0
    )
    gate_started = tmp_path / "gate-started"
    delivered = tmp_path / "delivered.json"
    terminal = tmp_path / "terminal.py"
    terminal.write_text(_TERMINAL)
    original_wait = bridge._wait_for_claude_prompt_ready
    started_at: list[float] = []

    if startup == "dead-no-status":
        original_pane_state = bridge._claude_pane_state

        def pane_state_without_exit_status(
            socket_path: str, tmux_target: str
        ) -> bridge._ClaudePaneState:
            return original_pane_state(socket_path, tmux_target)._replace(exit_status=None)

        monkeypatch.setattr(bridge, "_claude_pane_state", pane_state_without_exit_status)

    def observe_wait(
        socket_path: str,
        tmux_target: str,
        *,
        timeout_s: float,
        bridge_dir: Path | None = None,
    ) -> None:
        started_at.append(time.monotonic())
        gate_started.touch()
        original_wait(socket_path, tmux_target, timeout_s=timeout_s, bridge_dir=bridge_dir)
        if startup == "exit-after-ready":
            subprocess.run(
                ["tmux", "-S", socket_path, "kill-server"],
                check=True,
                capture_output=True,
                timeout=5,
            )

    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", observe_wait)
    with tempfile.TemporaryDirectory(prefix="claude-ready-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "tmux.sock"
        tmux = ["tmux", "-S", str(socket_path)]
        command = shlex.join(
            [
                sys.executable,
                str(terminal),
                str(gate_started),
                str(delivered),
                startup,
                str(Path(__file__).resolve().parents[3]),
            ]
        )
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
                    "120",
                    "-y",
                    "6" if startup.startswith("stale-") else "24",
                    command,
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                [*tmux, "set-option", "remain-on-exit", "on"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            if startup == "stale-shell-frame":
                gate_started.touch()
                deadline = time.monotonic() + 5.0
                while "! OLD 0" not in bridge._capture_pane(str(socket_path), "main"):
                    assert time.monotonic() < deadline, "The shell draft never rendered."
                    time.sleep(0.05)
            bridge_dir = tmp_path / "bridge"
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
            if startup == "slow":
                bridge.inject_user_message(
                    bridge_dir, content="FIRST_PROMPT_DELIVERED", timeout_s=0.3
                )
                assert time.monotonic() - started_at[0] >= 1.0
                assert json.loads(delivered.read_text()) == [["prompt", "FIRST_PROMPT_DELIVERED"]]
                assert bridge._claude_pane_state(str(socket_path), "main").alive
            elif startup.startswith("stale-"):
                bridge.inject_user_message(bridge_dir, content="ONLY_NEW_INPUT", timeout_s=2.0)
                assert json.loads(delivered.read_text()) == [["prompt", "ONLY_NEW_INPUT"]]
                bridge.inject_slash_command(bridge_dir, command="/effort high", timeout_s=2.0)
                assert json.loads(delivered.read_text()) == [
                    ["prompt", "ONLY_NEW_INPUT"],
                    ["prompt", "/effort high"],
                ]
                geometry = subprocess.run(
                    [*tmux, "display-message", "-p", "-t", "main", "#{window_height}"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                assert geometry.stdout.strip() == "6"
            elif startup == "exit-after-ready":
                caplog.set_level(logging.WARNING)
                with pytest.raises(RuntimeError, match="tmux command failed"):
                    bridge.inject_user_message(
                        bridge_dir, content="MUST_NOT_DELIVER", timeout_s=0.3
                    )
                record = next(
                    r
                    for r in caplog.records
                    if getattr(r, "event_name", None) == "claude_native_delivery_finished"
                )
                row = record_to_row(record, source="runner")
                assert row["attributes"]["tmux_operation"] == "send-keys"
                assert row["attributes"]["tmux_returncode"] == "1"
                assert "MUST_NOT_DELIVER" not in json.dumps(row)
                assert str(socket_path) not in json.dumps(row)
                assert not delivered.exists()
            elif startup in {"dead", "dead-no-status"}:
                with pytest.raises(bridge.ClaudeTerminalExited) as excinfo:
                    bridge.inject_user_message(
                        bridge_dir, content="MUST_NOT_DELIVER", timeout_s=0.3
                    )
                assert gate_started.with_suffix(".exited").exists()
                assert not delivered.exists()
                assert excinfo.value.exit_status in {None, "1"}
                if startup == "dead-no-status":
                    assert excinfo.value.exit_status is None
                expected_diagnostic = (
                    "is no longer running (exit status unknown)"
                    if excinfo.value.exit_status is None
                    else "has exited (status 1)"
                )
                assert expected_diagnostic in str(excinfo.value)
                pane_state = bridge._claude_pane_state(str(socket_path), "main")
                assert pane_state.alive is False
                assert pane_state.exited is True
                # Older tmux builds leave pane_dead_status empty.
                assert pane_state.exit_status in {None, "1"}
            else:
                with pytest.raises(bridge.ClaudePromptTimeout, match="did not become ready"):
                    bridge.inject_user_message(
                        bridge_dir, content="MUST_NOT_DELIVER", timeout_s=0.3
                    )
                assert not delivered.exists()
        finally:
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run([*tmux, "kill-server"], check=False, capture_output=True, timeout=5)


# Boots silently (a blank pane), then drops whatever was typed meanwhile, as the
# real TUI does when it switches the tty to raw mode, and takes a composer.
_SILENT_BOOT_TERMINAL = r"""
import json
import os
import sys
import termios
import time
import tty
from pathlib import Path

delivered, boot_s = Path(sys.argv[1]), float(sys.argv[2])
fd = sys.stdin.fileno()
tty.setraw(fd)
time.sleep(boot_s)
termios.tcflush(fd, termios.TCIFLUSH)

def render(draft=""):
    sys.stdout.write("\x1b[H\x1b[2J" + "─" * 80 + "\r\n❯ " + draft + "\r\n" + "─" * 80 + "\r\n")
    sys.stdout.flush()

render()
draft, lines = "", []
pending = ""
clear_keys = ("\x1b[107;5u", "\x1b[117;5u")
while True:
    pending += os.read(fd, 65536).decode(errors="ignore")
    while pending:
        clear_key = next((key for key in clear_keys if pending.startswith(key)), None)
        if clear_key is not None:
            draft = ""
            pending = pending[len(clear_key):]
            continue
        if any(key.startswith(pending) for key in clear_keys):
            break
        ch, pending = pending[0], pending[1:]
        if ch == "\x15":
            draft = ""
        elif ch == "\r":
            lines.append(draft)
            draft = ""
            delivered.write_text(json.dumps(lines))
        elif ch >= " ":
            draft += ch
    render(draft)
"""


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.timeout(20)
def test_slash_command_waits_for_a_silently_booting_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command sent during a silent boot is typed after it, not into the void.

    A blank pane gives the occupied-input check nothing to clear, so without the
    readiness gate the command was typed at once and dropped by the starting
    TUI, and the submit Enter then sent an empty line.
    """
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    delivered = tmp_path / "delivered.json"
    terminal = tmp_path / "terminal.py"
    terminal.write_text(_SILENT_BOOT_TERMINAL)
    with tempfile.TemporaryDirectory(prefix="claude-ready-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "tmux.sock"
        tmux = ["tmux", "-S", str(socket_path)]
        command = shlex.join([sys.executable, str(terminal), str(delivered), "1.0"])
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
                    "120",
                    "-y",
                    "24",
                    command,
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            bridge_dir = tmp_path / "bridge"
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
            bridge.inject_slash_command(
                bridge_dir, command="/effort high", timeout_s=1.0, ready_timeout_s=9.0
            )
            assert json.loads(delivered.read_text()) == ["/effort high"]
        finally:
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run([*tmux, "kill-server"], check=False, capture_output=True, timeout=5)
