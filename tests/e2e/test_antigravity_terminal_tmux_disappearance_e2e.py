"""E2E repro: the antigravity ``main`` terminal disappears from tmux.

The reported journey: a user runs an antigravity-native session (the ``agy``
TUI lives in the runner-owned tmux terminal named ``antigravity:main``); the
tmux backing that terminal becomes unavailable while the session is live; the
threaded idle watcher confirms three consecutive failed probes, logs
``tmux unavailable after 3 consecutive probes for terminal antigravity:main``
at ERROR, and fires the required-terminal exit that ends the session — the
main terminal disappears from the app.

Two scenarios, both driving a real tmux server on a private socket through the
production :class:`~omnigent.inner.terminal.TerminalInstance` threaded watcher
(the exact code path that emitted the field signature). The agy TUI itself is
stood in by a long-lived placeholder pane process: agy is OAuth-only and this
failure lives entirely in the tmux/watcher layer, never in agy.

1. **Genuine tmux death is detected** — killing the tmux server externally
   must surface the terminal exit (watcher stops, ``on_exit`` fires) and
   today logs the exact field signature. This documents the observable
   failure and guards the detection path.
2. **A transient probe outage must not kill a live terminal** — when the
   ``tmux`` client transiently fails (resource pressure, connect failure)
   while the tmux session itself stays alive, the watcher must NOT declare
   the terminal dead. Today it does: ``_tmux_session_exists_sync`` treats any
   probe-execution failure as "session gone", so three seconds of client
   flakiness kills a healthy required terminal and ends the session. This
   test FAILS before a fix and passes once probe-execution failure is
   distinguished from an authoritative "session does not exist".

Runs with no LLM, no agy binary, and no server — only ``tmux``::

    pytest tests/e2e/test_antigravity_terminal_tmux_disappearance_e2e.py -v
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from omnigent.inner.terminal import TerminalInstance

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

# The watcher probes once per second and declares exit after 3 consecutive
# confirmed failures, so a genuine death is reported within ~4s. Generous CI
# margin on top.
_EXIT_REPORT_TIMEOUT_S = 20.0
# Transient-outage window: comfortably above the watcher's 3-probe threshold
# so the misclassification (bug) deterministically fires before the outage
# ends, while the tmux session stays alive throughout.
_TRANSIENT_OUTAGE_S = 8.0
# After the outage clears, healthy probes need a few ticks to resume.
_RECOVERY_WAIT_S = 5.0

_SIGNATURE = "tmux unavailable after 3 consecutive probes for terminal antigravity:main"


def _launch_antigravity_main_terminal(short_dir: Path) -> TerminalInstance:
    """Launch a real tmux-backed terminal named ``antigravity:main``.

    The pane runs a long-lived placeholder process standing in for the agy
    TUI — this failure mode is entirely in the tmux/watcher layer.

    :param short_dir: Short-path directory for the private tmux socket
        (long pytest tmp paths overflow the AF_UNIX cap on some platforms).
    :returns: The launched instance, ``running=True``.
    """
    instance = TerminalInstance(
        name="antigravity",
        session_key="main",
        socket_path=short_dir / "tmux.sock",
        private_dir=short_dir,
        command="sh",
        args=["-c", "while :; do sleep 1; done"],
    )
    asyncio.run(instance.launch(cwd=short_dir))
    return instance


@pytest.fixture
def short_dir() -> Path:
    """A short-path scratch dir so the tmux AF_UNIX socket path stays legal."""
    return Path(tempfile.mkdtemp(prefix="og-agy-tmux-"))


def test_external_tmux_death_reports_the_required_terminal_exit(
    short_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Killing the tmux server out from under ``antigravity:main`` must be
    detected: the watcher stops, fires ``on_exit`` (the required-terminal
    death that ends the antigravity session), and reports the disappearance
    for terminal ``antigravity:main``.

    This is the reported journey's observable tail — the field occurrence
    logged exactly this signature from ``_idle_watch_loop_threaded``.
    """
    instance = _launch_antigravity_main_terminal(short_dir)
    exit_fired = threading.Event()
    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.inner.terminal"):
            instance.start_idle_watcher_thread(on_exit=exit_fired.set)
            # Let at least one healthy probe land before the kill.
            time.sleep(2.0)

            subprocess.run(
                ["tmux", "-S", str(instance.socket_path), "kill-server"],
                check=False,
                timeout=30.0,
            )

            assert exit_fired.wait(_EXIT_REPORT_TIMEOUT_S), (
                "watcher never reported the terminal exit after the tmux "
                "server backing antigravity:main was killed"
            )
        assert not instance.running
        messages = [record.getMessage() for record in caplog.records]
        assert any(_SIGNATURE in message for message in messages), (
            f"expected the disappearance to be reported for antigravity:main; got: {messages!r}"
        )
    finally:
        subprocess.run(
            ["tmux", "-S", str(instance.socket_path), "kill-server"],
            check=False,
            timeout=30.0,
        )


def test_transient_tmux_probe_outage_must_not_kill_a_live_terminal(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient tmux *client* outage — probes failing while the tmux
    session itself stays alive — must not make the watcher declare the
    ``antigravity:main`` terminal dead and end the session.

    Today it does: a probe-execution failure is indistinguishable from an
    authoritative "session does not exist" in ``_tmux_session_exists_sync``,
    so three seconds of client flakiness (fork pressure, socket connect
    failure) kills a healthy required terminal. The tmux session is verified
    alive after the outage, yet the watcher has already reported exit — the
    main terminal "disappears from tmux" while tmux still holds it.
    """
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None

    # A PATH shim standing in for the transient client failure: while the
    # flag file exists every tmux invocation fails the way a starved client
    # does (non-zero, "error connecting"), then recovers to the real binary.
    shim_dir = short_dir / "shim-bin"
    shim_dir.mkdir()
    outage_flag = short_dir / "outage-active"
    shim = shim_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ -e "{outage_flag}" ]; then\n'
        '  echo "error connecting to socket (transient failure)" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'exec "{real_tmux}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    instance = _launch_antigravity_main_terminal(short_dir)
    exit_fired = threading.Event()
    try:
        # The watcher resolves ``tmux`` via PATH on every probe, so the shim
        # governs probes from here on. Launch above used the real binary.
        monkeypatch.setenv("PATH", f"{shim_dir}:{Path(real_tmux).parent}")

        instance.start_idle_watcher_thread(on_exit=exit_fired.set)
        time.sleep(2.0)  # at least one healthy probe first

        outage_flag.touch()
        time.sleep(_TRANSIENT_OUTAGE_S)
        outage_flag.unlink()
        time.sleep(_RECOVERY_WAIT_S)

        session_alive = subprocess.run(
            [
                real_tmux,
                "-S",
                str(instance.socket_path),
                "has-session",
                "-t",
                instance.tmux_target,
            ],
            capture_output=True,
            timeout=30.0,
        )
        assert session_alive.returncode == 0, (
            "test precondition broken: the tmux session itself must survive "
            "the transient client outage"
        )

        assert not exit_fired.is_set(), (
            "watcher declared the antigravity:main terminal dead during a "
            "transient tmux client outage although the tmux session was "
            "alive the whole time — the live session was killed"
        )
        assert instance.running, (
            "watcher stopped the terminal instance during a transient tmux "
            "client outage although the tmux session was alive"
        )
    finally:
        subprocess.run(
            [real_tmux, "-S", str(instance.socket_path), "kill-server"],
            check=False,
            timeout=30.0,
        )
