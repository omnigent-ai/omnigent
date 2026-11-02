"""A real host and its runner connect when only their proxy can resolve the server."""

from __future__ import annotations

import contextlib
import os
import select
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests.e2e.conftest import lookup_agent_id, upload_agent
from tests.e2e.test_host_e2e import _write_smoke_agent_yaml

_SERVER_HOST = "omnigent-server.openshell.invalid"

_SITECUSTOMIZE = f"""\
import socket

_real_getaddrinfo = socket.getaddrinfo

def _getaddrinfo(host, *args, **kwargs):
    if host == {_SERVER_HOST!r}:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
    return _real_getaddrinfo(host, *args, **kwargs)

socket.getaddrinfo = _getaddrinfo
"""


@contextlib.contextmanager
def _mandatory_proxy(server_port: int) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """Forward HTTP and CONNECT only for the otherwise unresolvable server."""
    # Appended from handler threads and read by the test thread; CPython's
    # list.append is atomic, so no lock is needed here.
    relayed: list[tuple[str, str]] = []
    stopped = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            client = self.request
            client.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk or len(head) + len(chunk) > 65536:
                    return
                head += chunk
            raw_head, _, buffered = head.partition(b"\r\n\r\n")
            lines = raw_head.decode("latin-1").split("\r\n")
            request_line = lines[0].split(" ", 2)
            if len(request_line) != 3:
                return
            method, target, version = request_line
            try:
                parts = urlsplit(f"//{target}" if method == "CONNECT" else target)
                hostname, port = parts.hostname, parts.port
            except ValueError:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            if hostname != _SERVER_HOST or port != server_port:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            with socket.create_connection(("127.0.0.1", server_port), timeout=5) as upstream:
                relayed.append((method, target))
                if method == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    path = parts.path or "/"
                    if parts.query:
                        path += f"?{parts.query}"
                    headers = [
                        line
                        for line in lines[1:]
                        if line.split(":", 1)[0].lower()
                        not in {
                            "connection",
                            "proxy-connection",
                            "proxy-authorization",
                            "keep-alive",
                        }
                    ]
                    forwarded = [f"{method} {path} {version}", *headers, "Connection: close"]
                    upstream.sendall("\r\n".join(forwarded).encode("latin-1") + b"\r\n\r\n")
                upstream.sendall(buffered)
                # Either peer dropping the connection simply ends this relay;
                # setup failures above still surface through socketserver.
                with contextlib.suppress(OSError):
                    while not stopped.is_set():
                        for source in select.select([client, upstream], [], [], 0.2)[0]:
                            data = source.recv(65536)
                            if not data:
                                return
                            (upstream if source is client else client).sendall(data)

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", relayed
        finally:
            stopped.set()
            server.shutdown()
            thread.join(timeout=5)


# Older host builds dial the tunnel directly instead of through the proxy.
@pytest.mark.min_runner_version("0.21.0")
def test_host_comes_online_through_mandatory_proxy(
    live_server: str, http_client: httpx.Client, tmp_path: Path
) -> None:
    server_port = urlsplit(live_server).port
    assert server_port is not None
    server_url = f"http://{_SERVER_HOST}:{server_port}"
    # Only the proxy may resolve the server; the real host imports this DNS shim.
    dns_shim = tmp_path / "dns-shim"
    dns_shim.mkdir()
    (dns_shim / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    python = runner_executable()
    # Exclude ambient credentials and optional native harness binaries.
    base_env = {
        "PATH": os.pathsep.join([str(Path(python).parent), os.defpath]),
        "PYTHONPATH": str(dns_shim),
        **{
            name: os.environ[name]
            for name in ("OMNIGENT_HARNESS_TMP_PARENT", "LANG", "LC_ALL", "TMPDIR")
            if name in os.environ
        },
    }

    with _mandatory_proxy(server_port) as (proxy_url, relayed):
        assert httpx.get(f"{server_url}/health", proxy=proxy_url, timeout=10).status_code == 200
        env = {
            "OMNIGENT_DISABLE_KEYRING": "1",
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "ALL_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "all_proxy": proxy_url,
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
            # Proxy credentials cross the runner boundary only by explicit opt-in.
            "OMNIGENT_RUNNER_ENV_PASSTHROUGH": (
                "HTTP_PROXY,HTTPS_PROXY,ALL_PROXY,NO_PROXY,"
                "http_proxy,https_proxy,all_proxy,no_proxy"
            ),
        }
        with isolated_host(
            tmp_path / "host",
            server_url,
            env=env,
            base_env=base_env,
            command=[
                python,
                "-m",
                "omnigent",
                "host",
                "--server",
                server_url,
                "--non-interactive",
                "--no-open",
            ],
        ) as host:
            host.wait_online(http_client, timeout=90)
            assert ("CONNECT", f"{_SERVER_HOST}:{server_port}") in relayed

            agent_name = upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
            workspace = tmp_path / "project"
            workspace.mkdir()
            response = http_client.post(
                "/v1/sessions",
                json={
                    "agent_id": lookup_agent_id(http_client, agent_name),
                    "host_id": host.host_id,
                    "workspace": str(workspace),
                },
                timeout=90,
            )
            response.raise_for_status()
            runner_id = response.json()["runner_id"]
            assert runner_id, response.json()
            runner_online = False
            deadline = time.monotonic() + 60
            while host.proc.poll() is None and time.monotonic() < deadline:
                status = http_client.get(f"/v1/runners/{runner_id}/status", timeout=5)
                status.raise_for_status()
                if status.json().get("online"):
                    runner_online = True
                    break
                time.sleep(0.5)
            assert runner_online, (
                f"Runner {runner_id} never came online; relayed requests={relayed}\n"
                f"{host.log_path.read_text(errors='replace')[-3000:]}"
            )
            assert relayed.count(("CONNECT", f"{_SERVER_HOST}:{server_port}")) >= 2
