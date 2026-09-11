"""The startup orphan sweep must not take down a live ``opencode:main`` terminal.

An ``opencode-native`` session's runner launches the OpenCode TUI into a managed
tmux terminal named ``opencode:main`` and arms the threaded idle watcher on it.
Terminal instance dirs live in a temp root shared by every process on the box,
and any *other* runner starting up sweeps that root for leaked tmux servers
(:func:`omnigent.inner.terminal.reap_orphaned_terminals`). A sweep that reads an
owner pid it cannot place locally — a marker written in a different pid
namespace or boot reads as a bare pid naming no live process here — must NOT
treat the live terminal as an orphan. When it does, ``tmux kill-server`` lands
on a socket a running session is using; the idle watcher then fails three
consecutive probes, logs at ERROR::

    tmux unavailable after 3 consecutive probes for terminal opencode:main

flips the instance to not running, and fires ``on_exit`` — the runner deletes
``terminal_opencode_main`` and the user's OpenCode terminal disappears from the
session mid-flight.

This test drives that chain against a real tmux server and the real threaded
watcher: launch a live ``opencode:main`` terminal, arm the watcher, stamp the
owner marker with a pid that does not resolve in this namespace, run the
startup sweep, and require the terminal to survive — no reap, no watchdog
teardown, no ``tmux unavailable`` ERROR. On the namespace-blind sweep the live
server is killed and the watcher tears the terminal down, so this fails; the
pid-domain-aware sweep leaves it alone and it passes.

The opposite direction — a *genuinely* vanished tmux server must still tear the
terminal down, and a healthy pane must never trip the watcher — is covered by
``tests/e2e/test_codex_main_terminal_tmux_disappears_e2e.py`` (the watcher is
terminal-name-agnostic).

Runs with only ``tmux``::

    pytest tests/e2e/test_opencode_terminal_survives_orphan_sweep_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import (
    _IDLE_EXIT_FAILURE_THRESHOLD,
    _IDLE_POLL_INTERVAL_SECONDS,
    TerminalInstance,
    reap_orphaned_terminals,
)

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

# The runner arms the opencode-native idle watcher on a terminal named
# ``opencode`` with session key ``main`` — the watchdog signature the KPI
# groups on reads exactly "... for terminal opencode:main".
_TERMINAL_NAME = "opencode"
_SESSION_KEY = "main"

# After the sweep runs, watch the pane past the watcher's full probe budget
# (3 failed probes, one per poll interval) with headroom for a slow CI box: a
# wrongly killed tmux server MUST have tripped the watcher within this window,
# so a quiet watcher here means the terminal really survived.
_QUIET_OBSERVE_S = max(8.0, _IDLE_EXIT_FAILURE_THRESHOLD * _IDLE_POLL_INTERVAL_SECONDS * 3)


def _tmux_server_reachable(socket_path: Path, target: str) -> bool:
    """Return whether the private tmux server/session is still reachable.

    :param socket_path: The instance's private tmux control socket.
    :param target: The tmux session target, e.g. ``"main"``.
    :returns: ``True`` when ``has-session`` succeeds (server alive).
    """
    probe = subprocess.run(
        ["tmux", "-S", str(socket_path), "has-session", "-t", target],
        capture_output=True,
        timeout=5,
    )
    return probe.returncode == 0


async def test_opencode_main_terminal_survives_foreign_runner_startup_sweep(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A foreign runner's startup sweep must leave the live ``opencode:main`` alone.

    :param monkeypatch: Retargets the sweep's temp root at a private scratch dir.
    :param caplog: Asserts the watchdog's teardown signature is never logged.
    """
    # A SHORT scratch root — the tmux control socket path must stay under the
    # ~108-char AF_UNIX limit, so pytest's deeply nested ``tmp_path`` can't be
    # used. Retargeting the sweep's scan root here also keeps it off other
    # tests' live instance dirs.
    scratch = Path(tempfile.mkdtemp(prefix="og-oc-sweep-"))
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: scratch)

    # The dir name must carry the sweep's prefix so its glob matches it.
    instance_dir = scratch / f"{terminal_mod._TERMINAL_DIR_PREFIX}1"
    instance_dir.mkdir()
    socket_path = instance_dir / "tmux.sock"

    # A real, long-lived pane standing in for the booted OpenCode TUI: fills
    # the pane, then idles waiting for input, alive until tmux itself dies.
    instance = TerminalInstance(
        name=_TERMINAL_NAME,
        session_key=_SESSION_KEY,
        socket_path=socket_path,
        private_dir=instance_dir,
        command="bash",
        args=["-c", "echo 'OPENCODE TUI READY'; exec sleep 600"],
        keep_alive_after_exit=True,
    )
    exited = threading.Event()
    await instance.launch(cwd=instance_dir)
    try:
        for _ in range(250):
            if _tmux_server_reachable(socket_path, instance.tmux_target):
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - only on a launch hang/regression
            raise AssertionError("tmux server never became reachable after launch")

        # Arm the real threaded idle watcher exactly as the runner arms it for
        # the native terminal; its on_exit is what deletes the resource in
        # production.
        instance.start_idle_watcher_thread(on_exit=exited.set)
        assert not exited.wait(2.0), "watcher declared exit on a healthy opencode:main pane"

        # Owner marker the sweep cannot place: a pid that is dead in this
        # namespace — byte-identical to what a namespace-blind reader sees for
        # a marker written by a runner in a different pid namespace or boot,
        # while the tmux server (and its true owner) is alive.
        dead = subprocess.Popen(["sh", "-c", "exit 0"])
        dead.wait()
        assert not terminal_mod._process_alive(dead.pid), (
            f"precondition: pid {dead.pid} must be dead in this namespace "
            "(a reused pid would invalidate the scenario; extremely rare, rerun)"
        )
        (instance_dir / terminal_mod._OWNER_PID_FILENAME).write_text(
            str(dead.pid), encoding="utf-8"
        )

        with caplog.at_level(logging.ERROR, logger="omnigent.inner.terminal"):
            # A foreign runner starts up and sweeps for leaked terminals.
            reaped = reap_orphaned_terminals()
            # Watch past the watcher's whole probe budget: if the sweep killed
            # the live server, on_exit fires in here with the reported
            # signature in the log.
            fired = exited.wait(_QUIET_OBSERVE_S)

        signature = (
            f"tmux unavailable after {_IDLE_EXIT_FAILURE_THRESHOLD} consecutive "
            f"probes for terminal {_TERMINAL_NAME}:{_SESSION_KEY}"
        )
        assert not fired, (
            "the startup orphan sweep took down a LIVE opencode:main terminal: "
            "the idle watcher fired on_exit (in production the runner deletes "
            "terminal_opencode_main and the user's OpenCode terminal disappears "
            f"from the session). watcher log:\n{caplog.text}"
        )
        assert reaped == 0, (
            "orphan sweep reaped a LIVE opencode:main terminal whose owner pid "
            f"is merely unresolvable in this namespace (reaped={reaped})"
        )
        assert signature not in caplog.text, (
            f"the watchdog logged the teardown signature {signature!r} for a "
            f"terminal that was alive before the sweep:\n{caplog.text}"
        )
        assert instance.running, "live opencode:main terminal was marked not running"
        assert _tmux_server_reachable(socket_path, instance.tmux_target), (
            "orphan sweep killed the live tmux server backing opencode:main"
        )
        assert instance_dir.exists(), "orphan sweep removed a live terminal's instance dir"
    finally:
        # Under the bug the server is already gone; swallow teardown errors.
        with contextlib.suppress(Exception):
            await instance.close()
        shutil.rmtree(scratch, ignore_errors=True)
