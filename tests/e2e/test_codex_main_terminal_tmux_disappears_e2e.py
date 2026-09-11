r"""E2E regression: the Codex ``codex:main`` terminal disappears from tmux
and the session is torn down.

The observable signature: ``omnigent.inner.terminal._idle_watch_loop_threaded``
logs at ERROR

    tmux unavailable after 3 consecutive probes for terminal codex:main

The user journey: a user is running a Codex (``codex-native``)
session, whose Codex TUI the runner auto-launches into the session-scoped
``codex:main`` managed tmux terminal. Mid-session the tmux server backing that
terminal goes away. The runner's threaded idle watcher — armed on exactly this
terminal in ``omnigent/runner/resource_registry.py`` — probes the pane every
``_IDLE_POLL_INTERVAL_SECONDS`` with ``capture-pane``; when that fails it
confirms with ``has-session``. Once both fail for
``_IDLE_EXIT_FAILURE_THRESHOLD`` (3) consecutive probes the watcher declares
the terminal gone: it logs the signature above, flips ``instance.running`` to
``False`` and fires ``on_exit``. In production ``on_exit`` routes through
``_handle_terminal_exit`` → ``_publish_terminal_exit`` (``omnigent/runner/app.py``),
which publishes ``session.resource.deleted`` for the terminal (the pane
disappears from the web UI) and, because ``codex:main`` is a REQUIRED terminal,
marks the session ``failed`` with ``Required terminal exited unexpectedly``.

This test drives the real product path end to end with a real tmux server and
the production launch + watcher wiring (no LLM, no codex binary needed — the
inner pane process is a stand-in that fills the pane the way a booted Codex TUI
does; the managed-terminal launch, the ``codex:main`` naming and the threaded
idle watcher are the production ones):

1. Launch the ``codex:main`` managed terminal through the production
   ``TerminalRegistry`` (the same call ``launch_required_terminal`` makes).
2. Arm the real threaded idle watcher with an ``on_exit`` callback, exactly as
   the runner arms it for a native terminal.
3. Inject the reported fault: the tmux server backing ``codex:main`` disappears
   (``kill-server`` — the deterministic stand-in for the reported "disappears
   from tmux" condition, whatever removes it in production: an ungraceful
   runner teardown + startup orphan sweep, OOM, a /tmp reaper, a crash).
4. Observe the reported outcome: within the watcher's probe budget it logs the
   exact ``tmux unavailable after 3 consecutive probes for terminal codex:main``
   signature, fires ``on_exit`` (the teardown that removes the pane and fails
   the session), and marks the instance not running.

The sibling test guards the opposite direction — a healthy ``codex:main`` pane
must **not** trip the watcher, so a live user's Codex terminal never vanishes
while its tmux server is fine.

Runs with only ``tmux``::

    pytest tests/e2e/test_codex_main_terminal_tmux_disappears_e2e.py -v
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import (
    _IDLE_EXIT_FAILURE_THRESHOLD,
    _IDLE_POLL_INTERVAL_SECONDS,
)
from omnigent.terminals import TerminalRegistry

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

# The runner arms the codex-native idle watcher on a terminal named ``codex``
# with session key ``main`` — so the emitted signature reads exactly
# "... for terminal codex:main", the string the KPI groups on.
_TERMINAL_NAME = "codex"
_SESSION_KEY = "main"

# The watcher needs _IDLE_EXIT_FAILURE_THRESHOLD consecutive failed probes,
# one per poll interval, to declare the terminal gone. Give it generous
# headroom past that so a slow CI box never flakes the detection.
_EXIT_BUDGET_S = max(30.0, _IDLE_EXIT_FAILURE_THRESHOLD * _IDLE_POLL_INTERVAL_SECONDS * 6)

# How long a healthy pane is watched in the negative test — several poll
# intervals, long enough that a spurious teardown would have fired.
_HEALTHY_OBSERVE_S = max(6.0, _IDLE_POLL_INTERVAL_SECONDS * 5)


def _codex_like_pane_spec(cwd: Path) -> TerminalEnvSpec:
    """A pane that fills like a booted Codex TUI, then idles waiting for input.

    :param cwd: Working directory for the pane process.
    :returns: A managed-terminal spec whose inner process stays alive (so the
        pane is live and healthy until the tmux server itself is removed).
    """
    return TerminalEnvSpec(
        command="bash",
        args=["-c", "echo 'CODEX TUI READY'; exec sleep 600"],
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


async def test_codex_main_terminal_torn_down_when_tmux_server_disappears(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``codex:main``'s tmux server disappears the watcher tears the
    terminal down and logs the teardown signature.

    Reproduces the reported failure: the threaded idle watcher detects the
    vanished server after ``_IDLE_EXIT_FAILURE_THRESHOLD`` probes, logs
    ``tmux unavailable after 3 consecutive probes for terminal codex:main``,
    fires ``on_exit`` (which in production removes the pane from the UI and
    fails the required-terminal session), and marks the instance not running.

    :param tmp_path: Working directory for the managed terminal.
    :param caplog: Captures the watcher's ERROR-level signature line.
    """
    reg = TerminalRegistry()
    exited = threading.Event()
    try:
        instance = await reg.launch(
            "conv_codex_main_vanish",
            _TERMINAL_NAME,
            _SESSION_KEY,
            _codex_like_pane_spec(tmp_path),
        )
        assert instance.running, "codex:main terminal failed to launch"
        assert instance.tmux_target == _SESSION_KEY
        socket_path = str(instance.socket_path)

        def _on_exit() -> None:
            # Production ``on_exit`` -> _handle_terminal_exit ->
            # _publish_terminal_exit: session.resource.deleted (pane vanishes)
            # + session.status failed ("Required terminal exited unexpectedly").
            exited.set()

        instance.start_idle_watcher_thread(on_exit=_on_exit)

        # The watcher must stay quiet while the pane is healthy — a teardown
        # here would mean the exit was spurious, not caused by the fault.
        assert not exited.wait(2.0), "watcher declared exit on a healthy codex:main pane"
        assert instance.running

        with caplog.at_level(logging.ERROR, logger="omnigent.inner.terminal"):
            # THE REPORTED FAULT: the tmux server backing codex:main disappears.
            subprocess.run(
                ["tmux", "-S", socket_path, "kill-server"],
                check=False,
                capture_output=True,
                timeout=15.0,
            )

            fired = exited.wait(_EXIT_BUDGET_S)

        assert fired, (
            "the idle watcher never fired on_exit after the codex:main tmux "
            f"server disappeared (waited {_EXIT_BUDGET_S:.0f}s); the session "
            "would hang instead of tearing down"
        )
        assert not instance.running, (
            "instance.running stayed True after the tmux server vanished; the "
            "watcher must mark the terminal gone"
        )
        signature = (
            f"tmux unavailable after {_IDLE_EXIT_FAILURE_THRESHOLD} consecutive "
            f"probes for terminal {_TERMINAL_NAME}:{_SESSION_KEY}"
        )
        assert signature in caplog.text, (
            "expected the teardown signature "
            f"{signature!r} in the watcher's logs, got:\n{caplog.text}"
        )
    finally:
        await reg.shutdown()


async def test_codex_main_terminal_watcher_stays_quiet_while_tmux_is_healthy(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A healthy ``codex:main`` pane must never trip the watcher.

    Guards the opposite direction of the bug: a live user's Codex terminal
    must not disappear (no ``on_exit``, no teardown signature) while its tmux
    server is perfectly healthy. If a future change made the watcher declare
    exit on transient/slow probes, this catches it.

    :param tmp_path: Working directory for the managed terminal.
    :param caplog: Asserts the teardown signature is absent for a healthy pane.
    """
    reg = TerminalRegistry()
    exited = threading.Event()
    try:
        instance = await reg.launch(
            "conv_codex_main_healthy",
            _TERMINAL_NAME,
            _SESSION_KEY,
            _codex_like_pane_spec(tmp_path),
        )
        assert instance.running, "codex:main terminal failed to launch"

        instance.start_idle_watcher_thread(on_exit=exited.set)

        with caplog.at_level(logging.ERROR, logger="omnigent.inner.terminal"):
            spurious = exited.wait(_HEALTHY_OBSERVE_S)

        assert not spurious, (
            "the idle watcher declared codex:main exited while its tmux server "
            "was healthy — a live user's Codex terminal would vanish for no reason"
        )
        assert instance.running, "healthy codex:main terminal was marked not running"
        assert "tmux unavailable after" not in caplog.text, (
            f"the teardown signature was logged for a healthy pane:\n{caplog.text}"
        )
    finally:
        await reg.shutdown()
