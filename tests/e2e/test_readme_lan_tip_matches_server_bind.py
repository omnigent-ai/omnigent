"""E2E: the README's LAN-access tip must match the default server bind.

README step 4 tells users on their own network to skip a deploy and open the
machine's LAN address from a phone (``http://192.168.x.x:6767``), but the
default ``omnigent server`` binds ``127.0.0.1``, so that journey ends in
connection refused unless the tip also explains how to bind a reachable
interface (``--host``).

Both tests spawn the REAL ``omnigent server`` CLI subprocess. A second
loopback address (``127.0.0.2``) stands in for the machine's LAN IP: a
``127.0.0.1``-bound socket refuses it and a ``0.0.0.0``-bound socket accepts
it — the same accept/refuse semantics a phone on the LAN observes.

Run::

    .venv/bin/python -m pytest tests/e2e/test_readme_lan_tip_matches_server_bind.py -v
"""

from __future__ import annotations

import contextlib
import http.client
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"

# Stand-in for the machine's LAN address (see module docstring).
_LAN_STANDIN_ADDR = "127.0.0.2"

# A cold `omnigent server` imports the whole stack before serving; reuse the
# suite-wide boot budget with the same margin as the other server-spawn e2es.
_BOOT_TIMEOUT_S = HEALTH_TIMEOUT_S * 2

# Ambient config/credential vars would leak the harness's own setup into the
# server under test or break HOME isolation.
_ENV_PREFIXES_TO_CLEAR = ("DATABRICKS_", "ANTHROPIC_", "OPENAI_", "OMNIGENT_")


def _free_port() -> int:
    """Bind port 0 and return the OS-assigned free TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_env(home: Path) -> dict[str, str]:
    """Isolated environment for the ``omnigent server`` subprocess.

    :param home: Isolated ``$HOME`` so the sqlite DB / pidfile / logs never
        touch the real ``~/.omnigent``.
    :returns: The environment mapping for :class:`subprocess.Popen`.
    """
    env = os.environ.copy()
    for key in [k for k in env if k.startswith(_ENV_PREFIXES_TO_CLEAR)]:
        env.pop(key)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    return env


def _health_status(addr: str, port: int) -> int | None:
    """``GET /health`` status code, or ``None`` when TCP connect fails.

    :param addr: Address to connect to.
    :param port: Server port.
    :returns: The HTTP status, or ``None`` on refusal/unreachability.
    """
    try:
        conn = http.client.HTTPConnection(addr, port, timeout=5)
        try:
            conn.request("GET", "/health")
            return conn.getresponse().status
        finally:
            conn.close()
    except OSError:
        return None


def _wait_for_health(addr: str, port: int, log_path: Path) -> int:
    """Poll ``/health`` on *addr* until it answers, failing on timeout.

    :param addr: Address the server is expected to serve on.
    :param port: Server port.
    :param log_path: The server's captured stdout, surfaced on timeout.
    :returns: The first HTTP status observed.
    """
    deadline = time.time() + _BOOT_TIMEOUT_S
    while time.time() < deadline:
        status = _health_status(addr, port)
        if status is not None:
            return status
        time.sleep(POLL_INTERVAL_S)
    log_tail = log_path.read_text(errors="replace")[-2000:]
    pytest.fail(
        f"server did not answer /health on {addr}:{port} within "
        f"{_BOOT_TIMEOUT_S:.0f}s; server log tail:\n{log_tail}"
    )


@contextlib.contextmanager
def _running_server(tmp_path: Path, extra_args: list[str]) -> Iterator[tuple[int, Path]]:
    """Run a real ``omnigent server`` subprocess for the ``with`` body.

    :param tmp_path: Per-test scratch dir for the isolated HOME and log.
    :param extra_args: CLI args after ``server --port <port>`` (e.g.
        ``["--host", "0.0.0.0"]``); empty for the default bind.
    :returns: Yields ``(port, log_path)``.
    """
    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.cli", "server", "--port", str(port), *extra_args],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=_server_env(home),
            cwd=str(_REPO_ROOT),
        )
        try:
            yield port, log_path
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def _readme_lan_tip() -> str:
    """The README blockquote instructing direct LAN access, ``""`` if gone.

    :returns: The full ``>``-quoted tip block containing the LAN-address
        instruction, or an empty string when the README no longer gives one.
    """
    lines = _README.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(">"):
            continue
        if "LAN" not in line and "192.168" not in line:
            continue
        start = index
        while start > 0 and lines[start - 1].lstrip().startswith(">"):
            start -= 1
        end = index
        while end + 1 < len(lines) and lines[end + 1].lstrip().startswith(">"):
            end += 1
        return "\n".join(lines[start : end + 1])
    return ""


@pytest.mark.timeout(300)
def test_readme_lan_tip_matches_default_server_bind(tmp_path: Path) -> None:
    """The README's LAN instruction must work against the bind it implies.

    Journey: a user runs ``omnigent server`` with defaults, sees the UI on
    ``127.0.0.1``, then follows the README tip and opens the machine's LAN
    address from a phone. If that non-loopback connection is refused, the tip
    must say how to bind a reachable interface (``--host``); a tip that
    instructs LAN access the default bind refuses is the bug.
    """
    tip = _readme_lan_tip()
    if not tip:
        pytest.skip("README no longer instructs direct LAN access")

    with _running_server(tmp_path, []) as (port, log_path):
        assert _wait_for_health("127.0.0.1", port, log_path) == 200
        lan_status = _health_status(_LAN_STANDIN_ADDR, port)

    if lan_status is not None:
        return  # Default bind serves non-loopback clients: the tip works as written.

    assert "--host" in tip, (
        "README tells users to open the machine's LAN address, but the default "
        "`omnigent server` bind (127.0.0.1) refuses non-loopback clients "
        f"(connection to {_LAN_STANDIN_ADDR}:{port} was refused) and the tip "
        f"never mentions `--host`:\n{tip}"
    )


@pytest.mark.timeout(300)
def test_host_any_serves_non_loopback_clients(tmp_path: Path) -> None:
    """``--host 0.0.0.0`` — the remedy the README must point at — serves LAN clients."""
    with _running_server(tmp_path, ["--host", "0.0.0.0"]) as (port, log_path):
        assert _wait_for_health(_LAN_STANDIN_ADDR, port, log_path) == 200
