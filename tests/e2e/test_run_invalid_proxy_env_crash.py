"""Launch-crash e2e test: malformed proxy env must not crash ``omnigent run``.

Journey: a user's shell carries a proxy-bypass environment value with an
IPv6-style entry that httpx cannot parse as ``host:port`` (e.g.
``NO_PROXY=fe80::/10`` — a link-local CIDR without brackets, a common macOS
proxy-bypass entry). They launch ``omnigent run <agent> -p hi``. The CLI's
accounts first-run probe calls ``httpx.get(f"{base_url}/v1/info", ...)`` with
httpx's default ``trust_env=True``; building that client parses the proxy
environment and raises ``httpx.InvalidURL: Invalid port: ':'``. Because
``httpx.InvalidURL`` is NOT a subclass of ``httpx.HTTPError``, the probe's
``except (httpx.HTTPError, ValueError)`` guard misses it and the launch dies
on the crash-handler screen instead of starting the session.

This test spawns the real CLI as a subprocess with an isolated ``$HOME`` and
the poison ``NO_PROXY`` value, and asserts the launch never reaches the
crash handler. The launch is allowed to fail for unrelated environmental
reasons (no model credentials) or to still be mid-bring-up at the timeout —
it must simply never crash with the ``InvalidURL`` traceback.

Usage::

    python -m pytest tests/e2e/test_run_invalid_proxy_env_crash.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The crash reproduces within seconds of launch (right after the local server
# boots). Post-fix the run proceeds into runner bring-up, which can take up to
# ~106s worst case (see test_repl_approval_e2e.py's launch-budget note); a
# timeout past the crash point without crash markers means the bug is gone,
# so the timeout only bounds the healthy path.
_RUN_TIMEOUT_S = 120

# Ambient state that must not leak into the launch: real credentials and any
# real proxy configuration (the test injects its own poison value). All
# ``OMNIGENT_*`` variables are cleared by prefix below — auth-mode switches
# (OMNIGENT_AUTH_*/OIDC/ACCOUNTS/LOCAL_SINGLE_USER), state dirs, and
# runner/host identity leaked by a server-spawned runner all change launch
# behavior, so an explicit list here would rot.
_ENV_TO_CLEAR = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CONFIG_FILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_ENV_PREFIXES_TO_CLEAR = ("OMNIGENT_",)

# An IPv6-style proxy-bypass entry httpx's URL parser rejects: splitting on
# the last ``:`` yields port ``':'`` and ``httpx.InvalidURL`` at Client
# construction time — before any request is sent, so the target URL being
# loopback does not matter.
_POISON_NO_PROXY = "fe80::/10"

# Output that only appears when the launch died on the crash handler with
# this bug's exception (as opposed to failing gracefully for any reason).
_CRASH_MARKERS = (
    "httpx.InvalidURL",
    "A crash report was saved",
    "Traceback (most recent call last)",
)

# Startup output that only prints AFTER the accounts first-run ``/v1/info``
# probe (the crash site) has run: the probe executes between the "Preparing
# your agent" and "Connecting" startup phases, so reaching any of these
# proves the launch survived the repaired code rather than dying earlier for
# some unrelated reason.
_POST_PROBE_MARKERS = (
    "Connecting",
    "Launching your agent",
    "Omnigent session:",
    "No provider credentials",
)


@pytest.fixture
def isolated_home(tmp_path: Path) -> Path:
    """An empty ``$HOME`` so the launch sees a fresh, credential-less machine."""
    home = tmp_path / "home"
    (home / ".omnigent").mkdir(parents=True)
    return home


@pytest.fixture
def agent_yaml(tmp_path: Path) -> Path:
    """Write the minimal agent spec the user launches."""
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: proxy-env-crash-repro\n"
        "description: Minimal agent for the invalid-proxy-env launch repro.\n"
        "executor:\n"
        "  model: gpt-4o\n"
        "prompt: |\n"
        "  You are a test agent.\n"
    )
    return path


def _launch_env(home: Path) -> dict[str, str]:
    """Subprocess env: isolated HOME/state plus the poison proxy value.

    :param home: The staged isolated home directory.
    :returns: Environment for the ``omnigent run`` subprocess.
    """
    env = os.environ.copy()
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    for key in [k for k in env if k.startswith(_ENV_PREFIXES_TO_CLEAR)]:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    env["NO_PROXY"] = _POISON_NO_PROXY
    env["no_proxy"] = _POISON_NO_PROXY
    env["TERM"] = "dumb"
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    return env


@pytest.mark.timeout(_RUN_TIMEOUT_S + 90)
def test_run_with_invalid_proxy_env_does_not_crash(
    isolated_home: Path,
    agent_yaml: Path,
) -> None:
    """A malformed proxy env value must not crash the launch with InvalidURL.

    Journey: shell env carries ``NO_PROXY=fe80::/10`` -> ``omnigent run
    <agent> -p hi`` -> before the fix, the launch dies on the crash-handler
    screen with ``httpx.InvalidURL: Invalid port: ':'`` raised from the
    accounts first-run ``/v1/info`` probe. After the fix the probe tolerates
    (or bypasses) the unparseable proxy environment and the launch proceeds
    past that point — it may still fail gracefully later (no model
    credentials in this isolated HOME), or still be mid-bring-up at the
    timeout, but it never shows the crash handler.
    """
    env = _launch_env(isolated_home)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "run",
            str(agent_yaml),
            "-p",
            "hi",
        ],
        env=env,
        cwd=str(isolated_home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        try:
            output = proc.communicate(timeout=_RUN_TIMEOUT_S)[0] or ""
            timed_out = False
        except subprocess.TimeoutExpired:
            # Post-fix the launch survives into runner bring-up, which this
            # credential-less environment can't complete — surviving past the
            # crash point is the pass condition, so collect what printed.
            proc.kill()
            output = proc.communicate()[0] or ""
            timed_out = True
    finally:
        # The launch boots a local server/daemon before the crash point —
        # stop it so nothing leaks across tests.
        subprocess.run(
            [sys.executable, "-m", "omnigent.cli", "stop"],
            env=env,
            cwd=str(isolated_home),
            capture_output=True,
            timeout=60,
            check=False,
        )

    # THE BUG: the launch dies on the crash-handler screen with
    # ``httpx.InvalidURL: Invalid port: ':'``. Any of these markers means the
    # unguarded trust_env httpx client still crashes the CLI.
    hit = [marker for marker in _CRASH_MARKERS if marker in output]
    assert not hit, (
        "omnigent run crashed on a malformed proxy environment value "
        f"(NO_PROXY={_POISON_NO_PROXY!r}): found crash markers {hit} in the "
        f"launch output (timed_out={timed_out}).\n--- CLI output ---\n{output}"
    )

    # And the launch actually made it PAST the crash site: without a positive
    # post-probe milestone, an unrelated early failure (server never booted)
    # would pass the no-crash check without ever exercising the fixed code.
    reached = [marker for marker in _POST_PROBE_MARKERS if marker in output]
    assert reached, (
        "omnigent run never got past the accounts first-run probe (the crash "
        f"site) — none of {_POST_PROBE_MARKERS} appeared in the launch output "
        f"(timed_out={timed_out}).\n--- CLI output ---\n{output}"
    )
