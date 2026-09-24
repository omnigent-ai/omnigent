"""
Failed Databricks auth must not leak a host daemon.

A user with no saved credentials points the CLI at a Databricks-fronted
omnigent server (``omnigent run <agent> --server <url>``). The remote
connect pre-flight fails with the "Not signed in" hint; the launch must
not leave a host daemon running (``omnigent stop`` finds nothing to
stop) and no daemon may dial the server's ``/v1/hosts/<id>/tunnel``
endpoint with the pre-auth (missing) credentials.

The test drives the real user journey against a stub HTTP server that
plays the Databricks Apps edge the way a live deployment answers an
unauthenticated request: HTTP 302 to the fronting workspace's OIDC
authorize endpoint (the signature the pre-flight keys on; verified
against a live ``*.databricksapps.com`` deployment). The staged state is
a completely fresh ``$HOME`` — the user never logged in.

Usage::

    python -m pytest tests/e2e/test_databricks_auth_daemon_race.py -v
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Launch budget: agent bundle prep + pre-flight + daemon spawn against a
# loopback stub; failure is fast, the ceiling covers cold imports on CI.
_RUN_TIMEOUT_S = 120

# How long a spawned daemon gets to dial the stub's tunnel endpoint after
# the launch returns (observed live: first dial ~8s after the spawn).
_TUNNEL_DIAL_GRACE_S = 30

# Ambient credentials/config that would leak into the subprocess and defeat
# the staged never-logged-in state (CI runners carry Databricks vars).
_ENV_TO_CLEAR = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CONFIG_FILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    # Proxy vars would route the loopback stub through a proxy that can't
    # reach it (the CLI's clients pass trust_env for non-loopback URLs).
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


class _DatabricksEdgeHandler(BaseHTTPRequestHandler):
    """Plays a Databricks Apps edge fronting omnigent, pre-login.

    Every request bounces with a 302 to the fronting workspace's OIDC
    authorize endpoint — the shape a live ``*.databricksapps.com`` edge
    answers before the user has authenticated (the edge rejects before
    routing, so every path answers the same).

    ``/.well-known/databricks-config`` 404s so the databricks-sdk's host
    metadata probe fails fast instead of retrying.
    """

    protocol_version = "HTTP/1.1"

    # Populated by the fixture: every request seen, for asserting whether a
    # daemon dialed the tunnel endpoint during/after the failed launch.
    requests_seen: list[tuple[str, str, bool]] = []

    def _drain_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                break
            length -= len(chunk)

    def _answer(self) -> None:
        self._drain_body()
        has_auth = bool(self.headers.get("Authorization"))
        type(self).requests_seen.append((self.command, self.path, has_auth))
        if self.path.startswith("/.well-known/"):
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        else:
            # Name the stub itself (as https) as the fronting workspace: the
            # credential chain's TLS handshake against the plain-HTTP port
            # fails fast and non-retryably, like a workspace the user holds
            # no grant for (an unresolvable host hits the SDK's retry loop).
            body = b""
            self.send_response(302)
            self.send_header(
                "Location",
                f"https://{self.headers.get('Host')}/oidc/oauth2/v2.0/authorize"
                "?response_type=code&scope=default",
            )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer
    do_PATCH = _answer
    do_PUT = _answer
    do_DELETE = _answer

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def databricks_edge() -> Iterator[str]:
    """Run the stub Databricks edge on a free loopback port.

    :yields: The server URL, e.g. ``"http://127.0.0.1:8471"``.
    """
    _DatabricksEdgeHandler.requests_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DatabricksEdgeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _launch_env(home: Path) -> dict[str, str]:
    """Subprocess env: isolated HOME/state, no ambient creds, no proxies.

    :param home: The isolated home directory.
    :returns: Environment for the ``omnigent`` subprocesses.
    """
    env = os.environ.copy()
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    env["DATABRICKS_CONFIG_FILE"] = str(home / ".databrickscfg")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
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


def _run_cli(home: Path, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-P", "-m", "omnigent.cli", *args],
        env=_launch_env(home),
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _daemon_records(home: Path) -> list[Path]:
    return sorted((home / ".omnigent" / "daemons").glob("*.json"))


@pytest.fixture
def fresh_home(tmp_path: Path) -> Iterator[Path]:
    """A never-logged-in home; tears down any daemon the journey leaves behind."""
    home = tmp_path / "home"
    (home / ".omnigent").mkdir(parents=True)
    (home / "agent.yaml").write_text(
        "name: auth-daemon-race-repro\n"
        "description: Minimal agent for the auth/daemon race journey.\n"
        "executor:\n"
        "  model: gpt-4o\n"
        "prompt: |\n"
        "  You are a test agent.\n"
    )
    try:
        yield home
    finally:
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            _run_cli(home, "stop", "--force", timeout=60)


@pytest.mark.timeout(_RUN_TIMEOUT_S + _TUNNEL_DIAL_GRACE_S + 120)
def test_failed_databricks_auth_leaves_no_daemon_and_no_unauthed_tunnel(
    databricks_edge: str,
    fresh_home: Path,
) -> None:
    """A launch that fails Databricks auth must not leave a daemon behind.

    Journey: never logged in -> ``omnigent run agent.yaml --server <url>
    -p hi`` against a Databricks-fronted server -> the pre-flight fails
    with the "Not signed in" hint -> the user runs ``omnigent stop``.

    The failed pre-flight must prevent the daemon spawn: no tunnel dial
    with the missing credentials, and ``omnigent stop`` finds no daemon.
    """
    proc = _run_cli(
        fresh_home,
        "run",
        str(fresh_home / "agent.yaml"),
        "--server",
        databricks_edge,
        "-p",
        "hi",
        timeout=_RUN_TIMEOUT_S,
    )
    output = proc.stdout + proc.stderr

    # Journey sanity: the launch must fail with the sign-in hint (this part
    # is not the bug and already behaves).
    assert proc.returncode != 0, f"launch unexpectedly succeeded with no credentials:\n{output}"
    assert "Not signed in" in output and "omnigent login" in output, (
        f"launch did not fail with the Databricks sign-in hint:\n{output}"
    )

    # Give any spawned daemon time to dial the tunnel; when none was
    # spawned, the empty registry ends the wait immediately.
    deadline = time.monotonic() + _TUNNEL_DIAL_GRACE_S
    while time.monotonic() < deadline:
        if any("/tunnel" in path for _, path, _ in _DatabricksEdgeHandler.requests_seen):
            break
        if not _daemon_records(fresh_home):
            break
        time.sleep(0.5)

    records = [record.read_text() for record in _daemon_records(fresh_home)]
    tunnel_dials = [
        (method, path, has_auth)
        for method, path, has_auth in _DatabricksEdgeHandler.requests_seen
        if "/tunnel" in path
    ]

    # The user-facing orphan check doubles as cleanup: stopping here keeps a
    # failing run from leaking the daemon into the rest of the suite.
    stop = _run_cli(fresh_home, "stop", timeout=60)
    stop_output = stop.stdout + stop.stderr

    violations: list[str] = []
    if tunnel_dials:
        violations.append(
            "a host daemon dialed the server's tunnel endpoint during the "
            f"failed-auth launch (pre-auth credentials): {tunnel_dials}"
        )
    if "daemon(s)" in stop_output:
        violations.append(
            "the failed launch left an orphaned host daemon behind: "
            f"`omnigent stop` reported {stop_output.strip()!r} "
            f"(daemon records at exit: {records})"
        )
    assert not violations, (
        "Databricks auth failed but the host daemon startup was not gated on "
        "it:\n- " + "\n- ".join(violations) + f"\n--- launch output ---\n{output}"
    )
