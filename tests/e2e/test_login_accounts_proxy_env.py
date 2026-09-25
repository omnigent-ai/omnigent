"""``omnigent login`` must detect accounts auth even under an env proxy.

An accounts-mode server
answers unauthenticated ``GET /v1/me`` with ``401 {"login_url": "/login"}``,
and ``omnigent login`` routes on that payload to the username/password flow.
But the login command runs its probe with plain httpx that honors
``HTTP(S)_PROXY``, so when the machine carries a proxy and the server host is
not in ``NO_PROXY`` the proxy's own answer (an unconditional 403 here) replaces
the server's 401 payload. Login then silently falls into the OIDC ticket flow
and dies on ``POST /auth/cli-login`` with the misleading "Is the server running
with OMNIGENT_AUTH_PROVIDER=oidc?" error -- even though ``omnigent diagnose``
(which probes with ``trust_env=False``) reports accounts auth on the same host.

Loopback traffic must never be routed through a proxy (``is_loopback_url`` /
``_trust_env_for``), so login must still detect accounts auth, prompt for
credentials, and complete. This drives the real journey: a real
``omnigent server`` accounts subprocess, a deny-all env proxy, and the real
``omnigent login`` command on a PTY.

Usage::

    python -m pytest tests/e2e/test_login_accounts_proxy_env.py -v --timeout=300
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests._helpers.live_server import find_free_port

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_COOKIE_SECRET_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
_ADMIN_USERNAME = "admin"
_ADMIN_PASSWORD = "accounts-login-test-pw"
_SERVER_HEALTH_TIMEOUT_S = 60.0
_LOGIN_TIMEOUT_S = 60


def _await_health(base_url: str, log_path: Path) -> None:
    deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    tail = log_path.read_text()[-3000:] if log_path.exists() else "(no log)"
    raise RuntimeError(f"accounts server did not become healthy. Log:\n{tail}")


@pytest.fixture()
def deny_all_proxy() -> Iterator[str]:
    """An HTTP proxy that answers 403 to every request, as a base URL.

    Models a corporate proxy that refuses the target host: any client that
    routes login traffic through the environment proxy sees this 403 instead
    of the omnigent server's real responses.
    """

    class _DenyAll(BaseHTTPRequestHandler):
        def _deny(self) -> None:
            body = b"blocked by proxy policy"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_CONNECT = _deny

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _DenyAll)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def accounts_server(tmp_path: Path) -> Iterator[str]:
    """Run a real ``omnigent server`` subprocess with accounts auth enabled."""
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_path / "server.log"

    env = {**os.environ}
    env["OMNIGENT_AUTH_PROVIDER"] = "accounts"
    env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = _COOKIE_SECRET_HEX
    env["OMNIGENT_ACCOUNTS_BASE_URL"] = base_url
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME"] = _ADMIN_USERNAME
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD"] = _ADMIN_PASSWORD
    env.pop("OMNIGENT_OIDC_ISSUER", None)
    apply_server_env(env, _REPO_ROOT)

    log_handle = open(log_path, "w")  # noqa: SIM115 -- handle lives for the subprocess
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'e2e.db'}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _await_health(base_url, log_path)
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


def test_login_detects_accounts_auth_despite_proxy_env(
    accounts_server: str, deny_all_proxy: str, tmp_path: Path
) -> None:
    """Login must run the accounts flow, not fall into OIDC, under proxy env."""
    home = tmp_path / "home"
    home.mkdir()

    env = {**os.environ}
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["HOME"] = str(home)
    env["TERM"] = "xterm"
    for var in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(var, None)
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        env[var] = deny_all_proxy

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent.cli", "login", accounts_server],
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        timeout=_LOGIN_TIMEOUT_S,
    )
    try:
        index = child.expect(["Username", "auth/cli-login", pexpect.EOF])
        if index != 0:
            pytest.fail(
                "omnigent login misdetected the accounts server and fell into "
                "the OIDC ticket flow instead of prompting for username/"
                f"password. Output:\n{child.before}"
            )
        child.sendline(_ADMIN_USERNAME)
        child.expect("Password")
        child.sendline(_ADMIN_PASSWORD)
        child.expect(f"Logged in as {_ADMIN_USERNAME}")
        child.expect(pexpect.EOF)
    finally:
        child.close(force=True)
    assert child.exitstatus == 0
