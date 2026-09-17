"""E2E: a configured ``acp:<slug>`` agent must read as configured on its host.

Guards against a keyspace drift in the daemon's readiness map
(``omnigent.onboarding.harness_readiness.configured_harness_map``): when the
map is built only from fixed harness spellings plus the generic ``acp`` key --
without enumerating the user-configured ``acp:<slug>`` slugs -- it carries
``acp: True`` but no ``acp:traex`` key. The picker seeds one agent row per
configured slug and filters that row against the host's ``configured_harnesses``
map, where an undefined key on a non-empty map reads as "unconfigured", so a
launchable agent (an ``acp:`` config block, e.g. TraeX -> ``acp:traex``) is
wrongly badged "needs setup" in the New Chat picker.

This drives the reported precondition for real, end to end:

1. configure a generic-ACP agent (``acp:`` block naming TraeX) under an isolated
   ``OMNIGENT_CONFIG_HOME``,
2. start an ``omnigent host`` daemon under that environment,
3. inspect the host through ``GET /v1/hosts/{host_id}``,
4. assert the readiness map exposes the ``acp:traex`` slug as available -- not
   absent, which is what hides it behind a false "needs setup" badge.

The contradiction is the bug: the launch gate for the same slug
(``harness_is_configured`` -> canonical ``acp`` -> ``bool(acp_agents())``) is
``True`` on the same config, so the agent genuinely launches; only the readiness
map omits the slug key, so the badge is a false negative.
"""

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

# The exact acp: config from the bug report: one user-configured generic-ACP
# agent whose display name slugifies to ``traex`` (harness id ``acp:traex``).
_ACP_CONFIG_YAML = """\
acp:
  agents:
    - name: TraeX
      command: traex acp serve
"""

# The slug the report's agent registers under, and the readiness-map key that
# the picker filters the seeded row against.
_ACP_SLUG = "traex"
_ACP_HARNESS_KEY = f"acp:{_ACP_SLUG}"


@contextmanager
def _acp_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
) -> Iterator[Path]:
    """Spawn an ``omnigent host`` daemon whose config declares an ``acp:`` agent.

    The daemon runs with an isolated ``OMNIGENT_CONFIG_HOME`` (so the test never
    touches the developer's real host identity) holding the report's ``acp:``
    block -- i.e. exactly the machine state the bug describes.

    :param tmp_path: Per-test temp dir for the config home and daemon log.
    :param live_server: Test server URL the daemon registers with.
    :returns: The ``OMNIGENT_CONFIG_HOME`` path so the test can read the same
        config the daemon's readiness probe used.
    """
    config_home = tmp_path / "omnigent-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(_ACP_CONFIG_YAML)

    env = {**os.environ}
    # Drop any ambient runner/host identity (present when this test itself runs
    # inside a server-spawned runner) so the daemon starts clean.
    for var in list(env):
        if var.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST", "OMNIGENT_ZYGOTE")):
            env.pop(var)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    # Import the branch's source (and its in-repo SDK packages) rather than
    # whatever omnigent is installed in the venv -- same reasoning as the
    # live_server fixture's PYTHONPATH.
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
    """Poll ``GET /v1/hosts`` until a host is online; return its id.

    :param client: HTTP client bound to the live server.
    :param timeout: Max seconds to wait for the daemon to register.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
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
    """A configured ``acp:<slug>`` must appear (available) in the readiness map.

    The daemon reports readiness through ``configured_harness_map()``. With an
    ``acp:`` block declaring TraeX, the generic ``acp`` key is available (there
    is a configured agent), and the *launch gate* for ``acp:traex`` is also
    ``True`` -- so the agent genuinely launches. The map must therefore also
    carry the ``acp:traex`` slug key as available; when it is absent the picker
    (``harnessUnavailableReasonOnHost``) badges the seeded row "needs setup" on a
    non-empty map, the false negative under test.
    """
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

        # The launch gate accepts the slug on this exact config, so the agent
        # launches -- proving any "unconfigured" verdict from the readiness map
        # is a false negative. Read it under the daemon's config home so it
        # sees the same acp: block.
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

        # Generic acp is ready (there is a configured agent), so the specific
        # slug the picker filters on must be present and available too.
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
