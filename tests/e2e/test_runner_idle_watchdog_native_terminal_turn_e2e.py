"""E2E repro: the runner idle watchdog must not shut down during an active
native terminal turn.

Reported journey
----------------
1. Configure a short runner idle timeout in ``~/.omnigent/config.yaml``
   (``runner.idle_timeout_s: 10``).
2. Start a Codex-native session.
3. Send one prompt that keeps Codex working and producing terminal output for
   longer than the configured idle timeout, without sending another message.
4. Observe the runner log ``runner idle timeout reached after <N>s with no
   active work; shutting down`` and exit while the native turn is still active.

Why this is driven at the runner layer (not the codex-native TUI)
-----------------------------------------------------------------
The defect is entirely in the runner's inactivity watchdog. A native terminal
turn is *not* tracked in the runner's ``_active_turns`` map once terminal
delivery takes over; the runner instead records the native pane's ``running``
status (published through the resource registry's session-status publisher,
the same callback a codex/claude-native forwarder's ``running`` edge flows
through) and emits ``session.terminal.activity`` events. On the unfixed build
``has_active_work()`` consults neither signal, so a single long-running
Codex-native turn reaches ``runner.idle_timeout_s`` and the watchdog shuts the
runner down mid-turn.

The Codex-native TUI cannot be launched under CI (codex-native needs an
interactive Codex login anchored to the real ``$HOME``; it is gated behind
``OMNIGENT_E2E_CODEX_NATIVE=1`` and a present-but-unauthenticated binary hangs
the TUI), and the user-visible outcome is a runner log line + the terminal
disconnecting. So this drives the genuine runner objects in process: the real
``create_runner_app`` app, its registered native session-status /
terminal-activity publishers, the real ``app.state.has_active_work`` callback,
and the real ``_run_inactivity_monitor``. No fabricated end-state is written:
the native ``running`` edge is published exactly as a native forwarder does.

Contract asserted (RED on unfixed main, GREEN once the fix lands)
-----------------------------------------------------------------
* A native terminal turn that is ``running`` counts as active work.
* The real inactivity monitor, with a short idle timeout, does **not** request
  shutdown while the native pane is still running, and resumes normal idle
  shutdown once the turn settles to ``idle``.

Self-contained: no live server, no credentials, no network, no CLI binary.

Run::

    pytest tests/e2e/test_runner_idle_watchdog_native_terminal_turn_e2e.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app, pending_approvals
from omnigent.runner._entry import _run_inactivity_monitor
from omnigent.runner.app import _session_timers
from tests.runner.helpers import NullServerClient

# The runner's ``session.status`` value a native terminal turn publishes while
# the agent is working, and the settled value once the turn finishes.
_NATIVE_RUNNING = "running"
_NATIVE_IDLE = "idle"
# A native codex session id; the concrete value is immaterial to the defect.
_CONV_ID = "conv_codex_native_probe"


@pytest.fixture(autouse=True)
def _reset_runner_active_work_globals() -> None:
    """Reset module-global approval / timer registries between tests.

    ``has_active_work`` consults these process-global registries, so a leaked
    entry from another test sharing the interpreter could make the runner look
    busy and mask the defect. Each test also builds a fresh app (whose native
    pane state is per-app), so only these globals need clearing.

    :returns: None.
    """
    pending_approvals.reset_for_tests()
    for session_timers in list(_session_timers.values()):
        for task in list(session_timers.values()):
            task.cancel()
    _session_timers.clear()
    yield
    pending_approvals.reset_for_tests()
    for session_timers in list(_session_timers.values()):
        for task in list(session_timers.values()):
            task.cancel()
    _session_timers.clear()


def _publish_native_status(app: FastAPI, conversation_id: str, status: str) -> None:
    """Publish a native session-status edge through the runner's real publisher.

    Uses ``app.state.session_resource_registry._session_status_publisher`` —
    the callback ``create_runner_app`` registers via
    ``set_session_status_publisher`` and the exact channel a native
    forwarder's ``running`` / ``idle`` edge flows through into the runner
    (``_publish_session_status`` -> ``_publish_event`` -> ``native_pane_status``).

    :param app: The runner FastAPI app from :func:`create_runner_app`.
    :param conversation_id: Native session id, e.g. ``"conv_codex_native_probe"``.
    :param status: Session status edge, e.g. ``"running"`` or ``"idle"``.
    :returns: None.
    """
    publish_status = app.state.session_resource_registry._session_status_publisher
    assert callable(publish_status), (
        "runner must register a native session-status publisher; "
        "the native pane's running edge has no channel otherwise"
    )
    publish_status(conversation_id, status)


def _publish_native_terminal_activity(app: FastAPI, conversation_id: str) -> None:
    """Emit a ``session.terminal.activity`` pulse via the runner's real publisher.

    Mirrors a native pane producing terminal output mid-turn. Uses the
    ``_terminal_activity_publisher`` the runner registers via
    ``set_terminal_activity_publisher``.

    :param app: The runner FastAPI app.
    :param conversation_id: Native session id.
    :returns: None.
    """
    publish_activity = app.state.session_resource_registry._terminal_activity_publisher
    assert callable(publish_activity), "runner must register a terminal-activity publisher"
    publish_activity(conversation_id, "terminal_codex_main")


@pytest.mark.asyncio
async def test_active_native_terminal_turn_counts_as_active_work() -> None:
    """A running native terminal turn must count as active runner work.

    The core defect: after publishing the native ``running`` edge the runner
    records the pane as running, yet ``has_active_work()`` ignores it. On the
    unfixed build this returns ``False`` (RED); once native terminal turns are
    counted it returns ``True`` (GREEN).

    :returns: None.
    """
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    _publish_native_status(app, _CONV_ID, _NATIVE_RUNNING)

    # Sanity: the runner does record the native pane as running (present on the
    # unfixed build too — the running signal exists, it is just not consulted).
    assert app.state.native_pane_status.get(_CONV_ID) == _NATIVE_RUNNING

    # The defect. A native terminal turn that is running is active work.
    assert app.state.has_active_work() is True, (
        "runner idle watchdog does not count an active native terminal turn as "
        "work: has_active_work() is False while native_pane_status is 'running'"
    )


@pytest.mark.asyncio
async def test_idle_watchdog_does_not_shut_down_active_native_terminal_turn() -> None:
    """The real inactivity monitor must not shut down a running native turn.

    Drives the genuine ``_run_inactivity_monitor`` with a short idle timeout
    (the ticket's ``runner.idle_timeout_s`` shrunk from 10s to keep the test
    fast) against the runner's real ``has_active_work`` callback, while a
    native terminal turn is running and producing terminal activity. On the
    unfixed build the monitor logs the idle-timeout shutdown and calls
    ``request_shutdown`` mid-turn (RED). Once native turns count as work it
    stays alive while running and resumes idle shutdown after the turn settles
    to ``idle`` (GREEN).

    :returns: None.
    """
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    # The user sends one prompt; the native turn goes running and produces
    # terminal output for longer than the idle timeout.
    _publish_native_status(app, _CONV_ID, _NATIVE_RUNNING)
    _publish_native_terminal_activity(app, _CONV_ID)

    loop = asyncio.get_running_loop()
    shutdowns: list[str] = []
    monitor = asyncio.create_task(
        _run_inactivity_monitor(
            idle_timeout_s=0.02,
            # Last real inbound-tunnel activity is well past the idle window,
            # so only active work can keep the runner alive.
            get_last_activity=lambda: loop.time() - 1.0,
            has_active_work=app.state.has_active_work,
            request_shutdown=lambda: shutdowns.append("shutdown"),
            poll_interval_s=0.005,
        )
    )

    # While the native turn is still running the watchdog must NOT shut down.
    # Give it many idle windows to (mis)fire; on the unfixed build it fires.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(monitor), timeout=0.15)
    assert shutdowns == [], (
        "runner idle watchdog shut down while a native terminal turn was still "
        "running: expected the running pane to keep the "
        "runner alive"
    )
    assert not monitor.done()

    # The turn settles: normal idle shutdown must now resume.
    _publish_native_status(app, _CONV_ID, _NATIVE_IDLE)
    await asyncio.wait_for(monitor, timeout=1.0)
    assert shutdowns == ["shutdown"], (
        "after the native turn settled to idle the runner must become idle-eligible and shut down"
    )
