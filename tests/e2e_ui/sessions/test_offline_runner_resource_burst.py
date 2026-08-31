"""E2E: opening an offline-runner session must not fan out resource 503s.

The ``useSessionRunnerOnline`` gate that stops the steady-state resource-poll
storm on an offline-runner session is tri-state: ``undefined`` (not yet
polled) / ``true`` / ``false``, and consumers only block on ``=== false``. So
while the first ``/health`` poll is still in flight after opening the session,
the environment/terminal resource fetches fire anyway — and every one of them
comes back 503 ``runner_unavailable`` (each also logged server-side as an
ERROR + full traceback, the other facet of this bug).

This drives the real journey: bind a session to the live runner, SIGKILL the
runner (host reboot / idle-reap), open ``/c/<id>`` in the browser, and record
every session-scoped response for a window after load. The regression
assertion is that NO runner-proxied ``/resources/`` request 503s during the
open — the client must hold those fetches until the runner is known online.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page

# How long to watch network traffic after the session page loads. Long
# enough to cover the initial fan-out AND the first react-query retries
# (observed at ~1s and ~3s after load), short enough to keep the test fast.
_OBSERVE_WINDOW_MS = 10_000
_OFFLINE_POLL_ATTEMPTS = 20
_OFFLINE_POLL_INTERVAL_S = 0.5


def _find_runner_pids() -> list[int]:
    """Find this test run's runner PIDs (``omnigent.runner._entry``).

    The fixture spawns the runner as a child of the pytest process, so scope
    the command-line match to our own children (``-P``) — a bare ``pgrep -f``
    would match (and get killed as) any other runner on the machine.

    :returns: List of runner PIDs (may be empty).
    """
    result = subprocess.run(
        ["pgrep", "-P", str(os.getpid()), "-f", "omnigent[.]runner[.]_entry"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [int(line.strip()) for line in result.stdout.strip().splitlines() if line.strip()]


def test_open_offline_session_no_resource_503_burst(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Opening a session whose runner is offline must not 503-storm.

    Kills the session's runner, waits until the server reports it offline
    (so this is the steady "user comes back after a host reboot" open, with
    no race about the server's own knowledge), then loads ``/c/<id>`` and
    records every response for the session. Any 503 from a runner-proxied
    ``/resources/`` endpoint during the observation window is the bug: the
    client fired a fetch the online-gate should have held.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` of a pre-created
        session bound to the running runner.
    """
    base_url, session_id = seeded_session

    # Verify the runner is online before the kill, so the offline state
    # below is unambiguously produced by this test.
    # trust_env=False: the health probes target the local test server and
    # must bypass any ambient HTTP(S)_PROXY (which can't reach loopback).
    health_before = httpx.get(
        f"{base_url}/health",
        params={"session_id": session_id},
        timeout=5,
        trust_env=False,
    ).json()
    assert health_before.get("session", {}).get("runner_online") is True, (
        f"expected runner_online=true before kill, got: {health_before}"
    )

    runner_pids = _find_runner_pids()
    assert runner_pids, "no runner process found to kill"
    for pid in runner_pids:
        os.kill(pid, signal.SIGKILL)

    # Wait until the server has deregistered the tunnel and reports the
    # runner offline — the user's "open a dead session" moment starts here.
    health_after: dict[str, object] = {}
    for _attempt in range(_OFFLINE_POLL_ATTEMPTS):
        time.sleep(_OFFLINE_POLL_INTERVAL_S)
        health_after = httpx.get(
            f"{base_url}/health",
            params={"session_id": session_id},
            timeout=5,
            trust_env=False,
        ).json()
        if health_after.get("session", {}).get("runner_online") is False:
            break
    assert health_after.get("session", {}).get("runner_online") is False, (
        f"server never reported the killed runner offline: {health_after}"
    )

    # Record every response for this session (and the health polls, to show
    # the gate's timeline in the failure message) while the page opens.
    observed: list[tuple[float, int, str]] = []
    t0 = time.monotonic()

    def _on_response(response) -> None:
        path = urlparse(response.url).path
        query = urlparse(response.url).query
        if f"/v1/sessions/{session_id}/" in response.url or path == "/health":
            observed.append(
                (
                    round(time.monotonic() - t0, 2),
                    response.status,
                    f"{path}?{query}" if query else path,
                )
            )

    page.on("response", _on_response)
    page.goto(f"{base_url}/c/{session_id}")
    page.wait_for_timeout(_OBSERVE_WINDOW_MS)

    resource_503s = [
        (when, status, path)
        for when, status, path in observed
        if f"/v1/sessions/{session_id}/resources/" in path and status == 503
    ]
    timeline = "\n".join(f"  t={when:6.2f}s  {status}  {path}" for when, status, path in observed)
    assert not resource_503s, (
        f"opening an offline-runner session fired {len(resource_503s)} "
        "runner-proxied resource request(s) that 503'd (each is also logged "
        "server-side as ERROR + traceback). The runner-online gate must hold "
        "these fetches until the runner is known online. Timeline of "
        f"session-scoped responses:\n{timeline}"
    )
