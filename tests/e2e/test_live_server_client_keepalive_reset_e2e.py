"""The e2e suite's pooled HTTP clients must survive server-closed keep-alive.

The nightly e2e run intermittently went red with::

    httpx.ReadError: [Errno 104] Connection reset by peer

dying mid-journey (the cold-resume test's session-bind ``PATCH``), with frame
locals showing a reused connection in state ``CLOSED``. The mechanism:

* E2e tests drive a spawned ``omnigent server`` through **pooled**
  ``httpx.Client`` instances that keep connections alive and reuse them.
* The server sets no ``timeout_keep_alive`` (``omnigent/cli.py``), so
  uvicorn's **5 s default** applies: an idle keep-alive connection is closed
  server-side after 5 s.
* Under sharded parallel load the single-threaded server is CPU-starved; the
  idle gaps between a test's requests (LLM turns, multi-MB HTTP-silent
  seeding) stretch past 5 s, so the server closes the pooled connection in
  the exact window the client reuses it.
* ``httpx`` performs **no retry** on a reset connection, so the race surfaces
  as an unretried ``ReadError`` and the test hard-fails -- intermittently,
  only under load.

The guarded fix is :func:`tests.e2e.helpers.live_server_client`: the shared
constructor for clients that drive a spawned e2e server, with keep-alive
reuse disabled (``max_keepalive_connections=0``) so a reused-dead-socket
write can never happen -- every request opens a fresh loopback connection.

These tests make the load-dependent race **deterministic**. A tiny in-process
TCP proxy relays the first request/response on a connection normally, then --
when the client reuses that pooled keep-alive connection -- RSTs it instead
of forwarding, exactly mirroring the CPU-starved server closing the idle
keep-alive socket out from under the pool:

* :func:`test_pooled_default_client_dies_on_keepalive_reset` documents the
  hazard (and proves the poison harness works): a default pooled client
  fails the reused request. If this ever starts passing, httpx has grown
  reset-resilient reuse and ``live_server_client`` can be simplified.
* :func:`test_live_server_client_survives_keepalive_reset` asserts the
  shared constructor survives the same poison against a lightweight
  upstream (fast, no server spawn).
* :func:`test_live_server_client_survives_keepalive_reset_against_real_server`
  drives the same journey against a real spawned ``omnigent server``.

Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_live_server_client_keepalive_reset_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests.e2e.helpers import live_server_client

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The server subprocess imports ``omnigent_client`` / ``omnigent_ui_sdk``; in a
# worktree they resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        # CI shells often carry an egress proxy; localhost must bypass it.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    probe = httpx.Client(trust_env=False)
    try:
        while time.monotonic() < deadline:
            try:
                if probe.get(url, timeout=2.0).status_code == 200:
                    return
                last = "non-200"
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(_POLL_S)
    finally:
        probe.close()
    raise AssertionError(f"{url} never became healthy: {last}")


def _rst_close(sock: socket.socket) -> None:
    """Close *sock* with a TCP RST (SO_LINGER 0) so a peer read/write errors."""
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(OSError):
        sock.close()


class _PoisonProxy:
    """A localhost TCP proxy that resets a keep-alive connection on reuse.

    For each accepted client connection it opens one upstream connection to the
    real server and relays bytes both ways. The FIRST request/response flows
    normally; once a response has been returned, any further client bytes (a
    reused keep-alive request) trigger a TCP RST of both sockets instead of
    being forwarded -- deterministically reproducing a CPU-starved server that
    closed the idle keep-alive socket in the window the client reuses it.
    """

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._uh = upstream_host
        self._up = upstream_port
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(50)
        self.port = int(self._srv.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._srv.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection((self._uh, self._up), timeout=30)
        except OSError:
            _rst_close(client)
            return
        response_seen = threading.Event()

        def _pump_upstream_to_client() -> None:
            try:
                while True:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    client.sendall(data)
                    response_seen.set()
            except OSError:
                pass

        pump = threading.Thread(target=_pump_upstream_to_client, daemon=True)
        pump.start()
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                if response_seen.is_set():
                    # Reuse after a response was already returned -> POISON:
                    # RST both sockets, mimicking the server having closed the
                    # pooled keep-alive connection under load.
                    _rst_close(client)
                    _rst_close(upstream)
                    return
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                upstream.close()


def _drive_reuse(client: httpx.Client, base_url: str) -> httpx.Response | Exception:
    """Two GETs on *client*: open+pool a connection, then reuse the poisoned one.

    :returns: The second response on success, or the raised exception (so the
        caller can assert precisely on the reset).
    """
    client.get(f"{base_url}/health", timeout=10.0)  # opens + pools the conn
    try:
        return client.get(f"{base_url}/health", timeout=10.0)  # reuses it
    except httpx.HTTPError as exc:
        return exc


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal keep-alive HTTP/1.1 upstream answering 200 to ``/health``."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence per-request stderr noise."""


@pytest.fixture
def poisoned_upstream() -> Iterator[str]:
    """A lightweight HTTP upstream behind the poison proxy.

    :yields: The proxy's base URL; the first request on each connection is
        relayed, a reused keep-alive request is RST instead.
    """
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    server_thread.start()
    proxy = _PoisonProxy("127.0.0.1", upstream.server_address[1])
    proxy.start()
    try:
        yield f"http://127.0.0.1:{proxy.port}"
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def test_pooled_default_client_dies_on_keepalive_reset(poisoned_upstream: str) -> None:
    """A default pooled client reusing a server-closed connection fails.

    This is the hazard ``live_server_client`` exists to remove, and it doubles
    as the poison-harness sanity check: the RST must surface on reuse. If this
    test ever starts FAILING (the reused request succeeds), httpx has become
    resilient to reset-on-reuse and the no-keep-alive workaround in
    ``live_server_client`` can be revisited.
    """
    client = httpx.Client(trust_env=False)
    try:
        result = _drive_reuse(client, poisoned_upstream)
    finally:
        client.close()
    assert isinstance(result, httpx.HTTPError), (
        "expected the reused keep-alive request on a default pooled client to "
        f"fail with a connection reset, got: {result!r}"
    )


def test_live_server_client_survives_keepalive_reset(poisoned_upstream: str) -> None:
    """The shared e2e client constructor survives a server-closed connection.

    With keep-alive reuse disabled, the second request opens a fresh
    connection instead of reusing the poisoned one, so the journey continues.
    """
    client = live_server_client()
    try:
        result = _drive_reuse(client, poisoned_upstream)
    finally:
        client.close()
    if isinstance(result, Exception):
        pytest.fail(
            "live_server_client() reused a keep-alive connection the server "
            f"closed and raised {type(result).__name__}: {result} -- the exact "
            "signature that turned the nightly e2e suite red. Keep keep-alive "
            "reuse disabled (or make the client retry on a reset) so a "
            "server-closed connection cannot fail a request."
        )
    assert result.status_code == 200, f"reused request returned {result.status_code}, expected 200"


def test_live_server_client_survives_keepalive_reset_against_real_server(
    tmp_path: Path,
) -> None:
    """End to end: the same journey against a real spawned ``omnigent server``.

    A real server runs behind the poison proxy; a client drives two requests,
    the second reusing the pooled connection the server "closed". Expected:
    the reused request succeeds (a resilient client opens a fresh connection).
    Buggy behavior: a pooled keep-alive client reuses the dead socket and the
    write RSTs, surfacing as ``httpx.ReadError: [Errno 104] Connection reset
    by peer`` with no retry.

    :param tmp_path: Per-test temp dir (server DB / artifacts).
    """
    port = _find_free_port()
    base_direct = f"http://127.0.0.1:{port}"
    server_log = (tmp_path / "server.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    proxy: _PoisonProxy | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{tmp_path / 'chat.db'}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_direct}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        proxy = _PoisonProxy("127.0.0.1", port)
        proxy.start()
        base_url = f"http://127.0.0.1:{proxy.port}"

        # Positive control: a keep-alive-disabled client built inline (not via
        # the constructor under test) survives the poison proxy, so a failure
        # below is unambiguously the client construction -- not a broken proxy
        # or an unhealthy server.
        resilient = httpx.Client(
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
        )
        try:
            control = _drive_reuse(resilient, base_url)
        finally:
            resilient.close()
        assert isinstance(control, httpx.Response) and control.status_code == 200, (
            "harness sanity failed: a keep-alive-disabled client should survive "
            f"the poison proxy but got: {control!r}"
        )

        # Regression assertion: the suite's shared client constructor must
        # survive the reset too.
        client = live_server_client()
        try:
            result = _drive_reuse(client, base_url)
        finally:
            client.close()

        if isinstance(result, Exception):
            pytest.fail(
                "reusing a pooled keep-alive connection the server closed "
                f"raised {type(result).__name__}: {result} -- the exact nightly "
                "failure signature (httpx.ReadError [Errno 104] Connection "
                "reset by peer). live_server_client() must not hand tests a "
                "client that reuses server-closed connections."
            )
        assert result.status_code == 200, (
            f"reused request returned {result.status_code}, expected 200"
        )
    finally:
        if proxy is not None:
            proxy.stop()
        _terminate(server_proc)
        server_log.close()
