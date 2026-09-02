"""Browser e2e: stopping a session must visibly say it is stopped.

Journey: connect a real ``omnigent host`` daemon, create a host-bound session
(the only kind whose sidebar kebab offers "Stop session"), open it, stop it
via kebab -> "Stop session" -> confirm, and wait for the stop to land (the
host tears the session's dedicated runner down, ``/health`` flips
``runner_online`` to false).

The bug: after the stop lands, nothing on any stop-relevant surface says the
session is stopped. The confirm dialog closes silently, the chat surface
renders no banner (a host-up + runner-down session classifies as
``runner_asleep``, for which ``ConnectionIndicator`` deliberately renders
nothing), the composer keeps its normal "Send a message..." placeholder, the
sidebar row shows no stopped-state badge (``SessionStateBadge`` has no such
state), and no toast fires. The stopped session is indistinguishable from a
running one.

The only copy matching a stopped/asleep wording anywhere on the page is the
files panel's incidental "Asleep" (host-served files) pill, which describes
file serving rather than the stop and is invisible whenever the files panel
is closed — the final assertion excludes the files panel subtree for that
reason.

The final assertion encodes the fixed behavior: once the frontend has
observed the runner offline, SOME visible stopped/asleep indication must
appear on a surface the stopping user is looking at (chat banner, composer
hint, sidebar badge, toast). It is deliberately copy-agnostic (any wording
that communicates "not running" passes) so it survives whatever copy the fix
chooses.
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

# Host daemons register their tunnel within seconds; runner cold boot on the
# host adds more. Generous ceilings so a busy CI box doesn't false-fail.
_HOST_ONLINE_TIMEOUT_S = 60.0
_RUNNER_ONLINE_TIMEOUT_S = 120.0
_RUNNER_OFFLINE_TIMEOUT_S = 60.0
_POLL_S = 0.5

# How long the UI gets to render stop feedback after the stop lands. The
# frontend health poll runs every ~10s, so 30s is ample time to observe
# runner_online=false and paint whatever indication a fix adds.
_FEEDBACK_TIMEOUT_S = 30.0

# Any wording that communicates "this session is not running" satisfies the
# fixed behavior; the reproduction fails because NO such copy renders on any
# stop-relevant surface at all.
_STOPPED_COPY = re.compile(r"stopped|asleep|not running|disconnected|offline", re.IGNORECASE)


def _client(base_url: str) -> httpx.Client:
    """HTTP client pinned to the test server, ignoring ambient proxy env."""
    return httpx.Client(base_url=base_url, timeout=30.0, trust_env=False)


def _spawn_host_daemon(tmp_path: Path, live_server: str) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent host`` daemon pointed at the test server.

    Mirrors ``tests/e2e/test_host_opencode_native_e2e.py``. The daemon env
    carries the repo on PYTHONPATH (forwarded to spawned runners via the
    host's runner-env allowlist) and loopback NO_PROXY so the tunnel never
    routes through an ambient CI proxy.

    :param tmp_path: Per-test temp dir for the daemon log.
    :param live_server: Base URL of the spawned test server.
    :returns: The daemon process handle.
    """
    import os as _os

    env = _os.environ.copy()
    env["PYTHONPATH"] = f"{_REPO_ROOT}{_os.pathsep}{env.get('PYTHONPATH', '')}"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    # Isolate the daemon registry/pidfiles from any co-resident daemon.
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
    """Poll ``GET /v1/hosts`` until the daemon registers online."""
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
    """Find the pre-registered ``hello_world`` agent's id."""
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json().get("data", []) if a.get("name") == "hello_world"),
        None,
    )
    assert agent_id is not None, "hello_world agent not registered on the test server"
    return str(agent_id)


def _wait_runner_online(client: httpx.Client, session_id: str, *, online: bool) -> None:
    """Poll ``GET /health`` until the session's runner liveness matches."""
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
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _visible_stop_feedback(page: Page) -> list[str]:
    """Visible stopped/asleep/not-running copy outside the files panel.

    The files panel's "Asleep" (host-served files) pill and its
    ``RunnerAsleepHint`` describe file serving, not the stop, and are
    invisible whenever the files panel is closed — so the files panel
    subtree (root class ``@container/filespanel``) is excluded. Anything
    else that matches counts as genuine stop feedback.

    :param page: The Playwright page to scan.
    :returns: The matched visible texts (empty = no feedback rendered).
    """
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
    """A host-spawned session whose kebab offers "Stop session".

    Spawns a real ``omnigent host`` daemon against the test server and
    creates a ``hello_world`` session bound to it, so the host launches a
    dedicated runner for the session — the exact shape whose sidebar row
    is stoppable (``isSessionStoppable``: host_id + runner_id).

    :param live_server: Spawned server fixture (base URL).
    :param tmp_path: Per-test temp dir (daemon log, isolated data dir,
        session workspace).
    :returns: ``(base_url, session_id)`` for the host-bound session.
    """
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
        # The host cold-boots a dedicated runner; wait until its tunnel is
        # live so the UI reads a genuinely RUNNING session before the stop.
        _wait_runner_online(client, session_id, online=True)
        yield live_server, session_id
    finally:
        # Best-effort: stop the session's runner so no orphan outlives the
        # test, then bring the daemon down (it reaps its children).
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


# Opt out of the e2e workflows' stricter --timeout=180: the fixture's host
# registration + runner cold-boot ceilings alone can exceed it on a cold box.
@pytest.mark.timeout(420)
def test_stop_session_shows_stopped_state(
    page: Page,
    host_bound_session: tuple[str, str],
) -> None:
    """Stopping a session via the sidebar kebab must visibly say it stopped.

    Drives the exact user journey of the report: open a running host-bound
    session, stop it from the sidebar kebab, confirm, and look at the screen.

    The journey up to the stop landing is all verified to WORK (kebab offers
    the item, the confirm dialog closes, ``/health`` flips
    ``runner_online=false``) — the reproduction is the final assertion: after
    the frontend has had ample time to observe the stop, no visible
    stopped/asleep/not-running indication exists on any stop-relevant
    surface (the files panel's incidental host-served "Asleep" pill is
    excluded; see :func:`_visible_stop_feedback`).

    :param page: Playwright page fixture (fresh context per test).
    :param host_bound_session: ``(base_url, session_id)`` for a running
        host-bound session.
    """
    base_url, session_id = host_bound_session

    # Wait for a successful /health poll so the frontend has observed the
    # runner ONLINE before the stop — a fresh session inside its cold-boot
    # grace would otherwise mask the post-stop state as `starting`.
    with page.expect_response(
        lambda r: "/health" in r.url and "session_id" in r.url and r.status == 200,
        timeout=30_000,
    ):
        page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()

    # Sanity: the session really is running before the stop.
    with _client(base_url) as client:
        health = client.get("/health", params={"session_id": session_id}).json()
        assert health.get("session", {}).get("runner_online") is True, (
            f"runner should be online before the stop, got: {health}"
        )

        # The user journey: sidebar row -> kebab -> "Stop session" -> confirm.
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

        # The dialog closes on success — silently (this is part of the bug:
        # closing is the only acknowledgement the user gets).
        expect(confirm).not_to_be_visible(timeout=30_000)

        # The stop lands server-side: the host kills the session's runner and
        # its tunnel drops.
        _wait_runner_online(client, session_id, online=False)

    # THE BUG: nothing on the stop-relevant surfaces says the
    # session is stopped. The chat surface renders no banner (host-up +
    # runner-down classifies as `runner_asleep`, for which
    # `ConnectionIndicator` deliberately renders nothing), the composer keeps
    # its normal placeholder, the sidebar row has no stopped-state badge
    # (`SessionStateBadge` has no such state), and no toast fires. A genuine
    # fix renders stop feedback the stopping user can see — any visible copy
    # matching _STOPPED_COPY outside the files panel. Today NOTHING renders,
    # so this polls out.
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
