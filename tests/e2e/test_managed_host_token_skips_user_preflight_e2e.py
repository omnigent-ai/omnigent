"""``omnigent host`` started with a server-injected launch token.

A server-managed sandbox host is launched as ``OMNIGENT_HOST_TOKEN=…
OMNIGENT_HOST_ID=… OMNIGENT_HOST_NAME=… omnigent host --server <url>`` and its
tunnel authenticates with that token alone — a sandbox holds no user
credentials. The user sign-in preflight must stay out of its way: on a
Databricks-fronted server the preflight's unauthenticated ``GET /v1/me`` is
redirected to the workspace login and, with no user credential to mint, the
start would fail with the ``omnigent login`` hint before the tunnel is dialed.

A loopback stub plays the Databricks Apps edge. A request without credentials
answers 302 to the workspace ``/oidc/`` authorize endpoint; a request carrying
``X-Omnigent-Host-Token`` is recorded and refused with 400 so the host exits
instead of reconnecting. The stub never accepts the WebSocket upgrade, so a
successful token-authenticated tunnel is outside what this test shows.

Usage::

    python -m pytest tests/e2e/test_managed_host_token_skips_user_preflight_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
    MANAGED_HOST_TOKEN_HEADER,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

LAUNCH_TOKEN = "e2e-launch-token"
HOST_ID = "3f9d2c1b8a7e4f60b5c4d3e2f1a09876"
HOST_NAME = "managed-e2e"
TUNNEL_PATH = f"/v1/hosts/{HOST_ID}/tunnel"
# The Apps edge names the fronting workspace in its redirect; a reserved
# `.invalid` host keeps the SDK's credential lookup offline and empty.
WORKSPACE_AUTHORIZE_URL = "https://e2e-workspace.invalid/oidc/oauth2/v2.0/authorize?client_id=x"

# Identity load + one tunnel dial against a loopback stub; the ceiling only
# covers cold interpreter and import time on a loaded CI box.
HOST_TIMEOUT_S = 120

_ENV_TO_CLEAR = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_DATABASE_URI",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)
# DATABRICKS_* could hand the host a user credential; the runner/host variables
# are what a server-spawned agent session leaks into its children.
_ENV_PREFIXES_TO_CLEAR = ("DATABRICKS_", "OMNIGENT_RUNNER_", "OMNIGENT_HOST_")


@dataclass
class SeenRequest:
    """One request line the edge stub answered."""

    method: str
    path: str
    has_authorization: bool
    host_token: str | None


@dataclass
class DatabricksAppsEdge:
    """Loopback stand-in for the Databricks Apps edge fronting an omnigent server."""

    url: str = ""
    requests: list[SeenRequest] = field(default_factory=list)
    tunnel_dialed: threading.Event = field(default_factory=threading.Event)
    # Handler threads record while the test thread reads.
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, seen: SeenRequest) -> None:
        with self.lock:
            self.requests.append(seen)
        if seen.path == TUNNEL_PATH and seen.host_token:
            self.tunnel_dialed.set()

    def seen_requests(self) -> list[SeenRequest]:
        with self.lock:
            return list(self.requests)

    def tunnel_requests(self) -> list[SeenRequest]:
        return [seen for seen in self.seen_requests() if seen.path == TUNNEL_PATH]


class _EdgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    edge: DatabricksAppsEdge

    def do_GET(self) -> None:
        seen = SeenRequest(
            method=self.command,
            path=self.path,
            has_authorization=bool(self.headers.get("Authorization")),
            host_token=self.headers.get(MANAGED_HOST_TOKEN_HEADER),
        )
        self.edge.record(seen)
        if seen.host_token:
            body = b"stub edge recorded the launch-token request"
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
        elif seen.has_authorization:
            body = b'{"error_code": 403, "message": "Invalid access token."}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
        else:
            body = b""
            self.send_response(302)
            self.send_header("Location", WORKSPACE_AUTHORIZE_URL)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def serve_databricks_apps_edge() -> Iterator[DatabricksAppsEdge]:
    """Serve the edge stub on a free loopback port for the duration of the block."""
    edge = DatabricksAppsEdge()
    handler = type("_BoundEdgeHandler", (_EdgeHandler,), {"edge": edge})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    edge.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield edge
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def databricks_apps_edge() -> Iterator[DatabricksAppsEdge]:
    with serve_databricks_apps_edge() as edge:
        yield edge


@pytest.fixture
def credentialless_home(tmp_path: Path) -> Path:
    """A sandbox-like ``$HOME``: no ``omnigent login`` record, no Databricks profile."""
    home = tmp_path / "home"
    (home / ".omnigent").mkdir(parents=True)
    (home / ".databrickscfg").write_text("")
    return home


def host_launch_env(home: Path) -> dict[str, str]:
    """Environment for the host subprocess, shaped like a managed-sandbox launch."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _ENV_TO_CLEAR and not key.startswith(_ENV_PREFIXES_TO_CLEAR)
    }
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
            "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "TERM": "dumb",
            # A credential-less managed sandbox has no databricks user-CLI OAuth;
            # point the SDK at a missing CLI so it fails fast like production
            # rather than hanging on a runner's installed `databricks` binary.
            "DATABRICKS_CLI_PATH": "databricks-not-installed",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(_REPO_ROOT),
                    str(_REPO_ROOT / "sdks" / "python-client"),
                    str(_REPO_ROOT / "sdks" / "ui"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
            HOST_TOKEN_ENV_VAR: LAUNCH_TOKEN,
            HOST_ID_ENV_VAR: HOST_ID,
            HOST_NAME_ENV_VAR: HOST_NAME,
        }
    )
    return env


def run_host_until_tunnel_or_exit(
    edge: DatabricksAppsEdge, home: Path, *, timeout_s: float = HOST_TIMEOUT_S
) -> tuple[int | None, str]:
    """Start the host and return its exit code and combined output.

    Returns once the process exits on its own, or shortly after the stub has
    recorded the tunnel dial (the stub's 400 is permanent, so a host that got
    that far is given a moment to report it and is then stopped).
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.cli", "host", edge.url, "--non-interactive"],
        env=host_launch_env(home),
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.extend(proc.stdout), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_s
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            if edge.tunnel_dialed.wait(0.2):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=20)
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    # The process is dead here, so stdout reaches EOF unless a child kept the
    # pipe open; the bounded join turns that into a diagnosable failure.
    reader.join(timeout=30)
    assert not reader.is_alive(), "host stdout stayed open after exit (a child kept the pipe)"
    return proc.returncode, "".join(chunks)


@pytest.mark.timeout(HOST_TIMEOUT_S + 90)
def test_launch_token_host_skips_user_sign_in_preflight(
    databricks_apps_edge: DatabricksAppsEdge, credentialless_home: Path
) -> None:
    """A launch-token host dials its tunnel without being sent to user sign-in."""
    returncode, output = run_host_until_tunnel_or_exit(databricks_apps_edge, credentialless_home)

    assert "Traceback (most recent call last)" not in output, output
    assert "Not signed in" not in output and "omnigent login" not in output, (
        "the launch-token host was sent to user sign-in before dialing its tunnel "
        f"(exit {returncode}):\n{output}"
    )
    assert f"Connecting to {databricks_apps_edge.url} as {HOST_NAME!r} ({HOST_ID})" in output, (
        f"host never reached the connect step (exit {returncode}):\n{output}"
    )
    seen = databricks_apps_edge.seen_requests()
    assert all(request.path != "/v1/me" for request in seen), (
        f"the user sign-in probe still reached the edge: {seen}"
    )
    tunnel = databricks_apps_edge.tunnel_requests()
    assert tunnel, (
        "no tunnel upgrade reached the edge; requests seen: "
        f"{databricks_apps_edge.seen_requests()}\n{output}"
    )
    assert all(
        seen.host_token == LAUNCH_TOKEN and not seen.has_authorization for seen in tunnel
    ), tunnel
    # The stub's 400 is permanent, so the host gives up on its own.
    assert returncode == HOST_FATAL_EXIT_CODE, (
        f"host did not exit on the refused tunnel (exit {returncode}):\n{output}"
    )
