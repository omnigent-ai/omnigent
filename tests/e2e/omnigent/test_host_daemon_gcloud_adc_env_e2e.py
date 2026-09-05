"""E2E: the background host daemon must keep the gcloud ADC auth selectors.

Guards the background-daemon env chain for the Antigravity CLI (``agy``) gcloud
Application Default Credentials selectors. A user who runs
``gcloud auth application-default login`` and then exports the selectors that
``agy`` reads to pick ADC auth --

    AGY_ADC_AUTH=true
    GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/application_default_credentials.json
    GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_QUOTA_PROJECT
    CLOUDSDK_CONFIG / CLOUDSDK_ACTIVE_CONFIG_NAME (non-default gcloud config)

-- and then starts Omnigent in the background finds every one of them missing
from the detached daemon, and therefore missing from every runner the daemon
spawns. ``_build_host_daemon_env`` (omnigent/cli.py) filters ``os.environ``
through ``_RUNNER_ENV_ALLOWLIST`` + ``_LOCAL_DAEMON_ENV_ALLOWLIST`` + their
prefix sets, none of which named the ADC selectors or the gcloud config
selectors before the fix (the fix adds exact names only -- deliberately not a
``CLOUDSDK_`` prefix, which would also pass gcloud's secret-bearing
``CLOUDSDK_AUTH_*`` tokens);
``_build_runner_env`` (omnigent/host/connect.py) then re-applies the runner
allowlist. So the antigravity-native pane the runner launches inherits a
stripped environment, ``agy`` sees no ADC selector, and every dispatched pane
stops at the interactive "Select login method" menu instead of starting -- even
though the credential is valid. ``OMNIGENT_RUNNER_ENV_PASSTHROUGH`` cannot
recover the values because the first hop already discarded them.

Both tests drive the real user journey: ``omnigent host --background ""`` under
an isolated HOME with the ADC selectors exported, then observe the actual
process environments (``/proc/<pid>/environ`` -- the same evidence method the
report used with ``/proc/<daemon>/environ``).

- ``test_background_daemon_env_keeps_gcloud_adc_selectors`` asserts the detached
  daemon itself still carries the selectors (the CLI->daemon strip).
- ``test_daemon_spawned_runner_receives_gcloud_adc_selectors`` goes one hop
  further: create a host-bound session so the daemon spawns a real runner, and
  assert the selectors reached the runner process -- what the antigravity-native
  pane ultimately inherits.

Control variables (``OTEL_METRICS_EXPORTER``, ``OMNIGENT_TELEMETRY_ENABLED``)
are asserted to survive, so a failure is specifically the ADC-selector drop and
not a broken env chain.
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

# The gcloud ADC auth selectors ``agy`` reads, allowed through both hops by
# exact name. ``GOOGLE_APPLICATION_CREDENTIALS`` is a filesystem path (not the
# credential); the rest are non-secret auth-mode / project / config selectors
# -- the same security class as ``KUBECONFIG``. Only these exact CLOUDSDK_
# names are allowlisted; gcloud's CLOUDSDK_AUTH_* token vars stay stripped.
_ADC_SELECTORS: dict[str, str] = {
    "AGY_ADC_AUTH": "true",
    "GOOGLE_APPLICATION_CREDENTIALS": "<set-per-test>",
    "GOOGLE_CLOUD_PROJECT": "acme-dev",
    "GOOGLE_CLOUD_QUOTA_PROJECT": "acme-quota",
    "CLOUDSDK_CONFIG": "<set-per-test>",
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "alt",
}


def _adc_env(base_env: dict[str, str], home: Path) -> dict[str, str]:
    """The user's ADC environment for the ``host --background`` spawn.

    Seeds a realistic ADC credentials file under *home* so
    ``GOOGLE_APPLICATION_CREDENTIALS`` / ``CLOUDSDK_CONFIG`` point at real paths,
    exactly as they would after ``gcloud auth application-default login``.

    :param base_env: Fixture credential environment (mock LLM).
    :param home: Isolated HOME for this run.
    :returns: Environment dict for the CLI subprocess and the resolved selector
        values (mutated in place onto a copy of ``_ADC_SELECTORS``).
    """
    env = _connect_env(base_env, home)
    gcloud_dir = home / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True, exist_ok=True)
    adc_path = gcloud_dir / "application_default_credentials.json"
    adc_path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "x.apps.googleusercontent.com",
                "client_secret": "not-a-real-secret",
                "refresh_token": "not-a-real-refresh-token",
            }
        ),
        encoding="utf-8",
    )
    selectors = dict(_ADC_SELECTORS)
    selectors["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc_path)
    selectors["CLOUDSDK_CONFIG"] = str(gcloud_dir)
    env.update(selectors)
    # Exported as in the original report. With the allowlist fix the values
    # arrive via the allowlists regardless, so this only mirrors the reported
    # setup; it is not what carries the selectors through.
    env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = "AGY_ADC_AUTH,GOOGLE_APPLICATION_CREDENTIALS"
    env["OTEL_METRICS_EXPORTER"] = "otlp"
    env["OMNIGENT_TELEMETRY_ENABLED"] = "1"
    return env


def _resolved_selectors(env: dict[str, str]) -> dict[str, str]:
    """The expected ADC selector name->value map given the subprocess *env*.

    :param env: Environment produced by :func:`_adc_env`.
    :returns: The selector map with the per-test path values filled in.
    """
    return {name: env[name] for name in _ADC_SELECTORS}


def _spawn_background_daemon(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent host --background ""`` (spawns a detached local daemon).

    :param omnigent_python: Python interpreter with omnigent installed.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Subprocess environment from :func:`_adc_env`.
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
    """Best-effort SIGTERM (escalating to SIGKILL) so processes never leak.

    The daemon/runner are detached, not our children, so poll ``/proc`` for
    exit instead of ``waitpid`` and escalate if the process lingers.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                return
            _POLL_PAUSE.wait(0.25)
        os.kill(pid, signal.SIGKILL)


def _assert_adc_selectors_survived(
    env: dict[str, str], expected: dict[str, str], *, process: str
) -> None:
    """Assert the gcloud ADC selectors survived the env strip into *process*.

    Control assertions come first so a broken env chain fails distinctly from
    the specific ADC-selector drop under test.

    :param env: The observed process environment.
    :param expected: The selector name->value map that must be present.
    :param process: Human label for the process, e.g. ``"daemon"``.
    """
    assert env.get("OTEL_METRICS_EXPORTER") == "otlp", (
        f"control var OTEL_METRICS_EXPORTER missing from the {process} env -- "
        "the whole env chain is broken, not just the ADC selectors"
    )
    assert env.get("OMNIGENT_TELEMETRY_ENABLED") == "1", (
        f"control var OMNIGENT_TELEMETRY_ENABLED missing from the {process} env -- "
        "the whole env chain is broken, not just the ADC selectors"
    )
    observed = {name: env.get(name) for name in expected}
    assert observed == expected, (
        f"gcloud ADC selectors were dropped from the {process} env while the "
        f"OTEL_*/OMNIGENT_TELEMETRY_ENABLED controls survived: expected "
        f"{expected}, got {observed}. agy sees no ADC selector, so every "
        f"antigravity-native pane dispatched through a background host stops at "
        f"the interactive 'Select login method' menu instead of starting."
    )


def test_background_daemon_env_keeps_gcloud_adc_selectors(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """The detached background daemon keeps the gcloud ADC selectors.

    Journey: export the ADC selectors -> ``omnigent host --background ""`` ->
    inspect the daemon process env. The selectors must survive the CLI->daemon
    env strip exactly like the OTEL config alongside them.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _adc_env(mock_credentials_env, home)
    expected = _resolved_selectors(env)
    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = -1
    server_pid = -1
    try:
        daemon_pid = _wait_for_daemon_pid(home, timeout=_BOOT_TIMEOUT)
        server_pid, _port = _read_local_server_record(home)
        _assert_adc_selectors_survived(_proc_environ(daemon_pid), expected, process="daemon")
    finally:
        if daemon_pid > 0:
            _sigterm(daemon_pid)
        if server_pid > 0:
            _sigterm(server_pid)


def test_daemon_spawned_runner_receives_gcloud_adc_selectors(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A runner spawned by the background daemon gets the gcloud ADC selectors.

    Journey: export the ADC selectors -> ``omnigent host --background ""`` ->
    create a session bound to this host (the daemon spawns a real runner) ->
    inspect the runner process env. This is the delivery the antigravity-native
    agy pane ultimately inherits (``_build_harness_spawn_env`` merges the
    runner's ``os.environ``), so the selectors reaching the runner is the
    end-to-end contract. ``OMNIGENT_RUNNER_ENV_PASSTHROUGH`` is exported as in
    the original report, but with the fix the selectors travel via the
    allowlists, so this test does not distinguish that mechanism.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = _adc_env(mock_credentials_env, home)
    expected = _resolved_selectors(env)
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

        runner_env = _wait_for_runner_env(workspace, timeout=_RUNNER_APPEAR_TIMEOUT)
        runner_pid = int(runner_env["_OMNI_TEST_RUNNER_PID"])
        _assert_adc_selectors_survived(runner_env, expected, process="runner")
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


def _wait_for_runner_env(workspace: Path, *, timeout: float) -> dict[str, str]:
    """Find the daemon-spawned runner for *workspace* and return its environ.

    The runner env is built by ``_build_runner_env``, which always stamps
    ``OMNIGENT_RUNNER_WORKSPACE`` with the session workspace -- a per-test
    unique tmp path, so the match cannot pick up another test's runner. The
    matching pid is returned under the synthetic ``_OMNI_TEST_RUNNER_PID`` key
    so the caller can reap it even after the process exits.

    :param workspace: The session workspace passed at session create.
    :param timeout: Max seconds to poll for the runner process.
    :returns: The runner's environment plus ``_OMNI_TEST_RUNNER_PID``.
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
                env["_OMNI_TEST_RUNNER_PID"] = entry.name
                return env
        _POLL_PAUSE.wait(0.5)
        elapsed += 0.5
    raise AssertionError(f"no runner with OMNIGENT_RUNNER_WORKSPACE={needle} within {timeout}s")
