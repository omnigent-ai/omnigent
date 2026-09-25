"""A dead host runner fails its bound session with the cause (real host/server/runner)."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import (
    apply_runner_env,
    compat_runner_cwd,
    compat_runner_python,
    runner_executable,
)
from tests._helpers.runner_faults import disk_full_spec_cache_pythonpath
from tests.e2e.conftest import lookup_agent_id, upload_agent
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _runner_pid_from_daemon_log,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOST_ONLINE_TIMEOUT_S = 30.0
_LAUNCH_TIMEOUT_S = 60.0
_RUNNER_ONLINE_TIMEOUT_S = 30.0
_SESSION_FAILED_TIMEOUT_S = 60.0
_RUNNER_EXIT_MESSAGE = "runner process exited"
_TAIL_SEPARATOR = "\n--- runner log tail ---\n"


def _spawn_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
    mock_llm_server_url: str,
    runner_idle_timeout_s: float | None = None,
    pythonpath: str | None = None,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Start an isolated host daemon with a unique host_id, optionally idle-reaping
    runners after *runner_idle_timeout_s* and running under *pythonpath*."""
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    config: dict[str, object] = {
        "host": {"host_id": host_id, "name": f"runner-exit-{host_id[:8]}"}
    }
    if runner_idle_timeout_s is not None:
        config["runner"] = {"idle_timeout_s": runner_idle_timeout_s}
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(config, default_flow_style=False, sort_keys=True)
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    env = apply_runner_env(env)
    if pythonpath is not None:
        # Direct spawn so the fault lands in the runner process itself.
        env["OMNIGENT_RUNNER_ZYGOTE"] = "0"
        env["PYTHONPATH"] = pythonpath
    elif compat_runner_python() is None:
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=env,
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


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


def _await_runner_pid(daemon_log: Path, *, timeout: float = 15.0) -> int:
    """Return the launched runner's PID from the daemon log."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _runner_pid_from_daemon_log(daemon_log)
        if pid is not None:
            return pid
        time.sleep(0.2)
    raise AssertionError("daemon never logged a runner launch")


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


def _terminate(daemon: subprocess.Popen[bytes]) -> None:
    daemon.send_signal(signal.SIGTERM)
    try:
        daemon.wait(timeout=10)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait()


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


@pytest.mark.timeout(300)
def test_runner_process_exit_fails_bound_session(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A non-zero runner-process exit fails the bound session with its cause."""
    daemon, host_id, daemon_log = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        _wait_for_host_online(http_client, host_id, timeout=_HOST_ONLINE_TIMEOUT_S)
        session_id, workspace = _create_host_bound_session(http_client, tmp_path)
        _launch_and_bind_runner(
            http_client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        runner_pid = _await_runner_pid(daemon_log)
        assert _pid_alive(runner_pid), "runner exited before we could observe it connected"

        # A killed runner stands in for a non-zero runner-process exit.
        os.kill(runner_pid, signal.SIGKILL)

        body = _poll_session(
            http_client,
            session_id,
            until_status="failed",
            timeout=_SESSION_FAILED_TIMEOUT_S,
        )
        assert body.get("status") == "failed", f"session never failed: {body}"
        error = body.get("last_task_error") or {}
        message = error.get("message") or ""
        assert _RUNNER_EXIT_MESSAGE in message, f"unexpected last_task_error: {error}"
        assert error.get("code") == "runner_failed_to_start", error
    finally:
        _terminate(daemon)


@pytest.mark.min_runner_version("0.16.0")
@pytest.mark.timeout(300)
def test_runner_boot_crash_names_the_cause_before_the_log_tail(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A runner that crashes at boot fails its session with the cause stated
    before the raw log tail, not at the bottom of a traceback."""
    daemon, host_id, _daemon_log = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
        pythonpath=disk_full_spec_cache_pythonpath(
            tmp_path / "fault", _REPO_ROOT, os.environ.get("PYTHONPATH")
        ),
    )
    try:
        _wait_for_host_online(http_client, host_id, timeout=_HOST_ONLINE_TIMEOUT_S)
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
        assert _RUNNER_EXIT_MESSAGE in headline.splitlines()[0], message
        # The reason the runner recorded leads the report ...
        assert "No space left on device" in headline, message
        # ... and the raw traceback, with the crash site, is still attached below it.
        assert "in create_app" in tail, message
    finally:
        _terminate(daemon)


@pytest.mark.timeout(300)
def test_clean_idle_shutdown_does_not_fail_session(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A graceful post-connect code-0 idle-reaper exit must not fail the session."""
    daemon, host_id, daemon_log = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
        runner_idle_timeout_s=5.0,
    )
    try:
        _wait_for_host_online(http_client, host_id, timeout=_HOST_ONLINE_TIMEOUT_S)
        session_id, workspace = _create_host_bound_session(http_client, tmp_path)
        _launch_and_bind_runner(
            http_client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        runner_pid = _await_runner_pid(daemon_log)

        # Let the idle reaper exit the connected runner with code 0.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and _pid_alive(runner_pid):
            time.sleep(0.5)
        assert not _pid_alive(runner_pid), "runner never idle-exited within the window"

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
        _terminate(daemon)
