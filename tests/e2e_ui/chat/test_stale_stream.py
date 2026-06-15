"""Stale-stream banner: kill the runner mid-stream, verify the UI reacts.

The frontend polls ``GET /health?session_id=`` every 10 s while a
response is streaming. When the runner crashes, the tunnel drops and
the health endpoint returns ``runner_online: false``. The next poll
flips ``streamStale`` in the chat store, which swaps the "Working…"
shimmer for an "Agent is unresponsive" banner.

This test exercises the full chain: SPA → SSE stream → health poll →
banner render. It kills the runner subprocess with SIGKILL while the
LLM is processing, so the tunnel drops instantly — no graceful
shutdown, no terminal SSE event.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time

import httpx
from playwright.sync_api import Page, expect


def _find_runner_pids() -> list[int]:
    """
    Find all PIDs running the runner entry point
    (``omnigent.runner._entry``).

    The runner is a sibling subprocess of the server (both spawned
    by the test fixture), so we search by command-line pattern
    rather than by parent PID.

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


def test_stale_banner_on_runner_crash(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """
    Open a pre-created session, send a message, kill the runner while
    the LLM is thinking, and assert the "unresponsive" banner replaces
    "Working…".

    Starts from ``/c/<id>`` instead of ``/`` because the home route no
    longer renders a composer — see :func:`seeded_session`.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` of a pre-created
        session bound to the running runner.
    """
    live_server, session_id = seeded_session
    page.goto(f"{live_server}/c/{session_id}")

    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill("Write a 500-word essay about the history of computing.")
    page.get_by_role("button", name="Send", exact=True).click()

    # URL should still match /c/<session_id>.
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}"), timeout=15_000)

    # Wait for the "Working…" shimmer — proves the SSE stream opened
    # and the agent started processing.
    working = page.locator('[data-testid="working-indicator"]')
    expect(working).to_be_visible(timeout=15_000)

    # Verify the health endpoint reports online before the kill.
    health_before = httpx.get(
        f"{live_server}/health?session_id={session_id}",
        timeout=5,
    ).json()  # /health — no auth needed
    assert health_before.get("session", {}).get("runner_online") is True, (
        f"Health endpoint should report runner_online=true before kill, got: {health_before}"
    )

    # Kill the runner (sibling of the server, not a child).
    runner_pids = _find_runner_pids()
    assert runner_pids, (
        "No runner processes found. Process tree:\n"
        + subprocess.run(
            ["ps", "-ef"],
            capture_output=True,
            text=True,
        ).stdout[:2000]
    )
    for pid in runner_pids:
        os.kill(pid, signal.SIGKILL)

    # Poll until the health endpoint reports offline (tunnel teardown
    # is async — the server's WS route needs to notice the close and
    # deregister). 10 retries × 0.5 s = 5 s budget.
    health_after: dict[str, object] = {}
    for _attempt in range(10):
        time.sleep(0.5)
        health_after = httpx.get(
            f"{live_server}/health?session_id={session_id}",
            timeout=5,
        ).json()
        if health_after.get("session", {}).get("runner_online") is False:
            break
    assert health_after.get("session", {}).get("runner_online") is False, (
        f"Health endpoint should report runner_online=false after kill, got: {health_after}"
    )

    # The health poller fires every 10 s. No grace period — the
    # indicator flips on the next poll. Budget 20 s from the kill.
    indicator = page.locator('[data-testid="disconnected-indicator"]')
    expect(indicator).to_be_visible(timeout=20_000)
    expect(indicator).to_contain_text("disconnected")
