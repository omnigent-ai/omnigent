"""
Expired-Databricks-credential launch journey.

A user logs in once to a Databricks-fronted omnigent server (``omnigent
login <url>`` stores a pointer record naming the fronting workspace), then
lets the underlying Databricks OAuth refresh token lapse. Launching
``omnigent run <agent> --server <url>`` should detect the expired
credential during the pre-flight (``_ensure_databricks_server_auth``) and
offer the reauth flow (the browser ``databricks auth login`` on a TTY, or
the exact login command headlessly) — instead the launch dies much later,
at ``sessions.create()``, with the edge's raw
``{'error_code': 403, 'message': 'Invalid access token. [ReqId: ...]'}``.

The test drives the REAL user journey: a stub HTTP server plays the
Databricks edge exactly as a live workspace ``/api/2.0/omnigent`` mount
answers (verified against a real deployment):

- a request WITH a bearer the workspace rejects -> HTTP 403 with the
  ``Invalid access token`` JSON body;
- a request WITHOUT ``Authorization`` -> HTTP 401 with
  ``WWW-Authenticate: Bearer realm="DatabricksRealm"``.

The expired credential is staged the way a real lapse looks to the auth
chain: the ``omnigent login`` pointer record exists, and the workspace
credential store still resolves a (now-invalid) bearer — a stale ``token``
in ``~/.databrickscfg`` for the workspace host. The CLI's credential chain
happily mints that stale bearer, the ``/v1/me`` pre-flight probe answers
403 (which ``_databricks_workspace_login_target`` does not recognize as
the Databricks edge), and the run proceeds to die at session-create.

Regression contract (what the fix must make true): a launch holding only
an expired/invalid Databricks credential against a Databricks-fronted
server must NOT surface the raw edge 403 — it must instead surface the
reauth path (mention ``omnigent login`` / ``databricks auth login``), as
the pre-flight already does when the credential chain resolves nothing.

Usage::

    python -m pytest tests/e2e/test_expired_databricks_credential_launch.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact edge message from the crash report; the stub answers with it and
# the assertion below checks the CLI does NOT parrot it at the user.
_EDGE_403_MESSAGE = "Invalid access token. [ReqId: 145ae51b-da74-40a7-ac56-0ad190834faf]"

# Launch budget: agent bundle prep + pre-flight + session-create against a
# loopback stub — no runner ever comes online, so failure is fast; the
# ceiling only covers cold interpreter + import time on a loaded CI box.
_RUN_TIMEOUT_S = 120

# Ambient credentials/config that would leak into the subprocess and defeat
# the staged expired-credential state (CI runners carry Databricks vars).
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
    """Plays a Databricks workspace API mount fronting omnigent.

    Behavior mirrors a live workspace ``/api/2.0/omnigent`` mount probed
    with and without credentials (the edge rejects before routing, so
    every path answers the same):

    - ``Authorization`` present -> 403 + the ``Invalid access token`` body
      (what an expired/invalid OAuth bearer gets);
    - no ``Authorization`` -> 401 + ``WWW-Authenticate: Bearer
      realm="DatabricksRealm"`` (the signature
      ``_databricks_workspace_login_target`` keys on).

    ``/.well-known/databricks-config`` 404s so the databricks-sdk's host
    metadata probe fails fast instead of retrying.
    """

    protocol_version = "HTTP/1.1"

    # Populated by the fixture: every request line seen, for assertions on
    # what the CLI actually sent (e.g. that the create POST happened).
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
        elif has_auth:
            body = json.dumps({"error_code": 403, "message": _EDGE_403_MESSAGE}).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
        else:
            body = json.dumps(
                {
                    "error_code": 401,
                    "message": (
                        "Credential was not sent or was of an unsupported type for this API."
                    ),
                }
            ).encode()
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="DatabricksRealm"')
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer
    do_PATCH = _answer
    do_PUT = _answer
    do_DELETE = _answer

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture
def databricks_edge() -> Iterator[str]:
    """Run the stub Databricks edge on a free loopback port.

    :yields: The server URL, e.g. ``"http://127.0.0.1:8471"``.
    """
    _DatabricksEdgeHandler.requests_seen = []
    # Port 0 lets the OS pick a free port; read it back off the bound server.
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


@pytest.fixture
def expired_credential_home(tmp_path: Path, databricks_edge: str) -> Path:
    """Stage the post-expiry state a real user's machine is left in.

    ``omnigent login <url>`` stored a Databricks pointer record naming the
    fronting workspace; the workspace credential store still resolves a
    bearer, but the workspace now rejects it (expired). The stale bearer
    comes from a ``~/.databrickscfg`` profile pinned to the workspace host
    — the first source ``_resolve_databricks_auth_for_host`` consults.

    :returns: The isolated ``$HOME`` directory.
    """
    home = tmp_path / "home"
    data_dir = home / ".omnigent"
    data_dir.mkdir(parents=True)
    (home / ".databrickscfg").write_text(
        f"[stale]\nhost = {databricks_edge}\ntoken = stale-access-token\n"
    )
    (data_dir / "auth_tokens.json").write_text(
        json.dumps(
            {
                databricks_edge: {
                    "auth_type": "databricks",
                    "workspace_host": databricks_edge,
                    "user_id": "user@example.com",
                }
            },
            indent=2,
        )
    )
    return home


@pytest.fixture
def agent_yaml(tmp_path: Path) -> Path:
    """Write the minimal agent spec the user launches."""
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: expired-credential-repro\n"
        "description: Minimal agent for the expired-credential launch repro.\n"
        "executor:\n"
        "  model: gpt-4o\n"
        "prompt: |\n"
        "  You are a test agent.\n"
    )
    return path


def _launch_env(home: Path) -> dict[str, str]:
    """Subprocess env: isolated HOME/state, no ambient creds, no proxies.

    :param home: The staged isolated home directory.
    :returns: Environment for the ``omnigent run`` subprocess.
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


@pytest.mark.timeout(_RUN_TIMEOUT_S + 60)
def test_expired_databricks_credential_offers_reauth_not_edge_403(
    databricks_edge: str,
    expired_credential_home: Path,
    agent_yaml: Path,
) -> None:
    """An expired Databricks credential must surface reauth, not the raw 403.

    Journey: login once (staged pointer record) -> refresh token lapses
    (staged stale bearer the edge rejects) -> ``omnigent run <agent>
    --server <url> -p hi``.

    Before the fix the launch reaches ``POST /v1/sessions`` with the stale
    bearer and reports the edge's raw ``Invalid access token`` 403 (in the
    reported version, as an uncaught-crash traceback; on current main, as
    ``Error: Could not start a session on ...``). Either way the user is
    never told to re-authenticate. The fix must detect the expired
    credential (the ``/v1/me`` pre-flight probe already answers 403 to the
    stale bearer) and surface the login flow / command instead.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "run",
            str(agent_yaml),
            "--server",
            databricks_edge,
            "-p",
            "hi",
        ],
        env=_launch_env(expired_credential_home),
        cwd=str(expired_credential_home),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
    )
    output = proc.stdout + proc.stderr

    # The journey must fail (the credential IS invalid) — but gracefully.
    assert proc.returncode != 0, (
        f"launch unexpectedly succeeded against the rejecting edge:\n{output}"
    )

    # Never a crash-handler screen or raw traceback (the reported symptom).
    assert "Traceback (most recent call last)" not in output, (
        f"launch crashed with a traceback instead of a friendly error:\n{output}"
    )

    # THE BUG: the raw edge 403 reaches the user with no reauth remedy.
    # After the fix, the expired credential is caught (at the pre-flight or
    # at session-create) and the user is pointed at the login flow.
    mentions_reauth = "omnigent login" in output or "databricks auth login" in output
    surfaced_edge_403 = "Invalid access token" in output
    assert mentions_reauth and not surfaced_edge_403, (
        "Expired Databricks credential was not routed to reauth: expected the "
        "launch to surface the login flow (`omnigent login <url>` / "
        "`databricks auth login`) instead of the edge's raw 403 "
        f"'Invalid access token' failure.\n--- CLI output ---\n{output}"
    )
