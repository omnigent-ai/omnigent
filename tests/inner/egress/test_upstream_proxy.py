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


async def test_http_goes_through_connect_tunnel(monkeypatch):
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


async def test_refused_connect_raises(monkeypatch):
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


async def test_no_proxy_connects_directly(monkeypatch):
    async def target(reader, writer):
        writer.write(b"direct")
        await writer.drain()
        writer.close()

    server, port = await _serve(target)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")  # would fail if used
    monkeypatch.setenv("no_proxy", "localhost")
    reader, writer = await _open_upstream("localhost", "127.0.0.1", port)
    assert await reader.read() == b"direct"
    writer.close()
    server.close()


async def test_https_handshakes_with_original_host_over_tunnel(monkeypatch, tmp_path):
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    server_ctx = HostCertCache(cert_path, key_path).get_ssl_context("example.test")
    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    sni = []
    server_ctx.sni_callback = lambda _s, name, _c: sni.append(name)
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = listener.accept()
        while (peeked := conn.recv(4096, socket.MSG_PEEK)) and b"\r\n\r\n" not in peeked:
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


@pytest.mark.parametrize(
    "proxy_url",
    ["https://user:secret@127.0.0.1:1", "socks5://127.0.0.1:1"],
)
async def test_unsupported_proxy_scheme_logs_error_and_connects_directly(
    monkeypatch, proxy_url, caplog
):
    async def target(reader, writer):
        writer.write(b"direct")
        await writer.drain()
        writer.close()

    server, port = await _serve(target)
    monkeypatch.setenv("all_proxy", proxy_url)
    reader, writer = await _open_upstream("example.test", "127.0.0.1", port)
    assert await reader.read() == b"direct"
    writer.close()
    server.close()
    assert "Unsupported upstream proxy scheme" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("reply", [b"garbage 200 OK", b"HTTP/1.1 502 secret-reason"])
async def test_bad_connect_reply_is_rejected_without_echoing_reason(monkeypatch, reply):
    async def proxy(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(reply + b"\r\n\r\n")
        await writer.drain()
        writer.close()

    server, port = await _serve(proxy)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
    with pytest.raises(OSError, match="refused CONNECT") as exc:
        await _open_upstream("example.test", "203.0.113.5", 80)
    server.close()
    assert "reason" not in str(exc.value)


async def test_any_2xx_connect_reply_is_accepted(monkeypatch):
    async def proxy(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 204 No Content\r\n\r\nok")
        await writer.drain()
        writer.close()

    server, port = await _serve(proxy)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
    reader, writer = await _open_upstream("example.test", "203.0.113.5", 80)
    assert await reader.read() == b"ok"
    writer.close()
    server.close()


async def test_schemeless_proxy_is_read_as_http(monkeypatch):
    seen = []

    async def proxy(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\n\r\nok")
        await writer.drain()
        writer.close()

    server, port = await _serve(proxy)
    monkeypatch.setenv("http_proxy", f"127.0.0.1:{port}")
    reader, writer = await _open_upstream("example.test", "203.0.113.5", 80)
    assert await reader.read() == b"ok"
    writer.close()
    server.close()
    assert seen[0].startswith(b"CONNECT 203.0.113.5:80")


async def test_no_proxy_entry_with_port_bypasses_proxy(monkeypatch):
    async def target(reader, writer):
        writer.write(b"direct")
        await writer.drain()
        writer.close()

    server, port = await _serve(target)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")  # would fail if used
    monkeypatch.setenv("no_proxy", f"localhost:{port}")
    reader, writer = await _open_upstream("localhost", "127.0.0.1", port)
    assert await reader.read() == b"direct"
    writer.close()
    server.close()


async def test_all_proxy_is_fallback(monkeypatch):
    seen = []

    async def proxy(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\n\r\nok")
        await writer.drain()
        writer.close()

    server, port = await _serve(proxy)
    monkeypatch.setenv("all_proxy", f"http://127.0.0.1:{port}")
    reader, writer = await _open_upstream("example.test", "203.0.113.5", 80)
    assert await reader.read() == b"ok"
    writer.close()
    server.close()
    assert seen[0].startswith(b"CONNECT 203.0.113.5:80")
