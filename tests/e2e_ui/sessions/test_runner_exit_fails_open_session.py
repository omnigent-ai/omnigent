"""Browser journey: a host runner process that dies under an open session fails it."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.host_daemon import (
    launched_runners,
    spawn_host_daemon,
    terminate_host_daemon,
    wait_for_host_online,
)
from tests.e2e_ui.conftest import _register_extra_agent

_RUNNER_EXIT_TEXT = re.compile(r"runner process exited")


def _hold_for_recording(page: Page, ms: int) -> None:
    """Pause only while filming, so the clip shows the state; plain runs skip it."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(ms)


def _runner_online(base_url: str, runner_id: str) -> bool:
    """Return the server's ``online`` verdict for a runner tunnel."""
    try:
        resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=5.0)
    except httpx.HTTPError:
        return False
    return bool(resp.status_code == 200 and resp.json().get("online"))


def _session_status(base_url: str, session_id: str) -> str | None:
    """Return the server's current status for *session_id*."""
    try:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("status")


def _create_host_session(base_url: str, host_id: str, agent_name: str, workspace: Path) -> str:
    """Register a smoke agent and create a session the host must serve."""
    agent_id = _register_extra_agent(base_url, agent_name, "You are a terse smoke-test assistant.")
    assert agent_id is not None
    workspace.mkdir()
    create = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
        timeout=90.0,
    )
    create.raise_for_status()
    return create.json()["id"]


def _wait_for_failed(base_url: str, session_id: str, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _session_status(base_url, session_id) != "failed":
        time.sleep(0.25)
    assert _session_status(base_url, session_id) == "failed", (
        "session never transitioned to failed after the runner process exit"
    )


@pytest.mark.timeout(300)
def test_host_runner_exit_fails_open_session(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A killed host runner fails the open session; the banner names the exit."""
    daemon: subprocess.Popen[bytes] | None = None
    try:
        daemon, host_id, daemon_log = spawn_host_daemon(tmp_path, live_server, mock_llm_server_url)
        wait_for_host_online(live_server, host_id)
        session_id = _create_host_session(
            live_server, host_id, "runner-exit-agent", tmp_path / "project"
        )

        # Wait for the launched runner to connect its tunnel, keeping the PID from
        # the same launch record so a later relaunch cannot be killed by mistake.
        deadline = time.monotonic() + 60.0
        connected: tuple[str, int] | None = None
        while time.monotonic() < deadline:
            launches = launched_runners(daemon_log)
            if launches and _runner_online(live_server, launches[-1][0]):
                connected = launches[-1]
                break
            time.sleep(0.25)
        assert connected is not None, "runner never connected its tunnel"
        _runner_id, pid = connected

        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)

        # Kill the connected runner process: a stand-in for any non-zero exit.
        os.kill(pid, signal.SIGKILL)

        _wait_for_failed(live_server, session_id)

        # Reload so the SPA renders the failed snapshot's synthesized error block.
        page.reload()
        error_pill = page.get_by_test_id("error-pill")
        expect(error_pill).to_be_visible(timeout=30_000)
        error_pill.click()
        message = page.get_by_test_id("error-message-content")
        expect(message).to_contain_text(_RUNNER_EXIT_TEXT, timeout=15_000)
        _hold_for_recording(page, 2_500)
    finally:
        terminate_host_daemon(daemon)
