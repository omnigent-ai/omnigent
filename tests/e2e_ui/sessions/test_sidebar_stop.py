"""Browser e2e: a stopped session surfaces the reconnect affordance.

What "stop session" looks like to the user is the agent going away: the
runner tunnel drops, the open chat flips to ``local_stranded`` liveness
(not host-bound → no host to relaunch it), and the composer area swaps in
the "Agent disconnected — click to reconnect" banner
(``data-testid="disconnected-indicator"``). Clicking it opens the
reconnect dialog with the exact ``omnigent run … --resume <id>`` command
to bring the session back from the user's own machine.

The e2e harness binds a tunneled, non-host runner, so the sidebar kebab's
"Stop session" item — gated to host-spawned / claude-native sessions by
``isSessionStoppable`` — isn't offered, and a forwarded ``stop_session``
is a no-op for the openai-agents harness (no external process, and only
host-spawned sessions stop their runner). The observable that the same
liveness chain produces is reproduced here by dropping the runner
directly, then asserting the disconnected → click → reconnect-dialog flow
(the dialog half is not covered by ``test_stale_stream``, which kills the
runner mid-stream and only checks the banner text).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess

import httpx
from playwright.sync_api import Page, expect


def _find_runner_pids() -> list[int]:
    """Find PIDs of the runner entry point (``omnigent.runner._entry``).

    The runner is a sibling subprocess of the server (both spawned by the
    fixture), so we match on the command line rather than the parent PID.

    :returns: List of runner PIDs (may be empty).
    """
    result = subprocess.run(
        ["pgrep", "-f", "omnigent.runner._entry"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [int(line.strip()) for line in result.stdout.strip().splitlines() if line.strip()]


def test_stopped_session_shows_reconnect_affordance(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A dropped runner flips the open chat to the reconnect banner + dialog.

    Waits for the frontend's ``/health`` poll to observe the runner online
    first (so the ``starting`` cold-boot grace can't mask the drop — once a
    runner has been seen online, a later offline is a real disconnect),
    kills the runner, then asserts:

    - the "Agent disconnected — click to reconnect" banner appears, and
    - clicking it opens the reconnect dialog carrying the session's
      ``--resume`` command.

    Killing the shared runner is order-independent: the ``seeded_session``
    fixture respawns it for the next test (same contract as
    ``test_stale_stream``).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    # Confirm the frontend observed the runner ONLINE via a successful
    # /health poll before we kill it. Without this the freshly created
    # session could still be inside its 45s cold-boot grace, which would
    # render "Connecting…" instead of the reconnect banner. The health
    # poller fires on mount and reschedules every ~10s; capturing one
    # successful poll proves the frontend saw runner_online=true.
    with page.expect_response(
        lambda r: "/health" in r.url and "session_id" in r.url and r.status == 200,
        timeout=15_000,
    ):
        page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()

    health = httpx.get(f"{base_url}/health?session_id={session_id}", timeout=5).json()
    assert health.get("session", {}).get("runner_online") is True, (
        f"runner should be online before the kill, got: {health}"
    )

    runner_pids = _find_runner_pids()
    assert runner_pids, "no runner processes found to stop"
    for pid in runner_pids:
        os.kill(pid, signal.SIGKILL)

    # The health poll fires every 10s; the banner flips on the next poll
    # that reads runner_online=false. Budget generously past one interval.
    indicator = page.get_by_test_id("disconnected-indicator")
    expect(indicator).to_be_visible(timeout=30_000)
    expect(indicator).to_contain_text(re.compile("disconnected", re.IGNORECASE))

    # Clicking it opens the reconnect dialog with the resume command the
    # user runs to bring the stopped session back.
    indicator.click()
    dialog = page.get_by_test_id("reconnect-session-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(session_id)
