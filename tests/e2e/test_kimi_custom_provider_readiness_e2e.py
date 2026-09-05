"""E2E: a Kimi Code custom-provider install must read as configured on a host.

Kimi Code supports custom OpenAI-compatible providers (e.g. OpenRouter): with
``config.toml`` declaring ``default_model = "openrouter/..."`` and a
``[providers.openrouter]`` block carrying a valid ``api_key``, the ``kimi`` CLI
works normally from the command line. The host readiness probe
(``omnigent.onboarding.kimi_auth.kimi_auth_configured``) must therefore treat
such an install as configured; otherwise the host advertises
``configured_harnesses["kimi"/"kimi-native"/"native-kimi"] == false`` and the
web picker reports Kimi as unavailable even though it is fully usable.

This drives the reported user journey for real, end to end:

1. configure Kimi Code with an OpenRouter custom provider (``config.toml``
   under a ``KIMI_CODE_HOME``, plus a ``kimi`` CLI on ``PATH``),
2. start an ``omnigent host`` daemon under that environment,
3. inspect the host through ``GET /v1/hosts/{host_id}``,
4. assert the ``kimi`` / ``kimi-native`` / ``native-kimi`` readiness entries
   are available rather than ``false``.

The ``kimi`` binary is a shim (CI has no real Kimi Code install) that answers
``--version`` like a supported release; that is exactly what the readiness
layer probes (binary presence + version + file-based credential check — it
never runs a login command), so the shim exercises the real probe path. The
credential state under test is the real ``config.toml`` the reporter supplied.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact Kimi Code config from the bug report: the default provider is a
# custom OpenAI-compatible endpoint (OpenRouter) with a valid API key.
_OPENROUTER_CONFIG_TOML = """\
default_model = "openrouter/moonshotai/kimi-k3"

[providers.openrouter]
type = "openai"
base_url = "https://openrouter.ai/api/v1"
api_key = "sk-or-fake-test-key"
"""

# Readiness spellings the report checks on the host record. All three must be
# available when the kimi CLI is installed and its config carries a usable
# credential.
_KIMI_READINESS_KEYS = ("kimi", "kimi-native", "native-kimi")


def _write_kimi_shim(bin_dir: Path) -> None:
    """Write a ``kimi`` CLI stand-in that satisfies the readiness version probe.

    The readiness layer only ever runs ``kimi --version`` (credential state is
    read from files), so a shim that reports a supported version is
    indistinguishable from a real install for this journey.

    :param bin_dir: Directory (prepended to the daemon's ``PATH``) to write
        the shim into.
    """
    shim = bin_dir / "kimi"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "--version" ]; then\n'
        '  echo "kimi-code 0.39.1"\n'
        "  exit 0\n"
        "fi\n"
        'echo "OK"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@contextmanager
def _kimi_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
) -> Iterator[subprocess.Popen[bytes]]:
    """Spawn an ``omnigent host`` daemon with a custom-provider Kimi install.

    The daemon runs with an isolated ``OMNIGENT_CONFIG_HOME`` (so the test
    never touches the developer's real host identity), a ``KIMI_CODE_HOME``
    holding the OpenRouter ``config.toml``, and the ``kimi`` shim first on
    ``PATH`` — i.e. exactly the machine state the bug report describes.

    :param tmp_path: Per-test temp dir for the shim, configs, and daemon log.
    :param live_server: Test server URL the daemon registers with.
    :returns: The spawned daemon subprocess handle (terminated on exit).
    """
    bin_dir = tmp_path / "kimi_bin"
    bin_dir.mkdir()
    _write_kimi_shim(bin_dir)

    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    (kimi_home / "config.toml").write_text(_OPENROUTER_CONFIG_TOML)

    config_home = tmp_path / "omnigent-home"
    config_home.mkdir()

    env = {**os.environ}
    # Drop any ambient runner/host identity (present when this test itself
    # runs inside a server-spawned runner) so the daemon starts clean.
    for var in list(env):
        if var.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST", "OMNIGENT_ZYGOTE")):
            env.pop(var)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["KIMI_CODE_HOME"] = str(kimi_home)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    # Import the branch's source (and its in-repo SDK packages) rather than
    # whatever omnigent is installed in the venv — same reasoning as the
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
        yield daemon
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
def test_kimi_custom_provider_reports_configured(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A Kimi install whose default provider is OpenRouter must read configured.

    Regression test for the readiness check rejecting custom OpenAI-compatible
    providers: with the kimi CLI installed and ``config.toml`` declaring an
    OpenRouter provider (valid ``api_key``) as the configured default, the
    host's ``configured_harnesses`` map must report the ``kimi`` /
    ``kimi-native`` / ``native-kimi`` spellings as available — not ``false``,
    which hides Kimi from the picker on a machine where it genuinely works.
    """
    with _kimi_host_daemon(tmp_path=tmp_path, live_server=live_server):
        host_id = _online_host_id(http_client)

        resp = http_client.get(f"/v1/hosts/{host_id}")
        assert resp.status_code == 200, resp.text
        host = resp.json()

        configured = host.get("configured_harnesses")
        assert configured is not None, (
            "host connected without a readiness map — the daemon's harness "
            "probe failed; check the daemon log"
        )

        # The binary probe passed (the shim is on PATH with a supported
        # version), so a False here can only come from the credential check
        # rejecting the OpenRouter provider — the reported bug.
        wrongly_unconfigured = {
            key: configured.get(key)
            for key in _KIMI_READINESS_KEYS
            if configured.get(key) is False
        }
        assert not wrongly_unconfigured, (
            "Kimi harness reported unconfigured despite an installed kimi CLI "
            "and a config.toml whose configured default provider (OpenRouter, "
            f'type="openai") carries a valid API key: {wrongly_unconfigured!r}. '
            "The readiness check (omnigent/onboarding/kimi_auth.py) rejects "
            "custom OpenAI-compatible providers that Kimi Code itself supports."
        )
