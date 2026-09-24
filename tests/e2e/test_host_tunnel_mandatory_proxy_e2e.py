"""End-to-end repro: host tunnel ignores a mandatory egress proxy.

In a sandbox whose network namespace forces every byte of egress through
an injected HTTP CONNECT proxy (``HTTP_PROXY``/``HTTPS_PROXY``/
``ALL_PROXY`` set; no direct DNS or TCP path out — e.g. an NVIDIA
OpenShell sandbox), ``omnigent host --server <url>`` never comes online.
The host's plain REST calls (capability discovery, ``GET /v1/me``)
succeed because httpx honors the proxy environment for non-loopback
servers, but the WebSocket tunnel is opened by ``websockets<15`` — which
has no proxy support at all — so every attempt resolves the server
hostname directly, fails, and the daemon loops::

    WARN  host.connect  run  | Host tunnel disconnected: [Errno -3]
    Temporary failure in name resolution. Reconnecting in 3.0s

while the web UI reports "The runner didn't come online in time".

This test recreates that sandbox around a live local server: a CONNECT
proxy is the only thing able to resolve/reach the server's hostname,
direct resolution of that hostname fails exactly like a namespace with
no DNS route out, and the real ``omnigent host`` process runs with the
sandbox's mandatory-proxy environment. The host must come online — which
is only possible when the tunnel honors the proxy. On an unfixed build
the host never registers and the test fails quoting the name-resolution
loop from the host's own log.

Run with::

    python -m pytest tests/e2e/test_host_tunnel_mandatory_proxy_e2e.py -v --timeout=300
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The server as addressed from inside the sandbox: resolvable/reachable
# only through the sandbox's proxy, never via the local resolver.
SANDBOX_SERVER_HOSTNAME = "omnigent-server.openshell.invalid"

_LOOPBACK_NO_PROXY = "localhost,127.0.0.1,::1"

_HOST_ONLINE_DEADLINE_S = 90.0
_POLL_INTERVAL_S = 0.5

# Enough tunnel disconnects to prove a stuck direct-DNS loop rather than
# a transient blip the reconnect loop could ride out.
_LOOP_FAILURE_THRESHOLD = 5

_NAME_RESOLUTION_LOOP_RE = re.compile(
    r"Host tunnel disconnected: .*[Tt]emporary failure in name resolution"
)

_SITECUSTOMIZE = f'''\
"""Emulate a sandbox with no direct DNS path out (test shim).

Only the mandatory proxy can resolve the server hostname; the host
process's own resolver fails like a network namespace whose only
route — including to any DNS server — is the proxy.
"""
import socket

_real_getaddrinfo = socket.getaddrinfo


def _no_direct_path_getaddrinfo(host, *args, **kwargs):
    if host == {SANDBOX_SERVER_HOSTNAME!r}:
        raise socket.gaierror(
            socket.EAI_AGAIN, "Temporary failure in name resolution"
        )
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _no_direct_path_getaddrinfo
'''


class MandatoryEgressProxy:
    """Minimal HTTP forward proxy: CONNECT tunnels + absolute-form requests.

    Plays the sandbox's injected egress proxy: it alone can resolve the
    mapped hostnames, and anything unmapped is refused — there is no
    other path out.
    """

    def __init__(self, upstreams: dict[str, tuple[str, int]]) -> None:
        """
        :param upstreams: Hostname → ``(address, port)`` the proxy alone
            can reach, e.g. ``{"server.sandbox.invalid": ("127.0.0.1", 18501)}``.
        """
        self._upstreams = upstreams
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.tunnel_targets: list[str] = []
        self.proxied_requests: list[str] = []
        self.refused_targets: list[str] = []
        self._closed = False
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="egress-proxy-accept", daemon=True
        )

    def start(self) -> MandatoryEgressProxy:
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._closed = True
        with contextlib.suppress(OSError):
            self._listener.close()

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve, args=(conn,), name="egress-proxy-conn", daemon=True
            ).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                head += chunk
                if len(head) > (1 << 20):
                    return
            raw_head, _, buffered = head.partition(b"\r\n\r\n")
            lines = raw_head.decode("latin-1").split("\r\n")
            method, _, rest = lines[0].partition(" ")
            target = rest.rsplit(" ", 1)[0]

            if method.upper() == "CONNECT":
                hostname = target.rpartition(":")[0] or target
                upstream_addr = self._upstreams.get(hostname)
                if upstream_addr is None:
                    self.refused_targets.append(target)
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                self.tunnel_targets.append(target)
                upstream = socket.create_connection(upstream_addr)
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if buffered:
                    upstream.sendall(buffered)
                self._pump_pair(conn, upstream)
                return

            parts = urlsplit(target)
            upstream_addr = self._upstreams.get(parts.hostname or "")
            if upstream_addr is None:
                self.refused_targets.append(target)
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            self.proxied_requests.append(f"{method} {target}")
            origin_form = parts.path or "/"
            if parts.query:
                origin_form += f"?{parts.query}"
            forwarded = [f"{method} {origin_form} HTTP/1.1"]
            for line in lines[1:]:
                name = line.split(":", 1)[0].strip().lower()
                if name in ("connection", "proxy-connection", "proxy-authorization", "keep-alive"):
                    continue
                forwarded.append(line)
            forwarded.append("Connection: close")
            upstream = socket.create_connection(upstream_addr)
            upstream.sendall("\r\n".join(forwarded).encode("latin-1") + b"\r\n\r\n" + buffered)
            self._pump_pair(conn, upstream)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _pump_pair(self, client: socket.socket, upstream: socket.socket) -> None:
        """Relay bytes both ways until both directions close."""
        forward = threading.Thread(
            target=self._pump, args=(client, upstream), name="egress-proxy-pump", daemon=True
        )
        forward.start()
        self._pump(upstream, client)
        forward.join(timeout=5.0)
        for sock in (client, upstream):
            with contextlib.suppress(OSError):
                sock.close()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def _name_resolution_disconnects(log_path: Path) -> list[str]:
    """Tunnel-disconnect lines showing direct DNS of the server hostname.

    :param log_path: The host daemon's process log file.
    :returns: Matching log lines, oldest first.
    """
    if not log_path.exists():
        return []
    text = log_path.read_text(errors="replace")
    return [line for line in text.splitlines() if _NAME_RESOLUTION_LOOP_RE.search(line)]


def _tail(path: Path, lines: int = 15) -> str:
    if not path.exists():
        return "<missing>"
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def test_host_comes_online_through_mandatory_proxy(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A host whose only egress is the sandbox proxy must still come online."""
    server_port = urlsplit(live_server).port
    assert server_port is not None, f"live_server has no explicit port: {live_server!r}"
    proxy = MandatoryEgressProxy({SANDBOX_SERVER_HOSTNAME: ("127.0.0.1", server_port)}).start()
    sandbox_server_url = f"http://{SANDBOX_SERVER_HOSTNAME}:{server_port}"

    home = tmp_path / "home"
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True)
    # Unique (host_id, name): the host store enforces one row per
    # (owner, name), and the shared machine hostname would collide.
    host_id = uuid.uuid4().hex
    host_name = f"openshell-sandbox-{uuid.uuid4().hex[:8]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )

    inject_dir = tmp_path / "inject"
    inject_dir.mkdir()
    (inject_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE)

    host_log = tmp_path / "host-daemon.log"
    console_log = tmp_path / "host-console.log"

    try:
        # Preflight: the proxy is a working path to the server — exactly
        # what the sandbox guarantees. Keeps a broken harness from
        # masquerading as the bug.
        health = httpx.get(f"{sandbox_server_url}/health", proxy=proxy.url, timeout=10.0)
        assert health.status_code == 200, (
            f"harness broken: /health through the proxy returned {health.status_code}"
        )

        env = {
            **os.environ,
            "HOME": str(home),
            PROCESS_LOG_FILE_ENV_VAR: str(host_log),
            # The shim must load in the host process, and the process must
            # import this worktree's omnigent.
            "PYTHONPATH": os.pathsep.join(
                [str(inject_dir), str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
            # The sandbox's mandatory egress environment: proxy-aware
            # clients must use it; loopback is the only direct route.
            "HTTP_PROXY": proxy.url,
            "HTTPS_PROXY": proxy.url,
            "ALL_PROXY": proxy.url,
            "http_proxy": proxy.url,
            "https_proxy": proxy.url,
            "all_proxy": proxy.url,
            "NO_PROXY": _LOOPBACK_NO_PROXY,
            "no_proxy": _LOOPBACK_NO_PROXY,
        }

        with open(console_log, "w") as console_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnigent",
                    "host",
                    "--server",
                    sandbox_server_url,
                    "--non-interactive",
                    "--no-open",
                ],
                env=env,
                cwd=str(_REPO_ROOT),
                stdout=console_fh,
                stderr=subprocess.STDOUT,
            )
        try:
            online = False
            deadline = time.monotonic() + _HOST_ONLINE_DEADLINE_S
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    resp = http_client.get("/v1/hosts")
                    if resp.status_code == 200 and any(
                        h["host_id"] == host_id and h["status"] == "online"
                        for h in resp.json().get("hosts", [])
                    ):
                        online = True
                        break
                except httpx.HTTPError:
                    pass
                # Fail fast once the tunnel is provably stuck in the
                # direct-DNS loop; a fixed tunnel never emits these lines.
                if len(_name_resolution_disconnects(host_log)) >= _LOOP_FAILURE_THRESHOLD:
                    break
                time.sleep(_POLL_INTERVAL_S)

            if not online:
                loop_lines = _name_resolution_disconnects(host_log)
                raise AssertionError(
                    f"Host {host_name!r} never came online through the mandatory "
                    f"egress proxy (exit code: {proc.poll()}). REST discovery DID "
                    f"honor the proxy env ({len(proxy.proxied_requests)} proxied "
                    f"request(s), e.g. {proxy.proxied_requests[:3]}), but the "
                    f"WebSocket tunnel made {len(proxy.tunnel_targets)} CONNECT "
                    f"attempt(s) through it. Host log shows the tunnel resolving "
                    f"the server hostname directly instead of using the proxy:\n"
                    + "\n".join(loop_lines[-5:] or ["<no matching disconnect lines>"])
                    + f"\n--- console tail ---\n{_tail(console_log)}"
                )

            # The only route to the server is the proxy, so an online host
            # implies the tunnel used it; assert explicitly so a future
            # regression cannot pass via some accidental direct path.
            assert any(
                target.rpartition(":")[0] == SANDBOX_SERVER_HOSTNAME
                for target in proxy.tunnel_targets
            ), (
                f"host came online but the tunnel never issued a CONNECT for "
                f"{SANDBOX_SERVER_HOSTNAME!r} through the mandatory proxy "
                f"(tunnels seen: {proxy.tunnel_targets!r})"
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    finally:
        proxy.stop()
