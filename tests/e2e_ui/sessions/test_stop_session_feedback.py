"""Browser e2e: a confirmed host-bound session stop has visible feedback.

Use a real host daemon and runner, then check the UI after the runner drops.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, expect

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Allow for host registration and runner cold boot on a busy CI worker.
_HOST_ONLINE_TIMEOUT_S = 60.0
_RUNNER_ONLINE_TIMEOUT_S = 120.0
_RUNNER_OFFLINE_TIMEOUT_S = 60.0
_POLL_S = 0.5

# The frontend health poll runs about every 10s.
_FEEDBACK_TIMEOUT_S = 30.0

# Keep the end-to-end assertion independent of the exact UI copy.
_STOPPED_COPY = re.compile(r"stopped|asleep|not running|disconnected|offline", re.IGNORECASE)


def _client(base_url: str) -> httpx.Client:
    """HTTP client pinned to the test server, ignoring ambient proxy env."""
    return httpx.Client(base_url=base_url, timeout=30.0, trust_env=False)


def _spawn_host_daemon(tmp_path: Path, live_server: str) -> subprocess.Popen[bytes]:
    """Spawn an isolated host daemon whose runners import this checkout."""
    import os as _os

    env = _os.environ.copy()
    # Runners inherit this absolute PYTHONPATH after changing directory.
    env["PYTHONPATH"] = _os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
    )
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    data_dir = tmp_path / "omnigent-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _online_host_id(client: httpx.Client) -> str:
    deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(_POLL_S)
    raise AssertionError(f"No host came online within {_HOST_ONLINE_TIMEOUT_S}s")


def _hello_world_agent_id(client: httpx.Client) -> str:
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json().get("data", []) if a.get("name") == "hello_world"),
        None,
    )
    assert agent_id is not None, "hello_world agent not registered on the test server"
    return str(agent_id)


def _wait_runner_online(client: httpx.Client, session_id: str, *, online: bool) -> None:
    timeout = _RUNNER_ONLINE_TIMEOUT_S if online else _RUNNER_OFFLINE_TIMEOUT_S
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        resp = client.get("/health", params={"session_id": session_id})
        if resp.status_code == 200:
            last = resp.json().get("session", {})
            if last.get("runner_online") is online:  # type: ignore[union-attr]
                return
        time.sleep(_POLL_S)
    raise AssertionError(
        f"runner_online never became {online} within {timeout}s; last health: {last!r}"
    )


def _sidebar_row(page: Page, session_id: str) -> Locator:
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _visible_stop_feedback(page: Page) -> list[str]:
    """Find visible stop feedback, excluding unrelated files-panel status."""
    texts: list[str] = []
    for loc in page.get_by_text(_STOPPED_COPY).all():
        try:
            if not loc.is_visible():
                continue
            if loc.evaluate("el => !!el.closest('[class*=\"filespanel\"]')"):
                continue
            texts.append(loc.inner_text().strip()[:120])
        except PlaywrightError:
            continue  # Element went stale between polls.
    return texts


@pytest.fixture
def host_bound_session(
    live_server: str,
    tmp_path: Path,
) -> Iterator[tuple[str, str]]:
    """Create a real host-spawned runner with a stoppable sidebar row."""
    client = _client(live_server)
    daemon = _spawn_host_daemon(tmp_path, live_server)
    session_id: str | None = None
    try:
        host_id = _online_host_id(client)
        agent_id = _hello_world_agent_id(client)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        create = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = str(create.json()["id"])
        # Ensure the UI begins with a live runner, not startup grace.
        _wait_runner_online(client, session_id, online=True)
        yield live_server, session_id
    finally:
        # Reap the runner and daemon even if the browser assertion fails.
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={"type": "stop_session", "data": {}},
                    timeout=30.0,
                )
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        client.close()


# Cold host registration and runner startup can exceed the e2e default timeout.
@pytest.mark.timeout(420)
def test_stop_session_shows_stopped_state(
    page: Page,
    host_bound_session: tuple[str, str],
) -> None:
    """Confirm a sidebar stop, then find visible feedback after runner teardown."""
    base_url, session_id = host_bound_session

    # Let the frontend observe the live runner before stopping it.
    with page.expect_response(
        lambda r: "/health" in r.url and "session_id" in r.url and r.status == 200,
        timeout=30_000,
    ):
        page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()

    with _client(base_url) as client:
        health = client.get("/health", params={"session_id": session_id}).json()
        assert health.get("session", {}).get("runner_online") is True, (
            f"runner should be online before the stop, got: {health}"
        )

        row = _sidebar_row(page, session_id)
        expect(row).to_be_visible()
        row.hover()
        row.get_by_test_id("conversation-actions").click()
        stop_item = page.get_by_test_id("stop-conversation")
        expect(stop_item).to_be_visible()
        stop_item.click()
        confirm = page.get_by_test_id("stop-session-confirm")
        expect(confirm).to_be_visible()
        confirm.click()

        expect(confirm).not_to_be_visible(timeout=30_000)

        _wait_runner_online(client, session_id, online=False)

    deadline = time.monotonic() + _FEEDBACK_TIMEOUT_S
    feedback: list[str] = []
    while time.monotonic() < deadline:
        feedback = _visible_stop_feedback(page)
        if feedback:
            break
        page.wait_for_timeout(int(_POLL_S * 1000))
    assert feedback, (
        "After the stop landed (runner_online=false) and the UI had "
        f"{_FEEDBACK_TIMEOUT_S:.0f}s to observe it, no visible stopped/asleep/"
        "not-running indication rendered on any stop-relevant surface — the "
        "stopped session is indistinguishable from a running one."
    )
