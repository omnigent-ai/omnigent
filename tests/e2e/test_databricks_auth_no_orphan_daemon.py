"""E2E regression test: a failed Databricks auth must not orphan a host daemon.

Reproduces the failure mode where, on a *remote connect* to a
Databricks-fronted Omnigent server with no saved credentials,
``_ensure_backend`` ran Databricks auth and the host-daemon spawn
**concurrently** on a ``ThreadPoolExecutor`` (``omnigent/cli.py``). The daemon
connected to the server before auth completed, so:

* the first tunnel connect dials the server **unauthenticated** (auth hasn't
  written credentials yet), and
* when auth then fails (no credentials, non-TTY), the CLI exits non-zero but
  the already-spawned daemon is **left running as an orphan**.

The user journey is::

    omnigent claude --server <databricks-fronted-url>   # no saved credentials

which reaches ``_ensure_backend`` before touching the harness binary. This test
drives that exact command against a mock Databricks Apps *edge* — a local HTTP
server that answers every request with the authentic Databricks Apps
signature (``302`` to ``https://<workspace>/oidc/oauth2/v2.0/authorize?...``),
the same shape ``_databricks_workspace_login_target`` recognizes — with an
isolated ``HOME`` so the daemon registry is scoped to the test.

It asserts the **correct post-fix behaviour** (auth is sequenced before the
daemon starts, so a failed auth spawns no daemon at all):

* the CLI fails loud with the actionable "Not signed in ... Databricks-fronted"
  auth error (this holds before and after the fix — it confirms the journey),
* **no orphaned host daemon** remains alive after the CLI exits, and
* the edge saw **no (unauthenticated) tunnel connect**.

On the current (buggy) build the last two assertions fail: a daemon is orphaned
and it dials ``/v1/hosts/<id>/tunnel`` with no ``Authorization`` header.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_databricks_auth_no_orphan_daemon.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# A workspace host for the mock edge's OIDC redirect. Any https host works;
# _databricks_workspace_login_target only checks the scheme + /oidc/ path.
_MOCK_WORKSPACE = "https://mock-workspace.cloud.databricks.com"

# Env that must not leak into the spawned CLI/daemon: real Databricks creds
# (would let auth succeed), a shared data dir (would escape HOME isolation),
# and any ambient runner/host wiring from a server-spawned runner (would push
# the daemon down the zygote path).
_ENV_STRIP_PREFIXES = ("DATABRICKS_", "OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_ENV_STRIP_EXACT = frozenset(
    {
        "OMNIGENT_DATA_DIR",
        "RUNNER_SERVER_URL",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
    }
)

# Bounded wait, after the CLI exits, for an orphaned daemon to attempt its
# first tunnel connect. On the buggy build the daemon dials in well under this
# (observed ~6-9s after spawn); on a fixed build no daemon is spawned, so the
# wait elapses cleanly and the "no tunnel connect" assertion passes.
_TUNNEL_WAIT_S = 30.0


class _DatabricksEdge:
    """A mock Databricks Apps edge: every request -> 302 to the OIDC authorize.

    Records each request (path + whether it carried an ``Authorization``
    header), and signals when a host-tunnel connect (``/v1/hosts/.../tunnel``)
    arrives. Optionally holds the ``/v1/me`` auth probe until a tunnel connect
    is seen, to make the "daemon connects before auth completes" ordering
    observable — mirroring the human-scale latency of a real interactive login.
    """

    def __init__(self, *, hold_auth_probe: bool) -> None:
        self._hold_auth_probe = hold_auth_probe
        self._lock = threading.Lock()
        self.requests: list[dict[str, object]] = []
        self.tunnel_connected = threading.Event()
        self._auth_probe_answered = threading.Event()
        edge = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                has_auth = any(k.lower() == "authorization" for k in self.headers)
                with edge._lock:
                    edge.requests.append(
                        {
                            "method": self.command,
                            "path": self.path,
                            "authorization": has_auth,
                            "upgrade": self.headers.get("Upgrade", ""),
                        }
                    )
                if "/tunnel" in self.path:
                    edge.tunnel_connected.set()
                if self.path.startswith("/v1/me") and edge._hold_auth_probe:
                    # Hold the auth probe until a tunnel connect lands (or a
                    # cap comfortably under the CLI's 10s probe timeout), so
                    # the daemon's connect provably races the pending auth.
                    edge.tunnel_connected.wait(timeout=8.0)
                    edge._auth_probe_answered.set()
                # Databricks Apps edge signature: 302 -> workspace OIDC authorize.
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"{_MOCK_WORKSPACE}/oidc/oauth2/v2.0/authorize?client_id=databricks-apps",
                )
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle
            do_HEAD = _handle

            def log_message(self, *args: object) -> None:  # silence access log
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def tunnel_requests(self) -> list[dict[str, object]]:
        with self._lock:
            return [r for r in self.requests if "/tunnel" in str(r["path"])]

    def auth_probe_answered_before_tunnel(self) -> bool:
        # True only if we held the probe and the tunnel arrived first.
        return self.tunnel_connected.is_set() and not self._auth_probe_answered.is_set()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _daemon_records(home: Path) -> list[dict[str, object]]:
    """Return the daemon registry records written under an isolated HOME."""
    registry = home / ".omnigent" / "daemons"
    records: list[dict[str, object]] = []
    if not registry.is_dir():
        return records
    for path in registry.glob("*.json"):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return records


def _alive_daemon_pids(home: Path) -> list[int]:
    return [
        int(rec["pid"])
        for rec in _daemon_records(home)
        if isinstance(rec.get("pid"), (int, str)) and _pid_alive(int(rec["pid"]))
    ]


def _kill_daemons(home: Path) -> None:
    for rec in _daemon_records(home):
        try:
            pid = int(rec["pid"])
        except (KeyError, ValueError, TypeError):
            continue
        if not _pid_alive(pid):
            continue
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    time.sleep(1.0)
    for rec in _daemon_records(home):
        try:
            pid = int(rec["pid"])
        except (KeyError, ValueError, TypeError):
            continue
        if _pid_alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.timeout(180)
def test_failed_databricks_auth_does_not_orphan_a_host_daemon(tmp_path: Path) -> None:
    """A failed Databricks auth on remote connect must not spawn/orphan a daemon.

    Journey (from the bug report): run ``omnigent claude --server <url>``
    against a Databricks-fronted server with no saved credentials. Expected
    (post-fix): auth is sequenced before the daemon starts, so it fails loud
    and *no* daemon is spawned. Actual (bug): auth and the daemon start
    concurrently, the daemon dials the tunnel unauthenticated, and when auth
    raises the daemon is left orphaned.
    """
    edge = _DatabricksEdge(hold_auth_probe=True)
    home = tmp_path / "home"
    home.mkdir()

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _ENV_STRIP_EXACT and not k.startswith(_ENV_STRIP_PREFIXES)
    }
    env["HOME"] = str(home)

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "omnigent", "claude", "--server", edge.url],
            env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
        output = proc.stdout or ""

        # 1. The journey drove the auth failure the report describes. This holds
        #    before and after the fix; it confirms we exercised the right path.
        assert proc.returncode != 0, (
            f"expected the CLI to fail auth (non-zero exit); got 0.\n{output}"
        )
        assert "Not signed in" in output and "Databricks-fronted" in output, (
            f"expected the actionable Databricks 'Not signed in' auth error; got:\n{output}"
        )

        # 2. Bug: a host daemon is orphaned when auth raises after it spawns.
        #    Post-fix (auth sequenced first) no daemon is spawned at all.
        orphaned = _alive_daemon_pids(home)
        assert not orphaned, (
            "a failed Databricks auth left an orphaned host daemon running "
            f"(alive PIDs {orphaned}); auth must be sequenced before the daemon "
            "starts so a failed connect spawns no daemon.\n"
            f"daemon records: {_daemon_records(home)}"
        )

        # 3. Bug: the daemon dials the server unauthenticated before auth
        #    completes. Post-fix, no daemon means no tunnel connect ever.
        deadline = time.monotonic() + _TUNNEL_WAIT_S
        while not edge.tunnel_connected.is_set() and time.monotonic() < deadline:
            time.sleep(0.25)
        tunnels = edge.tunnel_requests()
        assert not tunnels, (
            "the host daemon dialed the server's tunnel route before auth "
            f"completed (requests: {tunnels}); "
            f"unauthenticated={[t for t in tunnels if not t['authorization']]}"
        )
    finally:
        _kill_daemons(home)
        edge.close()
