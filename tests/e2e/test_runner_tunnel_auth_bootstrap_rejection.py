"""E2E reproduction: runner tunnel auth after bootstrap-bearer rejection and
failed local renewal.

Reported journey (a ``cli`` surface — the ``omnigent`` runner process):

1. ``omnigent host`` launches a fresh runner with a host-provided bootstrap
   bearer (``OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN``) and delegated auth enabled.
2. The server rejects that bootstrap bearer on the tunnel handshake (HTTP 403).
3. The runner tries to renew locally — delegated managed-mint, then the stored
   OIDC login (``auth_tokens.json``), then the Databricks SDK — and every path
   is refused.
4. The runner exits code 1 after the fatal 403 streak, and a fresh relaunch
   fails identically. Its user-facing diagnosis says only that no credential is
   available, hiding *why* renewal failed.

Both facets are driven through the REAL runner process
(``python -m omnigent.runner._entry``) over real sockets, with a local HTTP-403
front door standing in for the Databricks Apps front door / Databricks-network
host that rejects the bootstrap bearer and every renewal request. No runner
transport or auth code is stubbed.

Facet A (journey guard): with no recoverable credential, the runner cannot
connect and exits non-zero after the fatal 403 streak; a fresh relaunch is
identical.

Facet B (fail->pass target): when a stored OIDC credential *exists* but its
refresh grant is rejected (HTTP 403), the runner must not report "no SDK/OIDC
credential is available to renew it" — an inaccurate diagnosis that hides the
actionable reason. It must surface that a stored credential existed and its
renewal was refused.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from omnigent.runner.identity import (
    RUNNER_DELEGATED_AUTH_ENV_VAR,
    RUNNER_ID_ENV_VAR,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    RUNNER_PARENT_PID_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_WORKSPACE_ENV_VAR,
    token_bound_runner_id,
)
from omnigent.runner.transports.ws_tunnel.serve import (
    _HTTP_AUTH_REJECTION_FATAL_ATTEMPTS,
    RUNNER_TUNNEL_REJECTION_PREFIX,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The runner imports ``omnigent_client`` / ``omnigent_ui_sdk``; in a worktree
# they resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

_BOOTSTRAP_BEARER = "synthetic-host-bootstrap-bearer"
_NO_CREDENTIAL_CLAIM = "no SDK/OIDC credential is available to renew it"
_RENEWAL_REFUSED_DIAGNOSIS = "refresh refused with HTTP 403"
_RUN_TIMEOUT_S = 90.0
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Reject403(BaseHTTPRequestHandler):
    """Front door that rejects every request with HTTP 403.

    Stands in for the Databricks Apps front door rejecting the bootstrap
    bearer on the tunnel handshake and refusing every runner-local renewal
    request (the OIDC ``/oauth/token`` refresh and the delegated mint POST).
    """

    protocol_version = "HTTP/1.1"
    requests: list[dict[str, str]]
    lock: threading.Lock

    def log_message(self, *_args: object) -> None:
        return

    def _reject(self) -> None:
        type(self).lock.acquire()
        try:
            type(self).requests.append({"method": self.command, "path": self.path})
        finally:
            type(self).lock.release()
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        body = b'{"error":"invalid_grant","error_description":"refresh token rejected"}'
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reject
    do_POST = _reject


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def front_door() -> Iterator[tuple[str, list[dict[str, str]]]]:
    """Start the HTTP-403 front door; yield its URL and its request log."""

    class Handler(_Reject403):
        requests: list[dict[str, str]] = []
        lock = threading.Lock()

    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", _find_free_port()), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", Handler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _runner_env(
    server_url: str, state_dir: Path, workspace: Path, log_file: Path
) -> dict[str, str]:
    """Build the exact host->runner launch env, from a clean omnigent context.

    Mirrors ``omnigent host``: a host-provided bootstrap bearer plus delegated
    auth, pointed at *server_url*. Any inherited ``OMNIGENT*`` / proxy context
    is stripped so the spawned runner boots from a clean slate.
    """
    env = {**os.environ}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    for name in list(env):
        if name.startswith(("OMNIGENT", "DATABRICKS_")) or name == "RUNNER_SERVER_URL":
            env.pop(name, None)
    binding = secrets.token_urlsafe(32)
    empty_cfg = state_dir / "empty-databrickscfg"
    empty_cfg.write_text("")
    env.update(
        {
            "PYTHONPATH": _PYTHONPATH,
            "PYTHONUNBUFFERED": "1",
            # CI shells often carry an egress proxy; localhost must bypass it.
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "OMNIGENT_DATA_DIR": str(state_dir),
            "DATABRICKS_CONFIG_FILE": str(empty_cfg),
            "RUNNER_SERVER_URL": server_url,
            RUNNER_ID_ENV_VAR: token_bound_runner_id(binding),
            RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: binding,
            RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR: _BOOTSTRAP_BEARER,
            RUNNER_DELEGATED_AUTH_ENV_VAR: "1",
            RUNNER_WORKSPACE_ENV_VAR: str(workspace),
            RUNNER_PARENT_PID_ENV_VAR: str(os.getpid()),
            PROCESS_LOG_FILE_ENV_VAR: str(log_file),
            "OMNIGENT_LOG_LEVEL": "INFO",
        }
    )
    return env


def _write_expired_stored_login(state_dir: Path, server_url: str) -> None:
    """Simulate a completed ``omnigent login`` whose grant is now invalid.

    Writes an expired stored OIDC session (with a refresh token) so the runner
    genuinely *has* a credential to renew — its refresh POST is what the front
    door then rejects.
    """
    (state_dir / "auth_tokens.json").write_text(
        json.dumps(
            {
                server_url.rstrip("/"): {
                    "token": "expired-oidc-access-token",
                    "expires_at": time.time() - 60,
                    "refresh_token": "rejected-refresh-grant",
                }
            }
        )
    )


def _run_runner(
    server_url: str, *, with_stored_login: bool
) -> tuple[int | None, str, list[dict[str, str]]]:
    """Launch the real runner once against *server_url*; return exit + output.

    :returns: ``(returncode, combined_output, front_door_requests_snapshot)``
        where combined_output is stderr + the runner process log (ANSI
        stripped).
    """
    tmp = Path(tempfile.mkdtemp(prefix="runner-tunnel-auth-"))
    state_dir = tmp / "state"
    state_dir.mkdir()
    workspace = tmp / "ws"
    workspace.mkdir()
    log_file = tmp / "runner.log"
    if with_stored_login:
        _write_expired_stored_login(state_dir, server_url)
    env = _runner_env(server_url, state_dir, workspace, log_file)
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        cwd=str(workspace),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _out, err = proc.communicate(timeout=_RUN_TIMEOUT_S)
        rc: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        _out, err = proc.communicate()
        rc = None
    log_text = log_file.read_text() if log_file.exists() else ""
    combined = _ANSI.sub("", f"{err}\n{log_text}")
    return rc, combined, []


def test_runner_exits_after_bootstrap_rejection_with_no_recoverable_credential(
    front_door: tuple[str, list[dict[str, str]]],
) -> None:
    """Facet A journey guard: bootstrap 403 + no recoverable credential ->
    runner cannot connect, exits non-zero after the fatal 403 streak, and a
    fresh relaunch fails identically."""
    server_url, _requests = front_door

    rc1, out1, _ = _run_runner(server_url, with_stored_login=False)
    rc2, out2, _ = _run_runner(server_url, with_stored_login=False)

    for rc, out in ((rc1, out1), (rc2, out2)):
        assert rc is not None, f"runner hung instead of exiting; output tail:\n{out[-2000:]}"
        assert rc != 0, (
            f"runner exited 0 despite a rejected bootstrap bearer; output tail:\n{out[-2000:]}"
        )
        assert RUNNER_TUNNEL_REJECTION_PREFIX in out, (
            f"missing the fatal tunnel-rejection diagnostic; output tail:\n{out[-2000:]}"
        )
        assert f"persisted across {_HTTP_AUTH_REJECTION_FATAL_ATTEMPTS} attempts" in out, (
            "fatal rejection did not report the expected consecutive-403 streak; "
            f"output tail:\n{out[-2000:]}"
        )


def test_runner_failure_diagnosis_surfaces_rejected_refresh_reason(
    front_door: tuple[str, list[dict[str, str]]],
) -> None:
    """Facet B fail->pass target: a stored OIDC credential exists but its
    refresh grant is rejected. The runner attempts the refresh (proving a
    credential existed), so it must not claim "no SDK/OIDC credential is
    available to renew it"; it must surface that the stored credential's
    renewal was refused."""
    server_url, requests = front_door

    rc, out, _ = _run_runner(server_url, with_stored_login=True)

    assert rc is not None, f"runner hung instead of exiting; output tail:\n{out[-2000:]}"
    assert rc != 0, f"runner exited 0 despite rejected renewal; output tail:\n{out[-2000:]}"

    # A stored credential existed: the runner attempted to refresh it, and the
    # front door rejected that refresh (HTTP 403). This is what makes the
    # "no credential is available" claim below inaccurate.
    refresh_posts = [
        r
        for r in requests
        if r["method"] == "POST" and ("oauth" in r["path"] or "token" in r["path"])
    ]
    assert refresh_posts, (
        "expected the runner to attempt a refresh of the stored OIDC login "
        f"(POST /oauth/token); front-door requests were: {requests}"
    )

    # Fixed behaviour: because a stored credential existed and its renewal was
    # rejected, the runner must not report that none was available.
    assert _NO_CREDENTIAL_CLAIM not in out, (
        "runner inaccurately claimed no credential was available even though a "
        "stored OIDC login existed and its refresh was rejected (HTTP 403); "
        f"output tail:\n{out[-2000:]}"
    )

    # And its own failure diagnosis must name the refusal.
    assert _RENEWAL_REFUSED_DIAGNOSIS in out, (
        "runner did not surface why the stored login's renewal failed; "
        f"output tail:\n{out[-2000:]}"
    )
