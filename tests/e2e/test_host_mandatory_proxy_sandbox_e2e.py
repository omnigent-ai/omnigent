"""End-to-end repro: host tunnel can't connect from a proxy-mandatory sandbox.

Journey (from the bug report): run ``omnigent host --server <url>`` inside a
sandbox whose network namespace mandates all egress through a local CONNECT
proxy (``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` are set; there is no
direct DNS or TCP path out). Proxy-honoring clients reach the server fine —
a plain ``GET /health``/``/v1/me`` succeeds — but the host tunnel's WebSocket
ignores the proxy environment entirely (``websockets<15`` has no proxy
support, and ``host/connect.py`` passes no ``proxy`` kwarg), so the daemon
loops forever::

    WARN  host.connect  run  | Host tunnel disconnected: [Errno -3] Temporary
    failure in name resolution. Reconnecting in 3.0s

and the host never registers online.

The test emulates that network locally, without touching real DNS: the server
is addressed by a hostname only the proxy can reach — a ``sitecustomize``
shim makes ``socket.getaddrinfo`` fail for it inside the host process with
``EAI_AGAIN`` ("Temporary failure in name resolution", the exact failure a
resolver-less network namespace produces), while a real CONNECT-capable HTTP
proxy in front of the e2e ``live_server`` resolves the name itself. A
proxy-aware probe must reach the server through that proxy; the host, run
with the same proxy environment, must come online the same way.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_host_mandatory_proxy_sandbox_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import yaml

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests.e2e.conftest import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The server's hostname as the sandboxed host sees it: resolvable only by the
# mandatory proxy. The .invalid TLD guarantees real DNS can never resolve it,
# and the sitecustomize shim below makes the failure mode exact.
_PROXY_ONLY_HOST = "proxy-only.omni-sandbox.invalid"

# How long to wait for the host to register online. The tunnel's DNS failure
# is immediate and the reconnect backoff caps at 3s, so a proxy-honoring
# tunnel has dozens of chances to connect within this window.
_ONLINE_TIMEOUT_S = 45.0

_SITECUSTOMIZE = f'''\
"""Emulate a proxy-mandatory sandbox's resolver for one hostname (test shim).

Direct resolution of the server's hostname fails exactly as in a network
namespace with no direct egress path: EAI_AGAIN, "Temporary failure in name
resolution". Only the mandatory CONNECT proxy (which resolves the name
itself) can reach the server.
"""
import socket

_real_getaddrinfo = socket.getaddrinfo


def _proxy_only_getaddrinfo(host, *args, **kwargs):
    if host == {_PROXY_ONLY_HOST!r}:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _proxy_only_getaddrinfo
'''


class _MandatoryProxy:
    """A sandbox's mandatory egress proxy: the only path to the server.

    Accepts standard HTTP proxy traffic on a loopback port and resolves the
    proxy-only hostname itself, exactly like the CONNECT proxy such sandboxes
    inject:

    - ``CONNECT <host>:<port>`` (WebSocket / TLS clients) is answered with
      ``200 Connection Established`` and piped byte-for-byte to the backend.
    - Absolute-form requests (``GET http://<host>:<port>/path``, what an
      env-honoring HTTP client sends for plain-http origins) are rewritten to
      origin-form and forwarded.

    Only the mapped hostname is reachable; anything else gets 502, like a
    sandbox with restricted egress.
    """

    def __init__(self, hostname: str, backend_host: str, backend_port: int) -> None:
        self._hostname = hostname
        self._backend = (backend_host, backend_port)
        # CONNECT targets served, as (host, port) — lets the test prove the
        # tunnel actually traversed the proxy on the success path.
        self.connect_targets: list[tuple[str, int]] = []
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

    @property
    def url(self) -> str:
        """The proxy URL clients put in HTTP_PROXY/HTTPS_PROXY/ALL_PROXY."""
        return f"http://127.0.0.1:{self.port}"

    def _dial_backend(self, host: str | None) -> socket.socket | None:
        """Open a backend connection if *host* is the proxy-only hostname.

        :param host: Hostname the client asked the proxy to reach.
        :returns: A connected socket, or ``None`` when unreachable/refused.
        """
        if host != self._hostname:
            return None
        try:
            return socket.create_connection(self._backend, timeout=10.0)
        except OSError:
            return None

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(15.0)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    return
                head += chunk
        except OSError:
            return
        finally:
            # The head is read (or the client is gone); piping below must not
            # be cut by a read timeout — the tunnel is long-lived.
            with contextlib.suppress(OSError):
                client.settimeout(None)

        header_blob, _, early_body = head.partition(b"\r\n\r\n")
        lines = header_blob.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
        except ValueError:
            self._refuse(client, b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        if method.upper() == "CONNECT":
            host, _, port_s = target.partition(":")
            backend = self._dial_backend(host)
            if backend is None:
                self._refuse(client, b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            self.connect_targets.append((host, int(port_s or "443")))
            try:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            except OSError:
                backend.close()
                client.close()
                return
            self._pipe_both(client, backend, early_body)
            return

        # Absolute-form request: rewrite to origin-form and forward.
        parsed = urlparse(target)
        backend = self._dial_backend(parsed.hostname) if parsed.scheme else None
        if backend is None:
            self._refuse(client, b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        origin_form = parsed.path or "/"
        if parsed.query:
            origin_form += f"?{parsed.query}"
        out = [f"{method} {origin_form} {version}".encode("latin-1")]
        for raw in lines[1:]:
            lowered = raw.lower()
            if lowered.startswith((b"proxy-connection:", b"connection:")):
                continue
            out.append(raw)
        # One request per connection keeps the raw byte pipe below correct.
        out.append(b"Connection: close")
        try:
            backend.sendall(b"\r\n".join(out) + b"\r\n\r\n" + early_body)
        except OSError:
            backend.close()
            client.close()
            return
        self._pipe_both(client, backend, b"")

    @staticmethod
    def _refuse(client: socket.socket, response: bytes) -> None:
        with contextlib.suppress(OSError):
            client.sendall(response)
        with contextlib.suppress(OSError):
            client.close()

    def _pipe_both(self, client: socket.socket, backend: socket.socket, early_body: bytes) -> None:
        """Pipe bytes both ways until either side closes.

        :param client: The proxy client (host process / probe).
        :param backend: The backend (live server) connection.
        :param early_body: Client bytes already read past the request head.
        """
        if early_body:
            with contextlib.suppress(OSError):
                backend.sendall(early_body)
        t1 = threading.Thread(target=self._pipe, args=(client, backend), daemon=True)
        t2 = threading.Thread(target=self._pipe, args=(backend, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
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
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _spawn_host_in_proxy_mandatory_sandbox(
    *,
    tmp_path: Path,
    server_url: str,
    proxy_url: str,
    mock_llm_server_url: str,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn the real ``omnigent host`` CLI inside the emulated sandbox network.

    The child gets exactly what the sandboxed user's process has: the
    standard proxy env vars pointing at the mandatory proxy (loopback
    excluded via NO_PROXY, as such sandboxes do), and no direct resolution
    for the server's hostname (the sitecustomize shim).

    :param tmp_path: Per-test temp dir used as the host's ``HOME``.
    :param server_url: Server URL as the sandbox sees it (proxy-only host).
    :param proxy_url: The mandatory proxy's URL.
    :param mock_llm_server_url: Mock LLM server base URL (for any runners).
    :returns: ``(proc, host_id, host_log)``.
    """
    inject_dir = tmp_path / "inject"
    inject_dir.mkdir()
    (inject_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE)

    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-proxy-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )

    host_log = tmp_path / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_CONFIG_HOME": str(omni_dir),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        PROCESS_LOG_FILE_ENV_VAR: str(host_log),
        # The sandbox's mandatory egress proxy: every client honoring the
        # standard env vars can reach out; nothing else has a network path.
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
        # The shim must load in the child, and the child must import this
        # worktree's omnigent plus the SDK packages the CLI pulls in.
        "PYTHONPATH": os.pathsep.join(
            [
                str(inject_dir),
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ).rstrip(os.pathsep),
    }
    # A leaked managed-host identity would override the config.yaml identity
    # the test polls for.
    for var in (HOST_TOKEN_ENV_VAR, HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR):
        env.pop(var, None)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "host",
            "--server",
            server_url,
            "--non-interactive",
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, host_id, host_log


def _host_online(client: httpx.Client, host_id: str) -> bool:
    """Whether *host_id* is currently registered online with the server.

    :param client: HTTP client pointed directly at the live server.
    :param host_id: Host ID to look for.
    :returns: True when the host shows ``status == "online"``.
    """
    resp = client.get("/v1/hosts")
    if resp.status_code != 200:
        return False
    return any(
        host["host_id"] == host_id and host["status"] == "online"
        for host in resp.json().get("hosts", [])
    )


@pytest.mark.timeout(240)
def test_host_comes_online_through_mandatory_proxy(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A host whose only network path is a CONNECT proxy must come online.

    Journey: inside a proxy-mandatory sandbox (all egress through a local
    CONNECT proxy; the server's hostname has no direct resolution), run
    ``omnigent host --server <url>``. Expected: the tunnel honors the proxy
    environment like every other client in that sandbox and the host
    registers online. Actual (bug): the tunnel WebSocket dials direct,
    fails name resolution, and loops "Host tunnel disconnected: [Errno -3]
    Temporary failure in name resolution. Reconnecting in 3.0s" forever —
    the host never comes online.
    """
    parsed = urlparse(live_server)
    assert parsed.hostname is not None and parsed.port is not None
    proxy = _MandatoryProxy(_PROXY_ONLY_HOST, parsed.hostname, parsed.port)
    proc: subprocess.Popen[bytes] | None = None
    try:
        server_via_proxy = f"http://{_PROXY_ONLY_HOST}:{parsed.port}"

        # The sandbox's defining contrast: a proxy-honoring HTTP client
        # reaches the server through the mandatory proxy just fine (the
        # host's own REST calls do too) — only the tunnel is at issue.
        probe = httpx.get(f"{server_via_proxy}/health", proxy=proxy.url, timeout=10.0)
        assert probe.status_code == 200, (
            f"rig self-check failed: GET /health via the mandatory proxy "
            f"returned {probe.status_code} — the emulated sandbox network is "
            f"broken, not the product"
        )

        proc, host_id, host_log = _spawn_host_in_proxy_mandatory_sandbox(
            tmp_path=tmp_path,
            server_url=server_via_proxy,
            proxy_url=proxy.url,
            mock_llm_server_url=mock_llm_server_url,
        )

        deadline = time.monotonic() + _ONLINE_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if _host_online(http_client, host_id):
                online = True
                break
            time.sleep(POLL_INTERVAL_S)

        log_text = host_log.read_text(errors="replace") if host_log.exists() else ""
        assert proc.poll() is None, (
            f"host process exited (code {proc.returncode}) instead of serving or "
            f"retrying; log tail:\n{log_text[-2000:]}"
        )
        if not online:
            reconnects = log_text.count("Host tunnel disconnected:")
            dns_failures = log_text.count("Temporary failure in name resolution")
            raise AssertionError(
                f"Host never registered online within {_ONLINE_TIMEOUT_S:.0f}s. "
                f"REST through the mandatory proxy works (GET /health -> 200), but "
                f"the tunnel WebSocket ignores HTTP_PROXY/HTTPS_PROXY/ALL_PROXY and "
                f"dials the server directly: {dns_failures} direct name-resolution "
                f"failures across {reconnects} 'Host tunnel disconnected' reconnect "
                f"attempts. Host log tail:\n{log_text[-2000:]}"
            )

        # Online — and it must have gotten there through the proxy (the only
        # real path), not via some direct loophole in the emulation.
        assert any(host == _PROXY_ONLY_HOST for host, _ in proxy.connect_targets), (
            "host registered online without a CONNECT through the mandatory "
            "proxy — the emulated sandbox leaked a direct path, so this pass "
            "does not prove proxy support"
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        proxy.close()
