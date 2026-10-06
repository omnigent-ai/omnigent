"""A real host must come online when only its egress proxy can resolve the server."""

from __future__ import annotations

import contextlib
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

_REPO_ROOT = Path(__file__).resolve().parents[2]
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


def test_host_comes_online_through_mandatory_proxy(
    live_server: str, http_client: httpx.Client, tmp_path: Path
) -> None:
    server_port = urlsplit(live_server).port
    assert server_port is not None
    server_url = f"http://{_SERVER_HOST}:{server_port}"
    host_id = uuid.uuid4().hex
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir()
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": f"proxy-test-{host_id}"}})
    )
    # Only the proxy may resolve the server; the real host imports this DNS shim.
    (tmp_path / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    host_log = tmp_path / "host-daemon.log"
    host_log.touch()
    console_log = tmp_path / "host-console.log"

    with _mandatory_proxy(server_port) as (proxy_url, relayed):
        assert httpx.get(f"{server_url}/health", proxy=proxy_url, timeout=10).status_code == 200
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            PROCESS_LOG_FILE_ENV_VAR: str(host_log),
            "PYTHONPATH": os.pathsep.join(
                [str(tmp_path), str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "ALL_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "all_proxy": proxy_url,
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
        with console_log.open("w") as console:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnigent",
                    "host",
                    "--server",
                    server_url,
                    "--non-interactive",
                    "--no-open",
                ],
                env=env,
                cwd=_REPO_ROOT,
                stdout=console,
                stderr=subprocess.STDOUT,
            )
            try:
                online = False
                deadline = time.monotonic() + 90
                while proc.poll() is None and time.monotonic() < deadline:
                    hosts: list[dict[str, object]] = []
                    with contextlib.suppress(httpx.HTTPError):
                        response = http_client.get("/v1/hosts", timeout=5)
                        response.raise_for_status()
                        hosts = response.json().get("hosts", [])
                    online = any(
                        host.get("host_id") == host_id and host.get("status") == "online"
                        for host in hosts
                    )
                    if (
                        online
                        or host_log.read_text(errors="replace").count(
                            "Temporary failure in name resolution"
                        )
                        >= 5
                    ):
                        break
                    time.sleep(0.5)
                assert online, (
                    f"Host never came online: exit={proc.poll()}, relayed requests={relayed}\n"
                    f"{host_log.read_text(errors='replace')[-3000:]}\n"
                    f"{console_log.read_text(errors='replace')[-3000:]}"
                )
                assert ("CONNECT", f"{_SERVER_HOST}:{server_port}") in relayed
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
