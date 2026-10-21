"""E2E: the detached host forwards passthrough-named env and refreshes it on reuse.

Both tests drive the real user journey under an isolated HOME: export the
configuration, start the detached host with ``omnigent host --background ""``,
create a host-bound session so the daemon spawns a real runner, and read the
live process environments from ``/proc/<pid>/environ``.

- ``test_passthrough_named_variable_reaches_daemon_spawned_runner``: a
  non-``DATABRICKS_`` variable named in ``OMNIGENT_RUNNER_ENV_PASSTHROUGH``
  must reach the runner, while an unlisted ambient variable must not.
- ``test_environment_exported_after_daemon_start_reaches_later_runner``: a
  variable exported or changed after the host started must reach a runner
  spawned after a later ``omnigent host --background ""`` reused the daemon.

Control variables that every hop already allowlists (``OTEL_*``) are asserted
first so a failure is specifically the reported drop and not a broken chain.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import lookup_agent_id, register_inline_agent
from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _connect_env,
    _read_local_server_record,
)
from tests.e2e.omnigent.test_host_daemon_gcloud_adc_env_e2e import (
    _BOOT_TIMEOUT,
    _RUNNER_APPEAR_TIMEOUT,
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

_PASSTHROUGH_VARS: dict[str, str] = {
    "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway-Tenant: acme",
    "HARNESS_CLAUDE_SDK_GATEWAY_URL": "https://gateway.example.test/anthropic",
    "ISAAC_LAUNCH_MODE": "omni",
}
_UNLISTED_PROBE = "UNLISTED_AMBIENT_PROBE"
_FIRST_LAUNCH_ATTRS = "deployment.environment=first-launch"
_SECOND_LAUNCH_ATTRS = "deployment.environment=second-launch"


def _host_env(base_env: dict[str, str], home: Path) -> dict[str, str]:
    """Isolated environment for one ``omnigent host --background ""`` journey.

    :param base_env: Fixture credential environment (mock LLM).
    :param home: Isolated HOME for this run.
    :returns: Environment dict for the CLI subprocess.
    """
    env = _connect_env(base_env, home)
    env["OMNIGENT_HOST_ID"] = uuid.uuid4().hex
    env["OMNIGENT_HOST_NAME"] = "env-passthrough-e2e"
    env["OTEL_METRICS_EXPORTER"] = "otlp"
    # /proc shows exec-time env; a zygote-forked runner never re-execs.
    env["OMNIGENT_RUNNER_ZYGOTE"] = "0"
    return env


def _create_host_session(env: dict[str, str], port: int, workspace: Path) -> None:
    """Create a session bound to this journey's host (the call the web UI makes).

    Registers a mock-backed openai-agents agent first: the built-in agents of a
    fresh local server are native-UI agents whose harness is not configured on
    this host, so a session on them is rejected before any runner spawns.

    :param env: The journey environment (host identity + mock LLM URL).
    :param port: The daemon-owned local server port.
    :param workspace: Session workspace; unique per runner so it can be found.
    """
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=30.0,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    ) as client:
        host_id = _online_host_id(client, host_id=env["OMNIGENT_HOST_ID"], timeout=_BOOT_TIMEOUT)
        agent_name = register_inline_agent(
            client,
            name="env-passthrough-e2e",
            harness="openai-agents",
            model="gpt-4o",
            profile="",
            prompt="Reply briefly.",
            mock_llm_base_url=env["OPENAI_BASE_URL"],
        )
        create = client.post(
            "/v1/sessions",
            json={
                "agent_id": lookup_agent_id(client, agent_name),
                "host_id": host_id,
                "workspace": str(workspace),
            },
            timeout=60.0,
        )
        assert create.is_success, f"session create failed: {create.status_code} {create.text}"


def _assert_control_survived(env: dict[str, str], *, process: str) -> None:
    assert env.get("OTEL_METRICS_EXPORTER") == "otlp", (
        f"control var OTEL_METRICS_EXPORTER missing from the {process} env — "
        "the whole env chain is broken, not just the reported drop"
    )


def test_passthrough_named_variable_reaches_daemon_spawned_runner(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Variables named in ``OMNIGENT_RUNNER_ENV_PASSTHROUGH`` reach the runner.

    Journey: export gateway vars + the passthrough list naming them →
    ``omnigent host --background ""`` → create a host-bound session → inspect
    the runner env. The list variable itself survives both hops; the variables
    it names must too, and an unlisted ambient variable must not.
    """
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = _host_env(mock_credentials_env, home)
    env.update(_PASSTHROUGH_VARS)
    env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = ",".join(_PASSTHROUGH_VARS)
    env[_UNLISTED_PROBE] = "should-not-forward"
    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = server_pid = runner_pid = -1
    try:
        daemon_pid = _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT)
        server_pid, port = _read_local_server_record(home)
        _create_host_session(env, port, workspace)
        runner_env = _wait_for_runner_env(
            workspace, daemon_pid=daemon_pid, timeout=_RUNNER_APPEAR_TIMEOUT
        )
        runner_pid = int(runner_env["_OMNI_TEST_RUNNER_PID"])
        daemon_env = _proc_environ(daemon_pid)

        _assert_control_survived(runner_env, process="runner")
        assert runner_env.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH") == ",".join(_PASSTHROUGH_VARS)
        assert _UNLISTED_PROBE not in runner_env, "an unlisted ambient variable was forwarded"
        delivered = {name: runner_env.get(name) for name in _PASSTHROUGH_VARS}
        assert delivered == _PASSTHROUGH_VARS, (
            "variables named in OMNIGENT_RUNNER_ENV_PASSTHROUGH did not reach the runner: "
            f"{delivered}; present in daemon: "
            f"{sorted(name for name in _PASSTHROUGH_VARS if name in daemon_env)}"
        )
    finally:
        for pid in (runner_pid, daemon_pid, server_pid):
            if pid > 0:
                _sigterm(pid)


def test_environment_exported_after_daemon_start_reaches_later_runner(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A later CLI invocation's environment reaches runners of the reused host.

    Journey: start the host with the first-launch env → session A → export a
    new variable and change an existing one → ``omnigent host --background ""``
    again (reports the daemon already running) → session B → inspect runner B.
    """
    home = tmp_path / "home"
    workspace_a = tmp_path / "ws-a"
    workspace_b = tmp_path / "ws-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    env = _host_env(mock_credentials_env, home)
    env.pop("CLAUDE_CODE_ENABLE_TELEMETRY", None)
    env["OTEL_RESOURCE_ATTRIBUTES"] = _FIRST_LAUNCH_ATTRS
    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = server_pid = runner_a_pid = runner_b_pid = -1
    try:
        daemon_pid = _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT)
        server_pid, port = _read_local_server_record(home)
        _create_host_session(env, port, workspace_a)
        runner_a_env = _wait_for_runner_env(
            workspace_a, daemon_pid=daemon_pid, timeout=_RUNNER_APPEAR_TIMEOUT
        )
        runner_a_pid = int(runner_a_env["_OMNI_TEST_RUNNER_PID"])
        _assert_control_survived(runner_a_env, process="runner A")
        assert runner_a_env.get("OTEL_RESOURCE_ATTRIBUTES") == _FIRST_LAUNCH_ATTRS
        assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in runner_a_env

        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        env["OTEL_RESOURCE_ATTRIBUTES"] = _SECOND_LAUNCH_ATTRS
        rerun = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
        assert rerun.returncode == 0, (
            f"second spawn failed (rc={rerun.returncode}):\n{rerun.stderr}"
        )
        assert "Host daemon already running" in rerun.stdout, rerun.stdout
        assert _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT) == daemon_pid

        _create_host_session(env, port, workspace_b)
        runner_b_env = _wait_for_runner_env(
            workspace_b, daemon_pid=daemon_pid, timeout=_RUNNER_APPEAR_TIMEOUT
        )
        runner_b_pid = int(runner_b_env["_OMNI_TEST_RUNNER_PID"])
        _assert_control_survived(runner_b_env, process="runner B")
        refreshed = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": runner_b_env.get("CLAUDE_CODE_ENABLE_TELEMETRY"),
            "OTEL_RESOURCE_ATTRIBUTES": runner_b_env.get("OTEL_RESOURCE_ATTRIBUTES"),
        }
        assert refreshed == {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_RESOURCE_ATTRIBUTES": _SECOND_LAUNCH_ATTRS,
        }, f"runner B kept the first-launch environment of the reused host: {refreshed}"
    finally:
        for pid in (runner_a_pid, runner_b_pid, daemon_pid, server_pid):
            if pid > 0:
                _sigterm(pid)
