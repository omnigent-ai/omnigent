"""Exercise failed Databricks auth through the real CLI process boundary."""

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

# The auth detector accepts any HTTPS host with an OIDC redirect path.
_MOCK_WORKSPACE = "https://mock-workspace.cloud.databricks.com"

# Isolate credentials, state, and ambient runner wiring from the child process.
_ENV_STRIP_PREFIXES = ("DATABRICKS_", "OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_ENV_STRIP_EXACT = frozenset(
    {
        "OMNIGENT_DATA_DIR",
        "RUNNER_SERVER_URL",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
    }
)

# Allow a leaked daemon enough time to attempt its first tunnel connection.
_TUNNEL_WAIT_S = 30.0


class _DatabricksEdge:
    """Record requests and return the Databricks Apps OIDC redirect."""

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
                    # Hold the probe long enough to expose a concurrent tunnel dial.
                    edge.tunnel_connected.wait(timeout=8.0)
                    edge._auth_probe_answered.set()
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
    """Fail before daemon startup when the remote login is unavailable."""
    edge = _DatabricksEdge(hold_auth_probe=True)
    home = tmp_path / "home"
    home.mkdir()
    credential_stub = tmp_path / "credential-stub"
    credential_stub.mkdir()
    (credential_stub / "sitecustomize.py").write_text(
        "from omnigent import cli\ncli._databricks_workspace_auth_info = lambda _host: None\n"
    )

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _ENV_STRIP_EXACT and not k.startswith(_ENV_STRIP_PREFIXES)
    }
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(credential_stub)

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

        # Confirm the subprocess reached Databricks authentication.
        assert proc.returncode != 0, (
            f"expected the CLI to fail auth (non-zero exit); got 0.\n{output}"
        )
        assert "Not signed in" in output and "Databricks-fronted" in output, (
            f"expected the actionable Databricks 'Not signed in' auth error; got:\n{output}"
        )

        orphaned = _alive_daemon_pids(home)
        assert not orphaned, (
            "a failed Databricks auth left an orphaned host daemon running "
            f"(alive PIDs {orphaned}); auth must be sequenced before the daemon "
            "starts so a failed connect spawns no daemon.\n"
            f"daemon records: {_daemon_records(home)}"
        )

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
