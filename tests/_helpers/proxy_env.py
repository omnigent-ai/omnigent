"""Loopback HTTP stub and proxy-environment primitives for proxy-bypass tests.

These supply only the transport pieces tests would otherwise duplicate; each
test keeps its own proxy configuration, calls, and assertions.
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import socketserver
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@dataclass
class LoopbackStub:
    """A loopback HTTP server answering every GET with one JSON body.

    :param port: Ephemeral port the stub listens on.
    :param paths: Request targets received so far; a proxied request arrives in
        absolute form, e.g. ``"http://remote.test:4096/api/model"``.
    """

    port: int
    paths: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        """:returns: The stub's base URL, e.g. ``"http://127.0.0.1:49231"``."""
        return f"http://127.0.0.1:{self.port}"


@contextlib.contextmanager
def loopback_json_stub(body: bytes) -> Iterator[LoopbackStub]:
    """
    Serve *body* as JSON for every GET on an ephemeral loopback port.

    :param body: Response body, e.g. ``b'{"models": []}'``.
    :yields: The running stub, recording each request target in ``paths``.
    """
    stub: LoopbackStub

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            stub.paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    stub = LoopbackStub(port=httpd.server_address[1])
    try:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield stub
    finally:
        httpd.shutdown()
        httpd.server_close()


@contextlib.contextmanager
def dead_proxy_url() -> Iterator[str]:
    """
    Reserve a loopback port that is bound but never listens.

    :yields: A proxy URL whose connects are refused deterministically for the
        duration of the block, e.g. ``"http://127.0.0.1:49232"``.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    try:
        yield f"http://127.0.0.1:{probe.getsockname()[1]}"
    finally:
        probe.close()


def set_proxy_env(monkeypatch: pytest.MonkeyPatch, proxy_url: str) -> None:
    """
    Point every proxy variable at *proxy_url* with no loopback exemption.

    :param monkeypatch: The test's monkeypatch fixture.
    :param proxy_url: Proxy to configure, e.g. ``"http://127.0.0.1:49232"``.
    """
    for name in PROXY_ENV_VARS:
        monkeypatch.setenv(name, proxy_url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
