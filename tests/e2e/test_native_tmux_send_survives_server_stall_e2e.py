"""E2E regression: a too-short claude-native tmux send timeout kills workers
when the tmux server responds slowly.

The reported journey: a polly mission dispatches to a ``claude-native`` worker
on a large worktree; the parallel boots pin the machine, the per-session tmux
server is starved for a few seconds, the first ``tmux send-keys`` exceeds the
hardcoded ``_TMUX_SEND_TIMEOUT_S = 5.0`` budget, and the worker dies with
``claude_code failed on infra (tmux command timed out after 5.0s)`` — even
though the caller allowed a 30s delivery window and the tmux server recovers
seconds later. ``cursor_native_bridge`` gives the identical operation 10s.

This test drives the real transport end-to-end: a real tmux server on a
private socket, advertised through the production ``write_tmux_target``, and a
delivery through the public ``inject_interrupt`` entry point (the web-UI Stop
path — the same ``_run_tmux`` send that delivers every keystroke, including
the first dispatch message). The load condition is injected deterministically
by SIGSTOPping the tmux server for STALL_S seconds — exactly what CPU
starvation does to it — with the resume guaranteed by a timer.

Before the fix: ``inject_interrupt`` raises ``RuntimeError: tmux command
timed out after 5.0s`` at t=5 and this test FAILS.
After a fix (larger/configurable send budget, or send retries): the delivery
survives the 7s stall — well inside the caller's 30s window — and it PASSES.

Runs with no LLM, no claude binary, and no server — only ``tmux``::

    pytest tests/e2e/test_native_tmux_send_survives_server_stall_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.claude_native_bridge import (
    _BRIDGE_ROOT,
    inject_interrupt,
    write_tmux_target,
)

# How long the tmux server is unresponsive. Chosen between claude-native's 5s
# send budget (so the bug fires) and cursor-native's 10s budget for the same
# operation (so any consistent fix passes), and far inside the 30s delivery
# window the caller grants.
STALL_S = 7.0
DELIVERY_WINDOW_S = 30.0

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")


@pytest.fixture
def stalled_tmux_pane(tmp_path: Path) -> Iterator[Path]:
    """A claude-native bridge dir advertising a real tmux pane whose server
    is SIGSTOPped for the first STALL_S seconds.

    Yields the bridge dir. The tmux server always gets SIGCONT and is killed
    on teardown, even when the test body raises.
    """
    socket_path = tmp_path / "tmux.sock"
    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "claude",
            "-x",
            "80",
            "-y",
            "24",
            "cat",
        ],
        check=True,
        timeout=30.0,
    )
    server_pid = int(
        subprocess.run(
            ["tmux", "-S", str(socket_path), "display-message", "-p", "-t", "claude", "#{pid}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30.0,
        ).stdout.strip()
    )

    # The bridge validates its dir sits under the trusted claude-native root,
    # so the fixture cannot use tmp_path for it.
    bridge_dir = _BRIDGE_ROOT / f"stall-test-{uuid.uuid4().hex}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="claude")

    def _resume() -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(server_pid, signal.SIGCONT)

    # Starve the tmux server — the deterministic stand-in for the reported
    # machine-load condition — with the resume guaranteed by a timer.
    os.kill(server_pid, signal.SIGSTOP)
    resumer = threading.Timer(STALL_S, _resume)
    resumer.start()
    try:
        yield bridge_dir
    finally:
        resumer.cancel()
        _resume()
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            check=False,
            timeout=30.0,
        )
        shutil.rmtree(bridge_dir, ignore_errors=True)


def test_send_survives_a_tmux_server_stall_shorter_than_the_delivery_window(
    stalled_tmux_pane: Path,
) -> None:
    """A tmux server stall of STALL_S must not kill a delivery whose caller
    allowed DELIVERY_WINDOW_S.

    Before the fix the hardcoded 5.0s per-command budget fires mid-stall and
    the worker is marked failed on infra; the error message below is the
    exact string the bug report quotes.
    """
    started = time.monotonic()
    try:
        inject_interrupt(stalled_tmux_pane, timeout_s=DELIVERY_WINDOW_S)
    except RuntimeError as exc:
        elapsed = time.monotonic() - started
        pytest.fail(
            "claude-native send died mid-stall after "
            f"{elapsed:.1f}s with {exc!r} — the tmux server was back at "
            f"t={STALL_S}s, inside the {DELIVERY_WINDOW_S}s delivery window "
            "the caller allowed"
        )
    elapsed = time.monotonic() - started
    # Sanity: delivery genuinely waited out the stall (the send landed on the
    # live server, not on a dead socket before the stall began).
    assert elapsed >= STALL_S - 1.0, (
        f"delivery returned after only {elapsed:.1f}s — the stall never "
        "took effect, so this run proves nothing"
    )
