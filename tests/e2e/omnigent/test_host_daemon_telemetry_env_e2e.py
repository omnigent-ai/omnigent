"""E2E: the background host daemon must keep ``CLAUDE_CODE_ENABLE_TELEMETRY``.

Guards the background-daemon telemetry env chain: a user who exports
``CLAUDE_CODE_ENABLE_TELEMETRY=1`` (plus the ``OTEL_*`` exporter config)
and then starts Omnigent in the background finds the flag missing
from the detached daemon's environment — ``_build_host_daemon_env`` allowlists
``CLAUDE_CODE_OAUTH_TOKEN`` / ``CLAUDE_CODE_USE_BEDROCK`` but not the telemetry
opt-in — and therefore missing from every runner the daemon spawns, so the
claude-sdk lane can never export ``claude_code.*`` telemetry on a
background-daemon dispatch (foreground daemons work).

Both tests drive the real user journey: ``omnigent host --background ""`` under
an isolated HOME with the telemetry environment exported, then observe the
actual process environments (``/proc/<pid>/environ`` — the same evidence method
as the report's ``env | sort`` / ``ps -wwwE``).

- ``test_background_daemon_env_keeps_claude_telemetry_flag`` asserts the
  detached daemon itself still carries the flag (the allowlist drop).
- ``test_daemon_spawned_runner_receives_claude_telemetry_flag`` goes one hop
  further: create a host-bound session so the daemon spawns a real runner, and
  assert the flag reached the runner process (what the harness inherits).

Control variables (``OTEL_METRICS_EXPORTER``, ``OMNIGENT_TELEMETRY_ENABLED``)
are asserted to survive, so a failure is specifically the telemetry-flag drop
and not a broken env chain.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _connect_env,
    _read_local_server_record,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="reads /proc/<pid>/environ to observe the live process environments",
)

_BOOT_TIMEOUT = 90.0
# The runner is spawned asynchronously after session create; generous for CI.
_RUNNER_APPEAR_TIMEOUT = 60.0
_POLL_PAUSE = threading.Event()


def _telemetry_env(base_env: dict[str, str], home: Path) -> dict[str, str]:
    """The user's telemetry environment for the ``host --background`` spawn.

    :param base_env: Fixture credential environment (mock LLM).
    :param home: Isolated HOME for this run.
    :returns: Environment dict for the CLI subprocess.
    """
    env = _connect_env(base_env, home)
    env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
    env["OTEL_METRICS_EXPORTER"] = "otlp"
    env["OMNIGENT_TELEMETRY_ENABLED"] = "1"
    return env


def _spawn_background_daemon(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent host --background ""`` (spawns a detached local daemon).

    :param omnigent_python: Python interpreter with omnigent installed.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Subprocess environment from :func:`_telemetry_env`.
    :returns: The completed process (returns once the daemon registered).
    """
    return subprocess.run(
        [str(omnigent_python), "-m", "omnigent", "host", "--background", ""],
        env=dict(env),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_BOOT_TIMEOUT,
    )


def _wait_for_daemon_pid(home: Path, *, timeout: float) -> int:
    """Wait for the daemon registry record and return the daemon pid.

    :param home: Isolated HOME holding ``.omnigent/daemons``.
    :param timeout: Max seconds to poll for the record.
    :returns: The detached daemon's pid.
    :raises AssertionError: If no record appears within *timeout*.
    """
    daemons = home / ".omnigent" / "daemons"
    elapsed = 0.0
    while elapsed < timeout:
        records = sorted(daemons.glob("*.json")) if daemons.is_dir() else []
        if records:
            return int(json.loads(records[0].read_text())["pid"])
        _POLL_PAUSE.wait(0.25)
        elapsed += 0.25
    raise AssertionError(f"daemon record never appeared under {daemons}")


def _proc_environ(pid: int) -> dict[str, str]:
    """Read a live process's environment from ``/proc/<pid>/environ``.

    :param pid: The target process id.
    :returns: The process environment as a dict.
    """
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    env: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" in chunk:
            key, _, value = chunk.partition(b"=")
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _sigterm(pid: int) -> None:
    """Best-effort SIGTERM so spawned processes never leak past the test."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)


def _assert_telemetry_flag_survived(env: dict[str, str], *, process: str) -> None:
    """Assert the telemetry flag survived the env strip into *process*.

    Control assertions come first so a broken env chain fails distinctly
    from the specific telemetry-flag drop under test.

    :param env: The observed process environment.
    :param process: Human label for the process, e.g. ``"daemon"``.
    """
    assert env.get("OTEL_METRICS_EXPORTER") == "otlp", (
        f"control var OTEL_METRICS_EXPORTER missing from the {process} env — "
        "the whole env chain is broken, not just the telemetry flag"
    )
    assert env.get("OMNIGENT_TELEMETRY_ENABLED") == "1", (
        f"control var OMNIGENT_TELEMETRY_ENABLED missing from the {process} env — "
        "the whole env chain is broken, not just the telemetry flag"
    )
    assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1", (
        f"CLAUDE_CODE_ENABLE_TELEMETRY was dropped from the {process} env "
        "while OTEL_*/OMNIGENT_TELEMETRY_ENABLED survived — the claude-sdk "
        "lane cannot export claude_code.* telemetry on a background-daemon "
        "dispatch"
    )


def test_background_daemon_env_keeps_claude_telemetry_flag(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """The detached background daemon keeps ``CLAUDE_CODE_ENABLE_TELEMETRY``.

    Journey: export the telemetry env → ``omnigent host --background ""`` →
    inspect the daemon process env. The flag must survive the CLI→daemon
    env strip exactly like the OTEL config it belongs with.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _telemetry_env(mock_credentials_env, home)
    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = -1
    server_pid = -1
    try:
        daemon_pid = _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT)
        server_pid, _port = _read_local_server_record(home)
        _assert_telemetry_flag_survived(_proc_environ(daemon_pid), process="daemon")
    finally:
        if daemon_pid > 0:
            _sigterm(daemon_pid)
        if server_pid > 0:
            _sigterm(server_pid)


def test_daemon_spawned_runner_receives_claude_telemetry_flag(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A runner spawned by the background daemon gets the telemetry flag.

    Journey: export the telemetry env → ``omnigent host --background ""`` →
    create a session bound to this host (the daemon spawns a real runner) →
    inspect the runner process env. This is the delivery the harness
    subprocess ultimately inherits (``_build_harness_spawn_env`` merges the
    runner's ``os.environ``), so the flag reaching the runner is the
    end-to-end contract.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = _telemetry_env(mock_credentials_env, home)
    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = -1
    server_pid = -1
    runner_pid = -1
    try:
        daemon_pid = _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT)
        server_pid, port = _read_local_server_record(home)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(
            base_url=base,
            timeout=30.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as client:
            host_id = _online_host_id(client, timeout=_BOOT_TIMEOUT)
            agents = client.get("/v1/agents")
            agents.raise_for_status()
            rows = agents.json()["data"]
            assert rows, "server registered no agents"
            create = client.post(
                "/v1/sessions",
                json={
                    "agent_id": rows[0]["id"],
                    "host_id": host_id,
                    "workspace": str(workspace),
                },
                timeout=60.0,
            )
            create.raise_for_status()

        runner_pid = _wait_for_runner_pid(workspace, timeout=_RUNNER_APPEAR_TIMEOUT)
        _assert_telemetry_flag_survived(_proc_environ(runner_pid), process="runner")
    finally:
        if runner_pid > 0:
            _sigterm(runner_pid)
        if daemon_pid > 0:
            _sigterm(daemon_pid)
        if server_pid > 0:
            _sigterm(server_pid)


def _online_host_id(client: httpx.Client, timeout: float) -> str:
    """Poll ``GET /v1/hosts`` until a host is online; return its id.

    :param client: HTTP client pointed at the detached local server.
    :param timeout: Max seconds to wait for the daemon's host registration.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
    elapsed = 0.0
    while elapsed < timeout:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
            if online:
                return str(online[0]["host_id"])
        _POLL_PAUSE.wait(0.5)
        elapsed += 0.5
    raise AssertionError(f"no host came online within {timeout}s")


def _wait_for_runner_pid(workspace: Path, *, timeout: float) -> int:
    """Find the daemon-spawned runner for *workspace* by scanning ``/proc``.

    The runner env is built by ``_build_runner_env``, which always stamps
    ``OMNIGENT_RUNNER_WORKSPACE`` with the session workspace — a per-test
    unique tmp path, so the match cannot pick up another test's runner.

    :param workspace: The session workspace passed at session create.
    :param timeout: Max seconds to poll for the runner process.
    :returns: The runner's pid.
    :raises AssertionError: If no runner appears within *timeout*.
    """
    needle = str(workspace)
    elapsed = 0.0
    while elapsed < timeout:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                env = _proc_environ(int(entry.name))
            except (OSError, PermissionError):
                continue
            if env.get("OMNIGENT_RUNNER_WORKSPACE") == needle and "RUNNER_SERVER_URL" in env:
                return int(entry.name)
        _POLL_PAUSE.wait(0.5)
        elapsed += 0.5
    raise AssertionError(f"no runner with OMNIGENT_RUNNER_WORKSPACE={needle} within {timeout}s")
