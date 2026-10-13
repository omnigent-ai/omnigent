"""Proxy environment selection and real CONNECT socket round-trips."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import logging
import socket
import ssl
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from omnigent.util.ws_proxy import (
    open_proxy_connect_socket,
    redact_proxy_url,
    ws_env_proxy_url,
)

_TLS_HOST = "server.sandbox.test"
_TUNNEL_URL = f"ws://{_TLS_HOST}:8000/v1/hosts/h/tunnel"
_OK = b"HTTP/1.1 200 Connection Established\r\n\r\n"


@pytest.mark.parametrize(
    ("url", "env", "expected"),
    [
        (_TUNNEL_URL, {"http_proxy": "http://p:1"}, "http://p:1"),
        ("wss://s.test/t", {"https_proxy": "http://p:1"}, "http://p:1"),
        ("wss://s.test/t", {"http_proxy": "http://p:1"}, None),
        ("ws://s.test/t", {"ALL_PROXY": "http://p:1"}, "http://p:1"),
        ("wss://s.test/t", {"ALL_PROXY": "http://p:1"}, "http://p:1"),
        (_TUNNEL_URL, {"http_proxy": "http://p:1", "HTTP_PROXY": "http://p:2"}, "http://p:1"),
        (_TUNNEL_URL, {}, None),
        (_TUNNEL_URL, {"http_proxy": "p:1"}, "http://p:1"),
        (_TUNNEL_URL, {"http_proxy": "socks5://p:1"}, None),
        ("unix:///tmp/sock", {"all_proxy": "http://p:1"}, None),
        ("not a url", {"all_proxy": "http://p:1"}, None),
        ("ws://[::1:8000/t", {"http_proxy": "http://p:1"}, None),
        (_TUNNEL_URL, {"http_proxy": "http://[::1:3128"}, None),
        # A lowercase variable that is set but empty suppresses its uppercase
        # form, as it does for urllib and httpx.
        (_TUNNEL_URL, {"HTTP_PROXY": "http://p:2", "http_proxy": ""}, None),
        (_TUNNEL_URL, {"HTTP_PROXY": "", "http_proxy": "http://p:1"}, "http://p:1"),
        (_TUNNEL_URL, {"HTTP_PROXY": ""}, None),
        (_TUNNEL_URL, {"http_proxy": "", "all_proxy": "http://p:1"}, "http://p:1"),
        (_TUNNEL_URL, {"http_proxy": "http://p:1", "NO_PROXY": "server.sandbox.test"}, None),
        (
            _TUNNEL_URL,
            {"http_proxy": "http://p:1", "NO_PROXY": "server.sandbox.test", "no_proxy": ""},
            "http://p:1",
        ),
        # Any capitalisation counts, as in urllib/httpx; a lowercase-suffixed
        # spelling wins and may suppress.
        (_TUNNEL_URL, {"Http_Proxy": "http://p:1"}, "http://p:1"),
        (_TUNNEL_URL, {"http_proxy": "http://p:1", "No_Proxy": "server.sandbox.test"}, None),
        (_TUNNEL_URL, {"HTTP_PROXY": "http://p:2", "HTTP_proxy": ""}, None),
        # urllib ignores HTTP_PROXY under CGI; the lowercase spelling survives.
        (_TUNNEL_URL, {"HTTP_PROXY": "http://p:1", "REQUEST_METHOD": "GET"}, None),
        (_TUNNEL_URL, {"http_proxy": "http://p:1", "REQUEST_METHOD": "GET"}, "http://p:1"),
        # URL-form bypass entries match by scheme like httpx mounts.
        (
            "wss://example.com/t",
            {"https_proxy": "http://p:1", "no_proxy": "https://example.com"},
            None,
        ),
        (
            "wss://example.com/t",
            {"https_proxy": "http://p:1", "no_proxy": "http://example.com"},
            "http://p:1",
        ),
        # A scheme wildcard outranks httpx's all:// mount from all_proxy, but
        # not a scheme-specific proxy mount.
        (_TUNNEL_URL, {"all_proxy": "http://p:1", "no_proxy": "http://*"}, None),
        ("wss://example.com/t", {"all_proxy": "http://p:1", "no_proxy": "https://*"}, None),
        (_TUNNEL_URL, {"all_proxy": "http://p:1", "no_proxy": "https://*"}, "http://p:1"),
        (_TUNNEL_URL, {"http_proxy": "http://p:1", "no_proxy": "http://*"}, "http://p:1"),
        (_TUNNEL_URL, {"all_proxy": "http://p:1", "no_proxy": "all://*"}, "http://p:1"),
        # Loopback never goes through a proxy, even without a no_proxy entry.
        ("ws://localhost:8000/t", {"http_proxy": "http://p:1"}, None),
        ("ws://127.0.0.1:8000/t", {"ALL_PROXY": "http://p:1"}, None),
        ("wss://[::1]:8443/t", {"https_proxy": "http://p:1"}, None),
    ],
)
def test_proxy_selection(url, env, expected):
    assert ws_env_proxy_url(url, env) == expected


@pytest.mark.parametrize(
    ("host", "no_proxy", "bypass"),
    [
        # Expectations mirror httpx 0.28, the host's own HTTP client.
        ("example.com", "example.com", True),
        ("sub.example.com", "example.com", True),
        ("notexample.com", "example.com", False),
        ("example.com", ".example.com", False),
        ("sub.example.com", ".example.com", True),
        ("deep.sub.example.com", ".example.com", True),
        ("example.com", "*.example.com", False),
        ("sub.example.com", "*.example.com", False),
        ("anything.test", "*", True),
        ("example.com:8443", "example.com:8443", True),
        ("sub.example.com:8443", "example.com:8443", True),
        ("example.com:8000", "example.com:8443", False),
        ("example.com", "example.com:80", False),
        ("example.com:80", "example.com:80", False),
        # URL-form entries behave like httpx mounts: scheme, host pattern and port.
        ("example.com", "http://example.com", True),
        ("example.com", "https://example.com", False),
        ("example.com", "all://example.com", True),
        ("sub.example.com", "http://example.com", False),
        ("sub.example.com", "http://*.example.com", True),
        ("example.com", "http://*example.com", True),
        ("example.com:8443", "http://example.com:8443", True),
        ("example.com:8443", "http://example.com", True),
        ("example.com", "http://example.com:80", True),
        ("example.com", "all://*", False),
        ("example.com:8443", "http://*:8443", True),
        ("example.com", "http://*:8443", False),
        ("10.1.2.3:8000", "localhost,10.1.2.3,fd00::1", True),
        ("[fd00::1]:8000", "localhost,10.1.2.3,fd00::1", True),
        ("[fd00::1]:8000", "[fd00::1]:8000", True),
        ("[fd00:0:0:0:0:0:0:1]:8000", "fd00::1", False),
        ("evil.10.1.2.3:8000", "10.1.2.3", False),
        # CIDR entries are not interpreted, as in httpx.
        ("10.1.2.3:8000", "10.0.0.0/8", False),
        # Deliberately stricter than httpx, which suffix-matches IP entries with a port.
        ("evil.10.1.2.3:8000", "10.1.2.3:8000", False),
        ("server.sandbox.test:8000", "localhost,10.1.2.3,fd00::1", False),
    ],
)
def test_no_proxy(host, no_proxy, bypass):
    env = {"http_proxy": "http://p:1", "no_proxy": no_proxy}
    assert ws_env_proxy_url(f"ws://{host}/t", env) == (None if bypass else "http://p:1")


@pytest.mark.parametrize("userinfo", ["", "user:secret@"])
def test_redact_proxy_url(userinfo):
    assert redact_proxy_url(f"http://{userinfo}proxy:3128") == "http://proxy:3128"
    assert redact_proxy_url(f"http://{userinfo}proxy:3128/path?token=x#f") == "http://proxy:3128"


@pytest.mark.parametrize(
    ("variable", "scheme"),
    [("http_proxy", "socks5"), ("ALL_PROXY", "socks5"), ("http_proxy", "https")],
)
def test_unsupported_scheme_warns_once_without_credentials(monkeypatch, caplog, variable, scheme):
    from omnigent.util import ws_proxy

    monkeypatch.setattr(ws_proxy, "_warned_proxy_env", set())
    env = {variable: f"{scheme}://user:secret@p:1"}
    with caplog.at_level(logging.WARNING, logger="omnigent.util.ws_proxy"):
        assert ws_env_proxy_url(_TUNNEL_URL, env) is None
        assert ws_env_proxy_url(_TUNNEL_URL, env) is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert variable.lower() in warnings[0] and scheme in warnings[0]
    assert "secret" not in warnings[0]


@asynccontextmanager
async def _connect_proxy(reply):
    requests = []

    async def respond(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(reply)
            await writer.drain()
            if reply == _OK:
                with contextlib.suppress(asyncio.IncompleteReadError):
                    writer.write(await reader.readexactly(4))
                    await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        yield server.sockets[0].getsockname()[1], requests


@pytest.mark.parametrize("userinfo", ["", "alice:open%20sesame@"])
async def test_connect_tunnel(userinfo):
    async with _connect_proxy(_OK) as (port, requests):
        url = f"http://{userinfo}127.0.0.1:{port}"
        with await open_proxy_connect_socket(url, _TUNNEL_URL, timeout=5) as sock:
            expected = (
                b"CONNECT server.sandbox.test:8000 HTTP/1.1\r\nHost: server.sandbox.test:8000\r\n"
            )
            if userinfo:
                expected += (
                    b"Proxy-Authorization: Basic "
                    + base64.b64encode(b"alice:open sesame")
                    + b"\r\n"
                )
            assert requests == [expected + b"\r\n"]
            assert sock.gettimeout() is None
            reader, writer = await asyncio.open_connection(sock=sock)
            try:
                writer.write(b"ping")
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(4), timeout=5) == b"ping"
            finally:
                writer.close()
                await writer.wait_closed()


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n", "refused CONNECT"),
        (b"GARBAGE 200 OK\r\n\r\n", "non-HTTP response"),
        (b"HTTP/1.1 \xb200 OK\r\n\r\n", "refused CONNECT"),
        (b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 65607 + b"\r\n\r\n", "oversized"),
        (_OK + b"GARBAGE", "unexpected bytes"),
        (b"", "closed the connection"),
    ],
    ids=["refused", "non-http", "non-decimal-status", "oversized", "trailing-bytes", "eof"],
)
async def test_connect_error(reply, error):
    async with _connect_proxy(reply) as (port, _requests):
        with pytest.raises(OSError, match=error):
            await open_proxy_connect_socket(f"http://127.0.0.1:{port}", _TUNNEL_URL, timeout=5)


async def test_connect_tunnel_idna_host():
    async with _connect_proxy(_OK) as (port, requests):
        with await open_proxy_connect_socket(
            f"http://127.0.0.1:{port}", "ws://bücher.example:8000/t", timeout=5
        ):
            pass
        authority = b"xn--bcher-kva.example:8000"
        assert requests == [
            b"CONNECT " + authority + b" HTTP/1.1\r\nHost: " + authority + b"\r\n\r\n"
        ]


@pytest.mark.parametrize(
    ("proxy_url", "ws_url", "error"),
    [
        ("http://127.0.0.1:bad", _TUNNEL_URL, "invalid port"),
        ("http://[::1:3128", _TUNNEL_URL, "malformed"),
        ("http://127.0.0.1:1", "ws://server.sandbox.test:bad/t", "invalid port"),
        ("http://127.0.0.1:1", "ws://[::1:8000/t", "malformed"),
    ],
)
async def test_connect_rejects_malformed_proxy_url(proxy_url, ws_url, error):
    with pytest.raises(OSError, match=error):
        await open_proxy_connect_socket(proxy_url, ws_url, timeout=5)


async def test_connect_closes_socket_when_awaiter_is_cancelled(monkeypatch):
    """A socket the worker thread hands back after cancellation is closed, not orphaned."""
    from omnigent.util import ws_proxy

    sock = socket.socket()

    def slow_connect(*_args):
        time.sleep(0.2)
        return sock

    monkeypatch.setattr(ws_proxy, "_connect_sync", slow_connect)
    dial = asyncio.ensure_future(
        open_proxy_connect_socket("http://127.0.0.1:1", _TUNNEL_URL, timeout=5)
    )
    await asyncio.sleep(0.05)
    dial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dial
    for _ in range(100):
        if sock.fileno() == -1:
            break
        await asyncio.sleep(0.02)
    assert sock.fileno() == -1


async def test_connect_timeout_bounds_a_slow_dial(monkeypatch):
    """The budget caps the dial itself, not only the handshake after it."""
    from omnigent.util import ws_proxy

    sock = socket.socket()

    def slow_dial(*_args):
        time.sleep(0.8)
        return sock

    monkeypatch.setattr(ws_proxy, "_connect_sync", slow_dial)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"within 0\.3s"):
        await open_proxy_connect_socket("http://127.0.0.1:1", _TUNNEL_URL, timeout=0.3)
    assert time.monotonic() - started < 0.7
    for _ in range(100):
        if sock.fileno() == -1:
            break
        await asyncio.sleep(0.02)
    assert sock.fileno() == -1


async def test_connect_timeout_race_closes_completed_dial(monkeypatch):
    """A deadline that fires as the dial completes still closes the returned socket."""
    from omnigent.util import ws_proxy

    sock = socket.socket()
    monkeypatch.setattr(ws_proxy, "_connect_sync", lambda *_args: sock)

    async def wait_for_after_completion(awaitable, _timeout):
        await awaitable
        raise TimeoutError

    # Patch the module's own asyncio reference, not the shared stdlib module.
    patched_asyncio = types.SimpleNamespace(**vars(asyncio))
    patched_asyncio.wait_for = wait_for_after_completion
    monkeypatch.setattr(ws_proxy, "asyncio", patched_asyncio)
    with pytest.raises(TimeoutError, match="within 5s"):
        await open_proxy_connect_socket("http://127.0.0.1:1", _TUNNEL_URL, timeout=5)
    await asyncio.sleep(0)
    assert sock.fileno() == -1


async def test_connect_timeout_bounds_the_whole_handshake():
    """A proxy that drips its CONNECT reply cannot stretch past the total budget."""

    async def drip(reader, writer):
        with contextlib.suppress(OSError, asyncio.IncompleteReadError):
            await reader.readuntil(b"\r\n\r\n")
            for byte in _OK:
                if reader.at_eof():
                    break
                writer.write(bytes([byte]))
                await writer.drain()
                await asyncio.sleep(0.1)
        writer.close()

    async with await asyncio.start_server(drip, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await open_proxy_connect_socket(f"http://127.0.0.1:{port}", _TUNNEL_URL, timeout=0.5)
        assert time.monotonic() - started < 2


def _self_signed_cert(directory: Path, hostname: str) -> Path:
    """Write a self-signed certificate and key for *hostname*; return the cert path."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path = directory / "cert.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path


@asynccontextmanager
async def _forwarding_proxy(upstream_port):
    """CONNECT proxy that resolves the tunnel host itself and relays raw bytes."""
    authorities = []

    async def relay(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        authorities.append(head.split(b" ", 2)[1].decode())
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1", upstream_port
        )
        writer.write(_OK)
        await writer.drain()
        await asyncio.gather(
            relay(reader, upstream_writer), relay(upstream_reader, writer), return_exceptions=True
        )

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        yield server.sockets[0].getsockname()[1], authorities


@pytest.mark.parametrize("certified_host", [_TLS_HOST, "other.sandbox.test"])
async def test_connect_tunnel_wss(tmp_path, certified_host):
    """TLS rides the CONNECT tunnel and is verified against the tunnel URL's host."""
    cert = _self_signed_cert(tmp_path, certified_host)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, tmp_path / "key.pem")
    seen_sni = []
    server_ctx.sni_callback = lambda _sock, name, _ctx: seen_sni.append(name)

    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    async with serve(echo, "127.0.0.1", 0, ssl=server_ctx) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"wss://{_TLS_HOST}:{port}/t"
        async with _forwarding_proxy(port) as (proxy_port, authorities):
            sock = await open_proxy_connect_socket(
                f"http://127.0.0.1:{proxy_port}", url, timeout=5
            )
            client_ctx = ssl.create_default_context(cafile=str(cert))
            if certified_host == _TLS_HOST:
                async with connect(url, sock=sock, ssl=client_ctx, open_timeout=5) as ws:
                    await ws.send("ping")
                    assert await ws.recv() == "ping"
            else:
                with pytest.raises(ssl.SSLCertVerificationError):
                    await connect(url, sock=sock, ssl=client_ctx, open_timeout=5)
    assert authorities == [f"{_TLS_HOST}:{port}"]
    assert seen_sni == [_TLS_HOST]
