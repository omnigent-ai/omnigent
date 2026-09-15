"""Relaunch-while-already-running CLI journey.

A user starts Omnigent on their machine, later loses the web UI tab, and
re-runs a launch command to get back in. The reported 0.10.0 experience:
a cryptic message with no URL, a stack trace on re-launch attempts, and no
way to reopen the web UI.

Regression contract (true on current main; guards the fixed behavior):

- Re-running ``omnigent start`` while the host daemon is up must succeed,
  say it is already running, and print the server URL to reopen.
- Re-running ``omnigent server`` (foreground or ``--background``) while
  the local server is up must reuse it and print its URL.
- A foreground ``omnigent host`` that loses the already-running race must
  fail with a one-line actionable error (pointing at ``host status`` /
  the exact stop command) — never a traceback.
- No re-launch spelling may ever print a Python traceback.

Every step drives the real CLI (``python -m omnigent.cli ...``) in an
isolated ``$HOME``, exactly as a user's shell would.

Usage::

    python -m pytest tests/e2e/test_relaunch_while_running_ux.py -v
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Cold budget: detached server spawn + host daemon registration on a loaded
# CI box. Subsequent relaunches only read the pidfile/daemon record and are
# fast; they get a smaller ceiling.
_FIRST_LAUNCH_TIMEOUT_S = 240
_RELAUNCH_TIMEOUT_S = 120

_TRACEBACK_MARKER = "Traceback (most recent call last)"

_URL_RE = re.compile(r"https?://[\w.\-]+(?::\d+)?")

# Ambient state that would leak into the subprocess and defeat the isolated
# first-launch/relaunch staging (CI runners carry Omnigent + proxy vars).
_ENV_TO_CLEAR = (
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_RUNNER_ID",
    # Proxy vars would route loopback health probes through a proxy that
    # can't reach them.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


def _launch_env(home: Path) -> dict[str, str]:
    """Subprocess env: isolated HOME/state, loopback unproxied, no browser.

    :param home: Isolated home directory (daemon records, pidfile, DB).
    :returns: Environment for the ``omnigent`` subprocesses.
    """
    env = os.environ.copy()
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    # Headless: never shell out to a real browser from auto-open paths.
    env["BROWSER"] = "true"
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


def _omnigent(
    env: dict[str, str],
    *args: str,
    timeout: int = _RELAUNCH_TIMEOUT_S,
) -> tuple[int, str]:
    """Run one real ``omnigent`` CLI command as a user's shell would.

    :param env: Environment from :func:`_launch_env`.
    :param args: CLI arguments, e.g. ``("start", "--non-interactive")``.
    :param timeout: Kill budget for the command.
    :returns: ``(returncode, combined stdout+stderr)``.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "omnigent.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture
def running_omnigent(tmp_path: Path) -> Iterator[dict[str, str]]:
    """Bring Omnigent up (``omnigent start``) in an isolated HOME.

    The journey's step 1: the machine already has the server + host daemon
    running. Tears everything down with ``omnigent stop`` afterwards.

    :param tmp_path: Per-test temp dir used as the isolated ``$HOME``.
    :yields: The subprocess env for follow-up relaunch commands.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _launch_env(home)
    code, output = _omnigent(env, "start", "--non-interactive", timeout=_FIRST_LAUNCH_TIMEOUT_S)
    assert code == 0, f"first `omnigent start` failed:\n{output}"
    assert _TRACEBACK_MARKER not in output, f"first launch crashed:\n{output}"
    try:
        yield env
    finally:
        # Best-effort teardown; a wedged daemon must not fail the test body.
        with contextlib.suppress(subprocess.TimeoutExpired):
            _omnigent(env, "stop", "--force", timeout=_RELAUNCH_TIMEOUT_S)


@pytest.mark.timeout(_FIRST_LAUNCH_TIMEOUT_S + 4 * _RELAUNCH_TIMEOUT_S + 60)
def test_relaunch_while_running_shows_url_and_never_tracebacks(
    running_omnigent: dict[str, str],
) -> None:
    """Every relaunch spelling reopens the door: URL shown, no traceback.

    Journey: start Omnigent -> lose the web UI tab -> re-run a launch
    command to get back. Each spelling a user reaches for must either
    print the server URL (so the web UI is one click away) or fail with a
    one-line actionable error - and never a stack trace.
    """
    env = running_omnigent

    # Relaunch 1 - the on switch again: `omnigent start`.
    code, output = _omnigent(env, "start", "--non-interactive")
    assert code == 0, f"`omnigent start` relaunch failed:\n{output}"
    assert _TRACEBACK_MARKER not in output, f"`start` relaunch crashed:\n{output}"
    assert "already running" in output, (
        f"`start` relaunch did not say the daemon is already running:\n{output}"
    )
    assert _URL_RE.search(output), (
        f"`start` relaunch printed no server URL to reopen the web UI:\n{output}"
    )

    # Relaunch 2 - `omnigent server` (foreground, canonical local server):
    # must reuse the running server and print its URL instead of dying on
    # the taken port.
    code, output = _omnigent(env, "server")
    assert code == 0, f"`omnigent server` relaunch failed:\n{output}"
    assert _TRACEBACK_MARKER not in output, f"`server` relaunch crashed:\n{output}"
    reuse_line = re.search(r"already running at (https?://\S+)", output)
    assert reuse_line, (
        f"`server` relaunch did not reuse the running server with its URL:\n{output}"
    )

    # Relaunch 3 - `omnigent server --background`: same reuse contract.
    code, output = _omnigent(env, "server", "--background")
    assert code == 0, f"`omnigent server --background` relaunch failed:\n{output}"
    assert _TRACEBACK_MARKER not in output, f"`server --background` relaunch crashed:\n{output}"
    assert re.search(r"already running at (https?://\S+)", output), (
        f"`server --background` relaunch printed no reuse URL:\n{output}"
    )

    # Relaunch 4 - foreground `omnigent host ""` loses the already-running
    # race: a one-line actionable conflict error (inspect/stop commands),
    # never a traceback.
    code, output = _omnigent(env, "host", "")
    assert _TRACEBACK_MARKER not in output, (
        f"foreground `host` relaunch crashed with a traceback:\n{output}"
    )
    assert "already running" in output, (
        f"foreground `host` relaunch did not explain the running daemon:\n{output}"
    )
    assert "host status" in output, (
        "foreground `host` conflict error lost its actionable next step "
        f"(`omnigent host status`):\n{output}"
    )
