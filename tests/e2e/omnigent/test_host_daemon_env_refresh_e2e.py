"""E2E: the detached host forwards passthrough-named variables and launches later
runners from the latest CLI environment. Drives the real ``omnigent host
--background ""`` journey and reads live process envs from ``/proc/<pid>/environ``."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, RUNNER_PARENT_PID_ENV_VAR
from tests.e2e.conftest import lookup_agent_id, register_inline_agent
from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _connect_env,
    _read_local_server_record,
)
from tests.e2e.omnigent.test_host_daemon_gcloud_adc_env_e2e import (
    _online_host_id,
    _proc_environ,
    _sigterm,
    _spawn_background_daemon,
    _wait_for_daemon_pid,
    _wait_for_runner_env,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="reads /proc/<pid>/environ to observe the live process environments",
)

_BOOT_TIMEOUT = 90.0
_RUNNER_APPEAR_TIMEOUT = 60.0

# Gateway configuration of the kind the report names. Neither hop allowlists
# these names, so only the operator's passthrough list can carry them.
_PASSTHROUGH_VARS: dict[str, str] = {
    "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway-Tenant: acme",
    "HARNESS_CLAUDE_SDK_GATEWAY_URL": "https://gateway.example.test/anthropic",
}
_PASSTHROUGH_NAMES = ",".join(_PASSTHROUGH_VARS)

# Allowlisted at both hops by exact name / prefix, so a stale or missing value
# in a later runner can only come from the daemon's frozen first-launch env.
_FIRST_LAUNCH_ATTRIBUTES = "deployment.environment=first-launch"
_LATER_LAUNCH_VARS: dict[str, str] = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=second-launch",
}


def _host_env(base_env: dict[str, str], home: Path, *, host_id: str) -> dict[str, str]:
    """Shell env for ``omnigent host --background ""``; a stable *host_id* lets
    a second launch reuse the daemon instead of replacing it."""
    env = _connect_env(base_env, home)
    env["OMNIGENT_HOST_ID"] = host_id
    env["OMNIGENT_HOST_NAME"] = "runner-env-e2e"
    env["OTEL_METRICS_EXPORTER"] = "otlp"
    # /proc exposes exec-time env, so spawn runners directly instead of
    # forking them from the zygote.
    env["OMNIGENT_RUNNER_ZYGOTE"] = "0"
    return env


def _server_client(port: int) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=30.0,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )


def _register_agent(client: httpx.Client, *, mock_llm_base_url: str) -> str:
    return register_inline_agent(
        client,
        name="runner-env-e2e",
        harness="openai-agents",
        model="gpt-4o",
        profile="",
        prompt="Reply briefly.",
        mock_llm_base_url=mock_llm_base_url,
    )


def _launch_host_session(
    client: httpx.Client, *, host_id: str, agent_name: str, workspace: Path
) -> dict[str, str]:
    """Create a session bound to *host_id* and return its runner's environment."""
    workspace.mkdir()
    create = client.post(
        "/v1/sessions",
        json={
            "agent_id": lookup_agent_id(client, agent_name),
            "host_id": host_id,
            "workspace": str(workspace),
        },
        timeout=60.0,
    )
    create.raise_for_status()
    return _wait_for_runner_env(workspace, timeout=_RUNNER_APPEAR_TIMEOUT)


def _assert_env_chain_intact(runner_env: dict[str, str], *, daemon_pid: int) -> None:
    assert runner_env.get(RUNNER_PARENT_PID_ENV_VAR) == str(daemon_pid), (
        "the runner was not spawned by this test's daemon"
    )
    assert runner_env.get("OTEL_METRICS_EXPORTER") == "otlp", (
        "control var OTEL_METRICS_EXPORTER missing from the runner env -- the whole env "
        "chain is broken, not just the variables under test"
    )


def test_passthrough_named_variables_reach_daemon_spawned_runner(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Export gateway vars named in the passthrough -> ``omnigent host --background ""``
    -> host-bound session -> the daemon-spawned runner carries the named values."""
    home = tmp_path / "home"
    host_id = uuid.uuid4().hex
    env = _host_env(mock_credentials_env, home, host_id=host_id)
    env.update(_PASSTHROUGH_VARS)
    env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = _PASSTHROUGH_NAMES
    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = server_pid = runner_pid = -1
    try:
        daemon_pid = _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT)
        server_pid, port = _read_local_server_record(home)
        with _server_client(port) as client:
            _online_host_id(client, host_id=host_id, timeout=_BOOT_TIMEOUT)
            agent_name = _register_agent(client, mock_llm_base_url=env["OPENAI_BASE_URL"])
            runner_env = _launch_host_session(
                client, host_id=host_id, agent_name=agent_name, workspace=tmp_path / "ws"
            )
        runner_pid = int(runner_env["_OMNI_TEST_RUNNER_PID"])
        daemon_env = _proc_environ(daemon_pid)

        _assert_env_chain_intact(runner_env, daemon_pid=daemon_pid)
        assert runner_env.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH") == _PASSTHROUGH_NAMES, (
            "the passthrough control variable itself was dropped before the runner"
        )
        observed = {name: runner_env.get(name) for name in _PASSTHROUGH_VARS}
        in_daemon = {name: daemon_env.get(name) for name in _PASSTHROUGH_VARS}
        assert observed == _PASSTHROUGH_VARS, (
            f"variables named in OMNIGENT_RUNNER_ENV_PASSTHROUGH never reached the "
            f"daemon-spawned runner: expected {_PASSTHROUGH_VARS}, got {observed} "
            f"(the detached daemon itself carries {in_daemon}, so the CLI->daemon "
            f"hop dropped them before the passthrough could forward them)"
        )
    finally:
        for pid in (runner_pid, daemon_pid, server_pid):
            if pid > 0:
                _sigterm(pid)


def test_env_changes_after_first_launch_reach_later_runner(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Session A reflects the first launch -> export new values -> a second
    ``omnigent host --background ""`` reuses the daemon -> session B's runner
    carries the new values."""
    home = tmp_path / "home"
    host_id = uuid.uuid4().hex
    first_env = _host_env(mock_credentials_env, home, host_id=host_id)
    first_env.pop("CLAUDE_CODE_ENABLE_TELEMETRY", None)
    first_env["OTEL_RESOURCE_ATTRIBUTES"] = _FIRST_LAUNCH_ATTRIBUTES
    later_env = {**first_env, **_LATER_LAUNCH_VARS}
    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, first_env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = server_pid = runner_a_pid = runner_b_pid = -1
    try:
        daemon_pid = _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT)
        server_pid, port = _read_local_server_record(home)
        with _server_client(port) as client:
            _online_host_id(client, host_id=host_id, timeout=_BOOT_TIMEOUT)
            agent_name = _register_agent(client, mock_llm_base_url=first_env["OPENAI_BASE_URL"])
            runner_a_env = _launch_host_session(
                client, host_id=host_id, agent_name=agent_name, workspace=tmp_path / "ws-a"
            )
            runner_a_pid = int(runner_a_env["_OMNI_TEST_RUNNER_PID"])
            _assert_env_chain_intact(runner_a_env, daemon_pid=daemon_pid)
            assert {name: runner_a_env.get(name) for name in _LATER_LAUNCH_VARS} == {
                "CLAUDE_CODE_ENABLE_TELEMETRY": None,
                "OTEL_RESOURCE_ATTRIBUTES": _FIRST_LAUNCH_ATTRIBUTES,
            }, "the first runner does not reflect the first launch's environment"

            relaunch = _spawn_background_daemon(omnigent_python, omnigent_repo_root, later_env)
            assert relaunch.returncode == 0, (
                f"second launch failed (rc={relaunch.returncode}):\n{relaunch.stderr}"
            )
            assert _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT) == daemon_pid, (
                "the second launch replaced the host daemon instead of reusing it"
            )
            runner_b_env = _launch_host_session(
                client, host_id=host_id, agent_name=agent_name, workspace=tmp_path / "ws-b"
            )
        runner_b_pid = int(runner_b_env["_OMNI_TEST_RUNNER_PID"])

        _assert_env_chain_intact(runner_b_env, daemon_pid=daemon_pid)
        observed = {name: runner_b_env.get(name) for name in _LATER_LAUNCH_VARS}
        assert observed == _LATER_LAUNCH_VARS, (
            f"a runner launched after the environment changed still carries the "
            f"host's first-launch snapshot: expected {_LATER_LAUNCH_VARS}, got "
            f"{observed}; the change only applies after the host is restarted"
        )
    finally:
        for pid in (runner_a_pid, runner_b_pid, daemon_pid, server_pid):
            if pid > 0:
                _sigterm(pid)
