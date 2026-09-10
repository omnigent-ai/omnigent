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
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, configure_mock_llm


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


@pytest.mark.compat_smoke
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

    composer = page.get_by_placeholder("Send a message…")
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


@pytest.mark.nightly
def test_silent_stream_stall_self_heals(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Freeze the server mid-view so the chat stream goes byte-silent
    without closing, and assert the client notices and reconnects.

    SIGSTOP models the half-open socket an app ingress leaves behind
    when it reaps an idle connection without a close frame: heartbeats
    stop but the client's ``reader.read()`` never resolves. The 45 s
    stall guard must declare the transport dead (console warning) and
    issue a fresh ``/stream`` open — without the guard the tab waits
    forever and only a manual reload recovers. After SIGCONT the
    reconnect completes and a real turn must still round-trip.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` of a pre-created
        session bound to the running runner.
    :param mock_llm_server_url: Mock LLM base URL, to script the
        post-recovery turn.
    """
    live_server, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "back from the stall"}],
        key="silent-stall-recover",
        match="Say hello",
    )
    stream_path = f"/v1/sessions/{session_id}/stream"

    # Listeners attach before goto so the initial open can't be missed.
    stream_opens: list[str] = []
    stall_warnings: list[str] = []
    page.on(
        "request",
        lambda req: (
            stream_opens.append(req.url) if urlparse(req.url).path == stream_path else None
        ),
    )
    page.on(
        "console",
        lambda msg: stall_warnings.append(msg.text) if "no stream bytes" in msg.text else None,
    )

    page.goto(f"{live_server}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()
    for _ in range(100):
        if stream_opens:
            break
        page.wait_for_timeout(100)
    assert stream_opens, "the chat page never opened its live stream"

    # Freeze the whole server process: the established stream stays open
    # but goes byte-silent — no events, no heartbeats, no close.
    server_pid = int(_server_state["pid"])
    os.kill(server_pid, signal.SIGSTOP)
    try:
        # The stall guard trips after 45 s of silence; poll to 70 s.
        # wait_for_timeout (not time.sleep) keeps dispatching this
        # page's console/request callbacks while we wait.
        for _ in range(700):
            if stall_warnings:
                break
            page.wait_for_timeout(100)
    finally:
        os.kill(server_pid, signal.SIGCONT)
    assert stall_warnings, (
        "the client never declared the byte-silent stream dead within 70s -- "
        "without the stall guard it blocks in reader.read() forever and the "
        "transcript freezes until a manual reload"
    )

    # The recycle issues a fresh /stream open (it may have been pending
    # since the freeze — the request itself is the reconnect signal).
    for _ in range(150):
        if len(stream_opens) >= 2:
            break
        page.wait_for_timeout(100)
    assert len(stream_opens) >= 2, "the stall guard tripped but no reconnect was attempted"

    # The freeze also severed the runner tunnel; wait for it to
    # re-register before proving the chat is usable end-to-end.
    runner_id = str(_server_state["runner_id"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status = httpx.get(f"{live_server}/v1/runners/{runner_id}/status", timeout=2)
            if status.status_code == 200 and status.json().get("online") is True:
                break
        except httpx.HTTPError:
            pass
        page.wait_for_timeout(500)

    composer = page.get_by_placeholder("Send a message…")
    composer.fill("Say hello")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    ).to_be_visible(timeout=15_000)
