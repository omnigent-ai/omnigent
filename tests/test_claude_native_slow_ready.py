"""Exercise slow-start delivery through a real tmux pane without a Claude account."""

from __future__ import annotations

import contextlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge

_TERMINAL = r"""
import json
import os
import sys
import time
import tty
from pathlib import Path

gate_started, delivered = map(Path, sys.argv[1:3])
tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?2004h")
sys.stdout.flush()
while not gate_started.exists():
    time.sleep(0.01)
if sys.argv[3] == "dead":
    print("Startup failed", flush=True)
    raise SystemExit(1)
print("Starting Claude Code...", flush=True)
time.sleep(1.0)

def render(draft=""):
    sys.stdout.write("\x1b[H\x1b[2J" + "─" * 80 + "\r\n❯ " + draft + "\r\n" + "─" * 80 + "\r\n")
    sys.stdout.flush()

render()
buffer = b""
draft = None
messages = []
while True:
    buffer += os.read(sys.stdin.fileno(), 65536)
    if b"\x1b[200~" in buffer and b"\x1b[201~" in buffer:
        pasted, buffer = buffer.split(b"\x1b[200~", 1)[1].split(b"\x1b[201~", 1)
        draft = pasted.decode().strip()
        render(draft)
    if draft is not None and b"\r" in buffer:
        messages.append(draft)
        delivered.write_text(json.dumps(messages))
        draft = None
        buffer = b""
        render()
"""


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize("startup", ["slow", "dead", "base-budget-only"])
@pytest.mark.timeout(20)
def test_first_message_survives_slow_terminal_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup: str
) -> None:
    """Hold startup until the real readiness gate starts, then exceed its base budget."""
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

    def observe_wait(socket_path: str, tmux_target: str, *, timeout_s: float) -> None:
        started_at.append(time.monotonic())
        gate_started.touch()
        original_wait(socket_path, tmux_target, timeout_s=timeout_s)

    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", observe_wait)
    with tempfile.TemporaryDirectory(prefix="claude-ready-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "tmux.sock"
        tmux = ["tmux", "-S", str(socket_path)]
        command = shlex.join(
            [sys.executable, str(terminal), str(gate_started), str(delivered), startup]
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
                    "24",
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
            bridge_dir = tmp_path / "bridge"
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
            if startup == "slow":
                bridge.inject_user_message(
                    bridge_dir, content="FIRST_PROMPT_DELIVERED", timeout_s=0.3
                )
                assert time.monotonic() - started_at[0] >= 1.0
                assert json.loads(delivered.read_text()) == ["FIRST_PROMPT_DELIVERED"]
                assert bridge._claude_pane_alive(str(socket_path), "main")
            else:
                with pytest.raises(bridge.ClaudePromptTimeout, match="did not become ready"):
                    bridge.inject_user_message(
                        bridge_dir, content="MUST_NOT_DELIVER", timeout_s=0.3
                    )
                assert not delivered.exists()
                if startup == "dead":
                    assert not bridge._claude_pane_alive(str(socket_path), "main")
        finally:
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run([*tmux, "kill-server"], check=False, capture_output=True, timeout=5)
