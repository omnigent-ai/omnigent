"""E2E: a reused background host launches runners with the latest CLI environment."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, OMNIGENT_SESSION_ENV_VAR
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _connect_env,
    _read_local_server_record,
)
from tests.e2e.omnigent.test_host_daemon_telemetry_env_e2e import (
    _BOOT_TIMEOUT,
    _RUNNER_APPEAR_TIMEOUT,
    _online_host_id,
    _proc_environ,
    _sigterm,
    _spawn_background_daemon,
    _wait_for_daemon_record,
    _wait_for_runner_pid,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="reads /proc/<pid>/environ to observe the live process environments",
)

_PASSTHROUGH = "ANTHROPIC_CUSTOM_HEADERS,ISAAC_LAUNCH_MODE"
_UPDATED_ENV = {"ANTHROPIC_CUSTOM_HEADERS": "X-Launch: second", "ISAAC_LAUNCH_MODE": "omni"}


def test_reused_background_host_launches_runner_with_updated_environment(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Values changed or exported after the host started reach later runners."""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = _connect_env(mock_credentials_env, home)
    env.pop(OMNIGENT_SESSION_ENV_VAR, None)
    env["OMNIGENT_HOST_ID"] = uuid.uuid4().hex
    env["OMNIGENT_HOST_NAME"] = "env-refresh-e2e"
    # /proc shows exec-time env; a zygote-forked runner never re-execs.
    env["OMNIGENT_RUNNER_ZYGOTE"] = "0"
    env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = _PASSTHROUGH
    env["ANTHROPIC_CUSTOM_HEADERS"] = "X-Launch: first"
    env.pop("ISAAC_LAUNCH_MODE", None)
    first = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert first.returncode == 0, (
        f"background spawn failed (rc={first.returncode}):\n{first.stderr}"
    )

    daemon_pid = server_pid = runner_pid = -1
    try:
        daemon = _wait_for_daemon_record(home, timeout=_BOOT_TIMEOUT)
        daemon_pid = daemon.pid
        server_pid, port = _read_local_server_record(home)

        env.update(_UPDATED_ENV)
        second = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
        assert second.returncode == 0, (
            f"second spawn failed (rc={second.returncode}):\n{second.stderr}"
        )
        assert "Host daemon already running" in second.stdout, second.stdout
        assert _wait_for_daemon_record(home, timeout=_BOOT_TIMEOUT).pid == daemon_pid

        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=30.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as client:
            host_id = _online_host_id(client, host_id=daemon.host_id, timeout=_BOOT_TIMEOUT)
            bundle = bundle_files(
                {
                    "agent.yaml": b"name: env-refresh\n"
                    b"executor:\n  harness: openai-agents\n  model: mock-model\n"
                    b"prompt: Reply briefly.\n"
                }
            )
            create = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle,
                metadata={"host_id": host_id, "workspace": str(workspace)},
                timeout=60.0,
            )
            create.raise_for_status()

        runner_pid = _wait_for_runner_pid(
            workspace, daemon_pid=daemon_pid, timeout=_RUNNER_APPEAR_TIMEOUT
        )
        runner_env = _proc_environ(runner_pid)
        assert runner_env.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH") == _PASSTHROUGH
        delivered = {name: runner_env.get(name) for name in _UPDATED_ENV}
        assert delivered == _UPDATED_ENV, (
            f"runner kept the reused host's start-time environment: {delivered}"
        )
    finally:
        for pid in (runner_pid, daemon_pid, server_pid):
            if pid > 0:
                _sigterm(pid)
