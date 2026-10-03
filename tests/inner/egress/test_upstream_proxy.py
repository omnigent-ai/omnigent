import asyncio
import socket
import ssl
import threading

import pytest

from omnigent.inner.egress.ca import ensure_ca
from omnigent.inner.egress.certs import HostCertCache
from omnigent.inner.egress.proxy import _open_upstream


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for k in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(k.upper(), raising=False)


async def test_http_goes_through_connect_tunnel(monkeypatch, clean_proxy_env):
    seen = []

    async def proxy(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        seen.append(head)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        writer.write(b"tunnelled")
        await writer.drain()
        writer.close()

    server, port = await _serve(proxy)
    monkeypatch.setenv("http_proxy", f"http://user:p%40ss@127.0.0.1:{port}")
    reader, writer = await _open_upstream("example.test", "203.0.113.5", 80)
    assert await reader.read() == b"tunnelled"
    writer.close()
    server.close()
    assert seen[0].startswith("CONNECT 203.0.113.5:80 HTTP/1.1")
    assert "Proxy-Authorization: Basic dXNlcjpwQHNz" in seen[0]


async def test_refused_connect_raises(monkeypatch, clean_proxy_env):
    async def proxy(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        await writer.drain()
        writer.close()

    server, port = await _serve(proxy)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
    with pytest.raises(OSError, match="403"):
        await _open_upstream("example.test", "203.0.113.5", 80)
    server.close()


async def test_no_proxy_connects_directly(monkeypatch, clean_proxy_env):
    async def target(reader, writer):
        writer.write(b"direct")
        await writer.drain()
        writer.close()

    server, port = await _serve(target)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")  # would fail if used
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    reader, writer = await _open_upstream("127.0.0.1", "127.0.0.1", port)
    assert await reader.read() == b"direct"
    writer.close()
    server.close()


async def test_https_handshakes_with_original_host_over_tunnel(
    monkeypatch, clean_proxy_env, tmp_path
):
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    server_ctx = HostCertCache(cert_path, key_path).get_ssl_context("example.test")
    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    sni = []
    server_ctx.sni_callback = lambda _s, name, _c: sni.append(name)
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = listener.accept()
        while b"\r\n\r\n" not in conn.recv(4096, socket.MSG_PEEK):
            pass
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        with server_ctx.wrap_socket(conn, server_side=True) as tls:
            tls.sendall(b"secure")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{listener.getsockname()[1]}")
    # Connect to a pinned IP; cert verification must still bind to the hostname.
    reader, writer = await _open_upstream("example.test", "203.0.113.5", 443, client_ctx)
    assert await reader.read() == b"secure"
    writer.close()
    t.join(5)
    listener.close()
    assert sni == ["example.test"]


async def test_https_proxy_url_is_rejected(monkeypatch, clean_proxy_env):
    monkeypatch.setenv("http_proxy", "https://user:secret@127.0.0.1:1")
    with pytest.raises(OSError, match="http://") as exc:
        await _open_upstream("example.test", "203.0.113.5", 80)
    assert "secret" not in str(exc.value)
