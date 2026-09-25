"""Check that opening an offline-runner session does not fan out resource 503s."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page

# Include the initial fetches and first query retries.
_OBSERVE_WINDOW_MS = 10_000
_OFFLINE_POLL_ATTEMPTS = 20
_OFFLINE_POLL_INTERVAL_S = 0.5


def _find_runner_pids() -> list[int]:
    """Find only this pytest process's child runners before killing them."""
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
    request: pytest.FixtureRequest,
    _recover_shared_runner: Callable[[], None],
) -> None:
    """Hold runner-proxied requests while an offline session opens."""
    base_url, session_id = seeded_session

    # Bypass ambient proxies for local health probes.
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
    request.addfinalizer(_recover_shared_runner)
    for pid in runner_pids:
        os.kill(pid, signal.SIGKILL)

    # Open the page only after the server reports the tunnel down.
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

    # Keep a timeline of session requests and health polls for failures.
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
        "runner-proxied resource request(s) that 503'd (each also lands a "
        "per-request server-side WARN). The runner-online gate must hold "
        "these fetches until the runner is known online. Timeline of "
        f"session-scoped responses:\n{timeline}"
    )
