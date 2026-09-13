"""E2E regression test for a host daemon riding out a server restart.

Reproduces the reported failure: ``omnigent host`` connects to a server
that sits behind a reverse proxy (Traefik-style). When the server
container restarts, the proxy briefly answers HTTP 404 for the tunnel
route. The host daemon's reconnect lands inside that window, and the
4xx classification treats the 404 as permanent (`"retrying will not
help"`), so the daemon exits — killing every live runner session on
the host as collateral.

The test stands up a real TCP proxy in front of the e2e ``live_server``,
connects a real host daemon THROUGH the proxy, flips the proxy into a
404-serving "backend restarting" mode for a few seconds (severing the
live tunnel so the daemon reconnects into the 404), brings it back up,
and asserts the daemon survived the window and the host is online again.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_host_transient_404_restart.py -v
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import yaml

from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S

# How long the proxy serves 404 while the "server container restarts".
# Comfortably longer than the daemon's reconnect backoff base (0.5s) so a
# reconnect attempt is guaranteed to land inside the outage window.
_RESTART_WINDOW_S = 6.0

_404_RESPONSE = (
    b"HTTP/1.1 404 Not Found\r\n"
    b"content-type: text/plain\r\n"
    b"content-length: 9\r\n"
    b"connection: close\r\n"
    b"\r\n"
    b"not found"
)


class _RestartableProxy:
    """A Traefik stand-in: forwards TCP to a backend, or serves HTTP 404.

    While ``up``, every accepted connection is piped byte-for-byte to the
    backend (HTTP and WebSocket alike). Flipping to ``down`` makes new
    connections receive a bare HTTP 404 (the proxy has no live route while
    the backend container restarts) and severs all live piped connections
    (the backend process died mid-tunnel).
    """

    def __init__(self, backend_host: str, backend_port: int) -> None:
        self._backend = (backend_host, backend_port)
        self._down = threading.Event()
        self._live_socks: set[socket.socket] = set()
        self._lock = threading.Lock()
        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handle(self, client: socket.socket) -> None:
        if self._down.is_set():
            # Backend is restarting: the proxy has no route → 404.
            try:
                client.settimeout(5.0)
                client.recv(4096)
                client.sendall(_404_RESPONSE)
            except OSError:
                # The daemon may drop the connection mid-handshake while the
                # simulated restart window is active; that's expected.
                pass
            finally:
                client.close()
            return
        try:
            backend = socket.create_connection(self._backend, timeout=10.0)
        except OSError:
            client.close()
            return
        with self._lock:
            self._live_socks.update((client, backend))
        try:
            t1 = threading.Thread(target=self._pipe, args=(client, backend), daemon=True)
            t2 = threading.Thread(target=self._pipe, args=(backend, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            with self._lock:
                self._live_socks.discard(client)
                self._live_socks.discard(backend)
            for sock in (client, backend):
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            # Either peer closing during the simulated restart tears the pipe
            # down; the finally block below closes both ends.
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    def go_down(self) -> None:
        """Backend container dies: sever live tunnels, serve 404 to new ones."""
        self._down.set()
        with self._lock:
            socks = list(self._live_socks)
        for sock in socks:
            # shutdown() interrupts blocked recv()s in the pipe threads
            # immediately (close() alone can leave them blocked), so the
            # daemon and the server both see the tunnel die right away.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def go_up(self) -> None:
        """Backend container is back: resume forwarding."""
        self._down.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _spawn_host_daemon_via(
    *,
    tmp_path: Path,
    server_url: str,
    mock_llm_server_url: str,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn an isolated host daemon pointed at *server_url*.

    Mirrors ``tests/e2e/test_host_e2e.py``'s spawner, but takes the URL the
    daemon should dial (here: the restartable proxy) instead of assuming the
    direct live-server URL.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param server_url: URL the daemon registers with — the PROXY address.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: ``(proc, host_id, daemon_log)``.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", server_url],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _wait_for_host_online(
    client: httpx.Client,
    host_id: str,
    timeout: float = 30.0,
) -> None:
    """Poll GET /v1/hosts until *host_id* appears online.

    :param client: HTTP client pointed at the (direct) server.
    :param host_id: Host ID to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            # The proxy/server may still be coming up; keep polling until the
            # deadline expires.
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


@pytest.mark.timeout(180)
def test_host_survives_transient_404_during_server_restart(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A brief proxy-served 404 while the server restarts must not kill the host.

    Journey (from the bug report): run an external host against a server
    behind a reverse proxy → restart the server container → the proxy
    briefly returns 404 for the tunnel route → the host daemon's reconnect
    lands in that window. Expected: the daemon retries with backoff and
    reconnects when the server is back. Actual (bug): it classifies the 404
    as "permanent — retrying will not help" and exits, killing every live
    runner session on the host.
    """
    parsed = urlparse(live_server)
    assert parsed.hostname is not None and parsed.port is not None
    proxy = _RestartableProxy(parsed.hostname, parsed.port)
    proc: subprocess.Popen[bytes] | None = None
    try:
        proxy_url = f"http://127.0.0.1:{proxy.port}"

        # Sanity: the server is reachable through the proxy, like a user URL.
        assert httpx.get(f"{proxy_url}/health", timeout=10.0).status_code == 200

        proc, host_id, daemon_log = _spawn_host_daemon_via(
            tmp_path=tmp_path,
            server_url=proxy_url,
            mock_llm_server_url=mock_llm_server_url,
        )
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        # "Restart the server container": the proxy serves 404 for the
        # tunnel route and severs the live tunnel, so the daemon
        # reconnects into the 404 window.
        proxy.go_down()
        time.sleep(_RESTART_WINDOW_S)
        proxy.go_up()

        # The daemon must ride out the window: give it time to notice the
        # restored backend and re-register, checking it never exits.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            rc = proc.poll()
            assert rc is None, (
                f"Host daemon exited with code {rc} during a transient "
                f"{_RESTART_WINDOW_S:.0f}s 404 window (a normal server "
                "restart behind a reverse proxy). "
                + (
                    "HOST_FATAL_EXIT_CODE — classified permanent. "
                    if rc == HOST_FATAL_EXIT_CODE
                    else ""
                )
                + f"Daemon log tail:\n{daemon_log.read_text()[-2000:]}"
            )
            resp = http_client.get("/v1/hosts")
            if resp.status_code == 200 and any(
                h["host_id"] == host_id and h["status"] == "online"
                for h in resp.json().get("hosts", [])
            ):
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            raise AssertionError(
                "Host daemon did not come back online within 30s of the "
                f"server returning. Daemon log tail:\n{daemon_log.read_text()[-2000:]}"
            )

        # And it must not have died at any point (belt-and-braces: the poll
        # above can exit on the first iteration if re-registration was fast).
        assert proc.poll() is None, (
            f"Host daemon exited (code {proc.poll()}) after the restart window. "
            f"Daemon log tail:\n{daemon_log.read_text()[-2000:]}"
        )

        # Prove the daemon actually reconnected INTO the 404 window (rather
        # than the poll above observing pre-restart state): its log must show
        # the ridden-out 404.
        assert "HTTP 404" in daemon_log.read_text(), (
            "Daemon log never recorded the 404 window - the test did not "
            f"exercise the ride-out path. Log tail:\n{daemon_log.read_text()[-2000:]}"
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        proxy.close()
