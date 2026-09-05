"""End-to-end repro: kimi-native silently loses a message dispatched during a slow TUI boot.

The reported journey: a kimi-native session's first boot can exceed 30s
(kimi 0.34.0 cold start with an OAuth refresh took ~36s in the live repro).
A message dispatched during that boot is silently lost:

1. ``inject_user_message``'s readiness gate ``_settle_pane`` hits its 30s
   deadline (``_TMUX_READY_TIMEOUT_S``) and **falls through silently**,
   blind-pasting into a still-booting TUI that discards the input on mount.
2. The paste-needle wait and the blind Enter land before/into an empty input
   box; nothing is submitted.
3. ``KimiNativeExecutor.run_turn`` yields ``TurnComplete`` unconditionally, so
   the session is marked idle. No error surfaces anywhere; a parent
   orchestrator waits forever.

This test drives the REAL product path a web-UI message send executes on the
harness side — ``KimiNativeExecutor.run_turn`` → ``inject_user_message`` →
tmux bracketed paste + Enter — against a real tmux pane. Only the kimi binary
is substituted (it is not installable/authenticatable in CI): the pane runs a
stand-in TUI that boots slower than the readiness gate, then mounts an input
box + the ``context:`` footer and, like a real TUI initializing the terminal,
flushes input buffered during boot (the live repro's observed end state was a
mounted but EMPTY input box). After mounting it records every line it receives.

Contract asserted (fails on unfixed main, passes once the injection path is
hardened): **a turn that reports success must have delivered its message** —
either the TUI actually received the text, or the turn must surface an error.
On unfixed main the turn completes "successfully" while the message was never
delivered, which is exactly the silent loss users hit.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_kimi_native_silent_message_loss_e2e.py -v --timeout=240
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.kimi_native_executor import KimiNativeExecutor
from omnigent.kimi_native_bridge import write_tmux_target

# Boot slower than the bridge's 30s readiness gate (_TMUX_READY_TIMEOUT_S) but
# only just — mirrors the live repro where kimi mounted ~5s after the gate gave
# up (first boot ~36s including an OAuth refresh).
_TUI_BOOT_DELAY_S = 40.0
# How long past the scripted boot delay to wait for the stand-in to mount.
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

# Stand-in for the kimi TUI: silent boot past the readiness gate, then mount an
# input box + the ``context:`` footer (the bridge's only readiness marker) and
# flush terminal input buffered during boot — real TUIs initialize the terminal
# on mount, and the live repro's end state was a mounted but EMPTY input box.
# After mounting it appends every received stdin line to a sink file.
_STANDIN_TUI = textwrap.dedent(
    """
    import sys, termios, time

    boot_delay = float(sys.argv[1])
    sink_path = sys.argv[2]
    time.sleep(boot_delay)
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    print("+" + "-" * 60 + "+")
    print("| > " + " " * 56 + " |")
    print("+" + "-" * 60 + "+")
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


def test_message_dispatched_during_slow_boot_is_not_silently_lost(tmp_path: Path) -> None:
    """A turn that reports TurnComplete must have delivered its message to the TUI.

    On unfixed main: the readiness gate times out silently, the paste is
    discarded by the booting TUI, run_turn still yields TurnComplete — the
    message is gone with no error anywhere (the live repro's end state).
    """
    socket_path = tmp_path / "tmux.sock"
    bridge_dir = tmp_path / "bridge"
    sink = tmp_path / "received.txt"
    standin = tmp_path / "standin_tui.py"
    standin.write_text(_STANDIN_TUI, encoding="utf-8")

    launch = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(standin))} "
        f"{_TUI_BOOT_DELAY_S} {shlex.quote(str(sink))}"
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
        # What the runner does at kimi terminal-create time.
        write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target=_TMUX_SESSION)

        # What a web-UI message send executes on the harness side.
        executor = KimiNativeExecutor(bridge_dir=bridge_dir)
        events = asyncio.run(_run_one_turn(executor))

        turn_completed = any(isinstance(e, TurnComplete) for e in events)
        turn_errored = any(isinstance(e, ExecutorError) for e in events)

        # Wait for the stand-in TUI to finish mounting (readiness footer up).
        mount_deadline = time.monotonic() + _TUI_BOOT_DELAY_S + _MOUNT_EXTRA_TIMEOUT_S
        pane = ""
        while time.monotonic() < mount_deadline:
            pane = _capture_pane(socket_path, _TMUX_SESSION)
            if "context:" in pane:
                break
            time.sleep(_POLL_S)
        assert "context:" in pane, f"stand-in TUI never mounted; pane:\n{pane}"

        # Give a fixed implementation room to deliver (verified submit/retry).
        delivered = False
        grace_deadline = time.monotonic() + _DELIVERY_GRACE_S
        while time.monotonic() < grace_deadline:
            if sink.exists() and _MESSAGE in sink.read_text(encoding="utf-8"):
                delivered = True
                break
            time.sleep(_POLL_S)

        received = sink.read_text(encoding="utf-8") if sink.exists() else ""
        pane = _capture_pane(socket_path, _TMUX_SESSION)

        assert turn_completed or turn_errored, f"turn yielded neither outcome: {events!r}"
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
