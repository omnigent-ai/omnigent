"""Verify a host advertises configured ``acp:<slug>`` agents."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ACP_CONFIG_YAML = """\
acp:
  agents:
    - name: TraeX
      command: traex acp serve
"""

_ACP_SLUG = "traex"
_ACP_HARNESS_KEY = f"acp:{_ACP_SLUG}"


@contextmanager
def _acp_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
) -> Iterator[Path]:
    """Run a host daemon with an isolated ACP configuration."""
    config_home = tmp_path / "omnigent-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(_ACP_CONFIG_YAML)

    env = {**os.environ}
    # Keep the daemon independent from a parent runner or host.
    for var in list(env):
        if var.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST", "OMNIGENT_ZYGOTE")):
            env.pop(var)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    # Import this worktree and its in-repo SDK packages.
    pythonpath = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        daemon = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    try:
        yield config_home
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


def _online_host_id(client: httpx.Client, timeout: float = 60.0) -> str:
    """Wait for the host daemon to register."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(1.0)
    raise AssertionError(f"No host came online within {timeout}s")


@pytest.mark.timeout(180)
def test_configured_acp_slug_reads_configured_on_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """Expose a launchable ACP slug in the host readiness map."""
    with _acp_host_daemon(tmp_path=tmp_path, live_server=live_server) as config_home:
        host_id = _online_host_id(http_client)

        resp = http_client.get(f"/v1/hosts/{host_id}")
        assert resp.status_code == 200, resp.text
        host = resp.json()

        configured = host.get("configured_harnesses")
        assert configured, (
            "host connected without a readiness map -- the daemon's harness "
            "probe failed; check the daemon log"
        )

        # Evaluate the launch gate with the daemon's configuration.
        prev_home = os.environ.get("OMNIGENT_CONFIG_HOME")
        os.environ["OMNIGENT_CONFIG_HOME"] = str(config_home)
        try:
            from omnigent.onboarding.acp_auth import acp_agents
            from omnigent.onboarding.harness_readiness import harness_is_configured

            slugs = {agent.slug for agent in acp_agents()}
            assert _ACP_SLUG in slugs, (
                f"the daemon's config home did not register the {_ACP_SLUG!r} agent: {slugs!r}"
            )
            assert harness_is_configured(_ACP_HARNESS_KEY) is True, (
                f"launch gate rejected {_ACP_HARNESS_KEY!r} despite a configured acp: block"
            )
        finally:
            if prev_home is None:
                os.environ.pop("OMNIGENT_CONFIG_HOME", None)
            else:
                os.environ["OMNIGENT_CONFIG_HOME"] = prev_home

        assert configured.get("acp"), (
            "generic 'acp' readiness should be available with a configured agent; "
            f"got {configured.get('acp')!r}"
        )
        availability = configured.get(_ACP_HARNESS_KEY)
        assert availability is not None and availability is not False, (
            f"configured acp agent {_ACP_HARNESS_KEY!r} is missing/unavailable in the host "
            f"readiness map ({availability!r}) even though 'acp' is available and its launch "
            "gate accepts it. The map (configured_harness_map) is not enumerating "
            "user-configured acp:<slug> slugs, so the New Chat picker badges the seeded row "
            "'needs setup' despite the agent launching fine."
        )
