"""A host runner that dies at boot or idles out, through a real host, server and runner."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.host_daemon import (
    await_launched_runner,
    pid_alive,
    spawn_host_daemon,
    terminate_host_daemon,
    wait_for_host_online,
)
from tests._helpers.runner_faults import disk_full_spec_cache_pythonpath
from tests.e2e.conftest import lookup_agent_id, upload_agent
from tests.e2e.test_host_e2e import _write_smoke_agent_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOST_ONLINE_TIMEOUT_S = 30.0
_LAUNCH_TIMEOUT_S = 60.0
_RUNNER_ONLINE_TIMEOUT_S = 30.0
_SESSION_FAILED_TIMEOUT_S = 60.0
_RUNNER_EXIT_MESSAGE = "runner process exited"
_TAIL_SEPARATOR = "\n--- runner log tail ---\n"


def _launch_and_bind_runner(
    client: httpx.Client,
    *,
    host_id: str,
    session_id: str,
    workspace: Path,
) -> str:
    """Launch a runner on the host, wait for it online, and bind it."""
    launch = client.post(
        f"/v1/hosts/{host_id}/runners",
        json={"session_id": session_id, "workspace": str(workspace)},
        timeout=_LAUNCH_TIMEOUT_S,
    )
    assert launch.status_code == 200, f"launch failed: {launch.status_code} {launch.text}"
    runner_id = launch.json()["runner_id"]

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        status = client.get(f"/v1/runners/{runner_id}/status")
        if status.status_code == 200 and status.json().get("online") is True:
            break
        time.sleep(0.5)
    else:
        raise AssertionError(f"runner {runner_id} never came online after launch")

    client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id}).raise_for_status()
    return runner_id


def _poll_session(
    client: httpx.Client,
    session_id: str,
    *,
    until_status: str,
    timeout: float,
) -> dict:
    """Poll GET /v1/sessions/{id} until it reaches *until_status* or times out."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        if resp.status_code == 200:
            last = resp.json()
            if last.get("status") == until_status:
                return last
        time.sleep(0.5)
    return last


def _create_host_bound_session(
    client: httpx.Client,
    tmp_path: Path,
) -> tuple[str, Path]:
    """Upload the smoke agent and create a session, returning (session_id, workspace)."""
    agent_name = upload_agent(client, _write_smoke_agent_yaml(tmp_path))
    agent_id = lookup_agent_id(client, agent_name)
    create = client.post("/v1/sessions", json={"agent_id": agent_id})
    create.raise_for_status()
    workspace = tmp_path / "project"
    workspace.mkdir()
    return create.json()["id"], workspace


# The cause line is composed by the host, new in 0.18.0.
@pytest.mark.min_runner_version("0.18.0")
@pytest.mark.timeout(300)
def test_runner_boot_crash_names_the_cause_before_the_log_tail(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A runner that crashes at boot fails its session with the cause stated
    before the raw log tail, not at the bottom of a traceback."""
    daemon, host_id, _daemon_log = spawn_host_daemon(
        tmp_path,
        live_server,
        mock_llm_server_url,
        pythonpath=disk_full_spec_cache_pythonpath(
            tmp_path / "fault", _REPO_ROOT, os.environ.get("PYTHONPATH")
        ),
        zygote=False,
    )
    try:
        wait_for_host_online(live_server, host_id, timeout=_HOST_ONLINE_TIMEOUT_S)
        agent_name = upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(http_client, agent_name)
        workspace = tmp_path / "project"
        workspace.mkdir()
        create = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=90.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        body = _poll_session(
            http_client,
            session_id,
            until_status="failed",
            timeout=_SESSION_FAILED_TIMEOUT_S,
        )
        assert body.get("status") == "failed", f"session never failed: {body}"
        error = body.get("last_task_error") or {}
        assert error.get("code") == "runner_failed_to_start", error
        message = error.get("message") or ""
        headline, separator, tail = message.partition(_TAIL_SEPARATOR)
        assert separator, f"report carries no runner log tail: {message}"
        assert headline.startswith(_RUNNER_EXIT_MESSAGE), message
        # The reason the runner recorded leads the report ...
        assert "No space left on device" in headline, message
        # ... and the raw traceback, with the crash site, is still attached below it.
        assert "in create_app" in tail, message
    finally:
        terminate_host_daemon(daemon)


@pytest.mark.timeout(300)
def test_clean_idle_shutdown_does_not_fail_session(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A graceful post-connect code-0 idle-reaper exit must not fail the session."""
    daemon, host_id, daemon_log = spawn_host_daemon(
        tmp_path, live_server, mock_llm_server_url, runner_idle_timeout_s=5.0
    )
    try:
        wait_for_host_online(live_server, host_id, timeout=_HOST_ONLINE_TIMEOUT_S)
        session_id, workspace = _create_host_bound_session(http_client, tmp_path)
        _launch_and_bind_runner(
            http_client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        _, runner_pid = await_launched_runner(daemon_log)

        # Let the idle reaper exit the connected runner with code 0.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and pid_alive(runner_pid):
            time.sleep(0.5)
        assert not pid_alive(runner_pid), "runner never idle-exited within the window"

        deadline = time.monotonic() + 15.0
        while (
            time.monotonic() < deadline
            and "exited cleanly (code 0)" not in daemon_log.read_text(errors="replace")
        ):
            time.sleep(0.25)
        log_text = daemon_log.read_text(errors="replace")
        assert "exited cleanly (code 0)" in log_text, log_text[-2000:]
        assert "no crash report" in log_text, log_text[-2000:]

        # The clean shutdown must leave the session unfailed.
        body = http_client.get(f"/v1/sessions/{session_id}").json()
        assert body.get("status") != "failed", f"clean idle exit wrongly failed session: {body}"
    finally:
        terminate_host_daemon(daemon)
