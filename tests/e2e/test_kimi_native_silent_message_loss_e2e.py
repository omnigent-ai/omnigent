"""A message sent during slow Kimi TUI boot must not silently disappear.

Drive the real executor, readiness polling and tmux transport with a stand-in
TUI. Hold its mount until the turn returns so it cannot beat the short readiness
budget on a busy worker. Mounting flushes buffered input, as a real boot does.
A successful turn must have delivered its message; a readiness timeout must
surface an explicit error. Only the readiness budget and stand-in are controlled.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.kimi_native.bridge import _kimi_tui_ready, write_tmux_target
from omnigent.inner import kimi_native_executor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.kimi_native_executor import KimiNativeExecutor
from tests._helpers.polling import poll_until

_READY_TIMEOUT_S = 2.0
# Mounting is released explicitly after the readiness gate returns.
_MOUNT_EXTRA_TIMEOUT_S = 30.0
# Window after the turn for a (correct) delayed/retried delivery to land — a
# fixed injection path that verifies its submit would deliver within this.
_DELIVERY_GRACE_S = 15.0
_POLL_S = 0.25
_TMUX_SESSION = "kimislowboot"
_MESSAGE = "slow-boot delivery probe: reply with the single word pong"

pytestmark = [
    pytest.mark.timeout(240, method="signal"),
    pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux"),
]

# The mount gate keeps boot slower than readiness without a long fixed sleep.
_STANDIN_TUI = textwrap.dedent(
    """
    import sys, termios, time
    from pathlib import Path

    mount_gate = Path(sys.argv[1])
    sink_path = sys.argv[2]
    Path(sys.argv[3]).touch()
    while not mount_gate.exists():
        time.sleep(0.05)
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    print("╭" + "─" * 60 + "╮")
    print("│ > " + " " * 56 + " │")
    print("╰" + "─" * 60 + "╯")
    print("context: 0% (0/1M)")
    sys.stdout.flush()
    with open(sink_path, "a", encoding="utf-8") as sink:
        for line in sys.stdin:
            sink.write(line)
            sink.flush()
    """
)


def _capture_pane(socket_path: Path, target: str) -> str:
    """Capture the visible pane contents; ``\"\"`` on any failure."""
    proc = subprocess.run(
        ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-t", target],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


async def _run_one_turn(executor: KimiNativeExecutor) -> list[Any]:
    events: list[Any] = []
    async for event in executor.run_turn(
        messages=[{"role": "user", "content": _MESSAGE}],
        tools=[],
        system_prompt="",
    ):
        events.append(event)
    return events


def test_message_dispatched_during_slow_boot_is_not_silently_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn reporting success must have delivered its message to the real pane."""
    monkeypatch.setattr(
        kimi_native_executor,
        "inject_user_message",
        partial(kimi_native_executor.inject_user_message, timeout_s=_READY_TIMEOUT_S),
    )
    socket_path = tmp_path / "tmux.sock"
    bridge_dir = tmp_path / "bridge"
    sink = tmp_path / "received.txt"
    mount_gate = tmp_path / "mount"
    boot_started = tmp_path / "boot_started"
    standin = tmp_path / "standin_tui.py"
    standin.write_text(_STANDIN_TUI, encoding="utf-8")

    launch = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(standin))} "
        f"{shlex.quote(str(mount_gate))} {shlex.quote(str(sink))} "
        f"{shlex.quote(str(boot_started))}"
    )
    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            _TMUX_SESSION,
            "-x",
            "200",
            "-y",
            "50",
            launch,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        poll_until(boot_started.exists, timeout_s=30, interval_s=_POLL_S)
        # What the runner does at kimi terminal-create time.
        write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target=_TMUX_SESSION)

        # What a web-UI message send executes on the harness side.
        executor = KimiNativeExecutor(bridge_dir=bridge_dir)
        events = asyncio.run(_run_one_turn(executor))

        turn_completed = any(isinstance(e, TurnComplete) for e in events)
        turn_errored = any(isinstance(e, ExecutorError) for e in events)

        mount_gate.touch()
        mount_deadline = time.monotonic() + _MOUNT_EXTRA_TIMEOUT_S
        pane = ""
        while time.monotonic() < mount_deadline:
            pane = _capture_pane(socket_path, _TMUX_SESSION)
            if "context:" in pane:
                break
            time.sleep(_POLL_S)
        assert "context:" in pane, f"stand-in TUI never mounted; pane:\n{pane}"
        assert _kimi_tui_ready(pane), f"mounted stand-in is not a valid ready Kimi pane:\n{pane}"

        # Only successful turns can promise a delayed delivery. An explicit
        # readiness failure has already satisfied the no-silent-loss contract.
        delivered = False
        grace_deadline = time.monotonic() + _DELIVERY_GRACE_S
        while turn_completed and not turn_errored and time.monotonic() < grace_deadline:
            if sink.exists() and _MESSAGE in sink.read_text(encoding="utf-8"):
                delivered = True
                break
            time.sleep(_POLL_S)

        received = sink.read_text(encoding="utf-8") if sink.exists() else ""
        pane = _capture_pane(socket_path, _TMUX_SESSION)

        assert turn_completed or turn_errored, f"turn yielded neither outcome: {events!r}"
        if turn_errored:
            assert any(
                isinstance(event, ExecutorError)
                and f"did not become ready within {_READY_TIMEOUT_S:.0f}s" in event.message
                for event in events
            ), f"expected readiness failure, got: {events!r}"
        if turn_completed and not turn_errored:
            # The silent-loss contract: success implies delivery. On unfixed
            # main this fails — TurnComplete was yielded (session marked idle,
            # parent orchestrator satisfied) while the TUI never received the
            # message.
            assert delivered, (
                "kimi-native silently lost the dispatched message: run_turn "
                "reported TurnComplete but the mounted TUI never received it.\n"
                f"events={events!r}\n"
                f"TUI received: {received!r}\n"
                f"pane after mount:\n{pane}"
            )
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            check=False,
            capture_output=True,
            timeout=10,
        )
