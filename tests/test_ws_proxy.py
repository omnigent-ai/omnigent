"""Proxy environment selection and real CONNECT socket round-trips."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import logging
import socket
import ssl
import threading
import time
import types
from contextlib import asynccontextmanager
from http import HTTPStatus

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from omnigent.inner.egress.ca import ensure_ca
from omnigent.inner.egress.certs import HostCertCache
from omnigent.util.ws_proxy import (
    open_proxy_connect_socket,
    redact_proxy_url,
    ws_env_proxy_url,
)
from tests._helpers.polling import poll_until_async

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


@pytest.mark.parametrize(
    ("userinfo", "ws_url", "authority"),
    [
        ("", _TUNNEL_URL, b"server.sandbox.test:8000"),
        ("alice:open%20sesame@", _TUNNEL_URL, b"server.sandbox.test:8000"),
        ("", "ws://bücher.example:8000/t", b"xn--bcher-kva.example:8000"),
        ("", "ws://server.sandbox.test/t", b"server.sandbox.test:80"),
        ("", "wss://[fd00::1]/t", b"[fd00::1]:443"),
    ],
    ids=["anonymous", "authenticated", "idna-host", "ws-default-port", "ipv6-wss-default-port"],
)
async def test_connect_tunnel(userinfo, ws_url, authority):
    async with _connect_proxy(_OK) as (port, requests):
        url = f"http://{userinfo}127.0.0.1:{port}"
        with await open_proxy_connect_socket(url, ws_url, timeout=5) as sock:
            expected = b"CONNECT " + authority + b" HTTP/1.1\r\nHost: " + authority + b"\r\n"
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
        (b"HTTP/1.1 407 secret-reason\r\n\r\n", "refused CONNECT.* with status 407$"),
        (b"HTTP/1.1 403 secret-reason\r\n\r\n", "status 403; .*NO_PROXY must list the server"),
        (b"GARBAGE 200 secret-reason\r\n\r\n", "non-HTTP response"),
        (b"HTTP/1.1 \xb200 secret-reason\r\n\r\n", "refused CONNECT.* an invalid status$"),
        (b"HTTP/1.1 " + b"9" * 5000 + b" secret-reason\r\n\r\n", "an invalid status$"),
        (b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 65607 + b"\r\n\r\n", "oversized"),
        (_OK + b"GARBAGE", "unexpected bytes"),
        (b"", "closed the connection"),
    ],
    ids=[
        "refused",
        "forbidden",
        "non-http",
        "non-decimal-status",
        "overlong-status",
        "oversized",
        "trailing-bytes",
        "eof",
    ],
)
async def test_connect_error(monkeypatch, reply, error):
    from omnigent.util import ws_proxy

    dialed = []
    dial_proxy = ws_proxy._dial_proxy

    def recording_dial_proxy(*args):
        dialed.append(dial_proxy(*args))
        return dialed[-1]

    monkeypatch.setattr(ws_proxy, "_dial_proxy", recording_dial_proxy)
    async with _connect_proxy(reply) as (port, _requests):
        with pytest.raises(OSError, match=error) as raised:
            await open_proxy_connect_socket(f"http://127.0.0.1:{port}", _TUNNEL_URL, timeout=5)
    assert "secret-reason" not in str(raised.value)
    assert len(dialed) == 1
    assert dialed[0].fileno() == -1


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


def _held_dial(monkeypatch) -> tuple[socket.socket, threading.Event, threading.Event]:
    """Block the dial worker until released; it then returns the socket.

    :returns: The socket, an event set once the worker starts, and the release event.
    """
    from omnigent.util import ws_proxy

    sock, started, release = socket.socket(), threading.Event(), threading.Event()

    def held_dial(*_args):
        started.set()
        release.wait(5)
        return sock

    monkeypatch.setattr(ws_proxy, "_connect_sync", held_dial)
    return sock, started, release


async def test_connect_closes_socket_when_awaiter_is_cancelled(monkeypatch):
    """A socket the worker thread hands back after cancellation is closed, not orphaned."""
    sock, started, release = _held_dial(monkeypatch)
    dial = asyncio.ensure_future(
        open_proxy_connect_socket("http://127.0.0.1:1", _TUNNEL_URL, timeout=5)
    )
    await poll_until_async(started.is_set, timeout_s=5, interval_s=0.005)
    dial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dial
    release.set()
    await poll_until_async(lambda: sock.fileno() == -1, timeout_s=2)


async def test_connect_timeout_bounds_a_slow_dial(monkeypatch):
    """The budget caps the dial itself, not only the handshake after it."""
    sock, _started, release = _held_dial(monkeypatch)
    began = time.monotonic()
    with pytest.raises(TimeoutError, match=r"within 0\.3s"):
        await open_proxy_connect_socket("http://127.0.0.1:1", _TUNNEL_URL, timeout=0.3)
    assert time.monotonic() - began < 0.7
    release.set()
    await poll_until_async(lambda: sock.fileno() == -1, timeout_s=2)


@pytest.mark.parametrize("dial_fails", [False, True])
async def test_connect_timeout_race_with_completed_dial(monkeypatch, dial_fails):
    """A deadline that fires as the dial completes closes its socket or keeps its error."""
    from omnigent.util import ws_proxy

    sock = None if dial_fails else socket.socket()

    def finish_dial(*_args):
        if sock is None:
            raise ConnectionRefusedError("refused by test proxy")
        return sock

    monkeypatch.setattr(ws_proxy, "_connect_sync", finish_dial)

    async def wait_for_after_completion(awaitable, _timeout):
        with contextlib.suppress(ConnectionRefusedError):
            await awaitable
        raise TimeoutError

    # Patch the module's own asyncio reference, not the shared stdlib module.
    patched_asyncio = types.SimpleNamespace(**vars(asyncio))
    patched_asyncio.wait_for = wait_for_after_completion
    monkeypatch.setattr(ws_proxy, "asyncio", patched_asyncio)
    error, message = (
        (ConnectionRefusedError, "refused by test proxy")
        if dial_fails
        else (TimeoutError, "within 5s")
    )
    with pytest.raises(error, match=message):
        await open_proxy_connect_socket("http://127.0.0.1:1", _TUNNEL_URL, timeout=5)
    await asyncio.sleep(0)
    assert sock is None or sock.fileno() == -1


def test_proxy_dial_shares_one_budget_across_addresses(monkeypatch):
    """A refused address falls through to the next; a blackholed one ends the dial."""
    from omnigent.util import ws_proxy

    addresses = [("192.0.2.1", 3128), ("192.0.2.2", 3128), ("192.0.2.3", 3128)]
    clock = [100.0]
    attempts = []
    opened = []

    class FakeSocket:
        def __init__(self, *_args):
            self.timeout = None
            self.closed = False
            opened.append(self)

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            attempts.append((address, self.timeout))
            if address == addresses[0]:
                clock[0] += 1.0
                raise ConnectionRefusedError("refused by test proxy")
            clock[0] += self.timeout
            raise TimeoutError("timed out")

        def close(self):
            self.closed = True

    patched_socket = types.SimpleNamespace(**vars(socket))
    patched_socket.getaddrinfo = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", address) for address in addresses
    ]
    patched_socket.socket = FakeSocket
    monkeypatch.setattr(ws_proxy, "socket", patched_socket)
    monkeypatch.setattr(ws_proxy, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(TimeoutError):
        ws_proxy._connect_sync("proxy.test", 3128, _TLS_HOST, 8000, None, 3.0)
    assert attempts == [(addresses[0], 3.0), (addresses[1], 2.0)]
    assert clock[0] == 103.0
    assert all(sock.closed for sock in opened)


def test_proxy_dial_skips_an_address_whose_socket_cannot_be_created(monkeypatch):
    """An address family the host cannot open falls through to the next address."""
    from omnigent.util import ws_proxy

    connected = []

    class FakeSocket:
        def __init__(self, family, *_args):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")

        def settimeout(self, _timeout):
            pass

        def connect(self, address):
            connected.append(address)

    patched_socket = types.SimpleNamespace(**vars(socket))
    patched_socket.getaddrinfo = lambda *_args, **_kwargs: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 3128, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 3128)),
    ]
    patched_socket.socket = FakeSocket
    monkeypatch.setattr(ws_proxy, "socket", patched_socket)
    assert isinstance(ws_proxy._dial_proxy("proxy.test", 3128, lambda: 3.0), FakeSocket)
    assert connected == [("192.0.2.1", 3128)]


async def test_connect_timeout_bounds_the_whole_handshake():
    """A proxy that drips its CONNECT reply cannot hold the dial or its socket past the budget."""
    drip_ended = asyncio.Event()

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
        drip_ended.set()

    async with await asyncio.start_server(drip, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await open_proxy_connect_socket(f"http://127.0.0.1:{port}", _TUNNEL_URL, timeout=0.5)
        assert time.monotonic() - started < 2
        # The full reply takes ~4s to drip; ending sooner means the worker hung up.
        await asyncio.wait_for(drip_ended.wait(), 1.5)


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
    ca, ca_key = ensure_ca(cache_dir=tmp_path)
    server_ctx = HostCertCache(ca, ca_key).get_ssl_context(certified_host)
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
            client_ctx = ssl.create_default_context(cafile=str(ca))
            if certified_host == _TLS_HOST:
                async with connect(url, sock=sock, ssl=client_ctx, open_timeout=5) as ws:
                    await ws.send("ping")
                    assert await ws.recv() == "ping"
            else:
                with pytest.raises(ssl.SSLCertVerificationError):
                    await connect(url, sock=sock, ssl=client_ctx, open_timeout=5)
    assert authorities == [f"{_TLS_HOST}:{port}"]
    assert seen_sni == [_TLS_HOST]


async def test_proxied_tunnel_does_not_follow_redirects():
    """A redirect cannot reuse the proxied socket, so its target is never dialed."""
    redirect_target_hits = []

    async def record_hit(_reader, writer):
        redirect_target_hits.append(True)
        writer.close()

    async with await asyncio.start_server(record_hit, "127.0.0.1", 0) as redirect_target:
        target_port = redirect_target.sockets[0].getsockname()[1]

        def redirect(connection, _request):
            response = connection.respond(HTTPStatus.FOUND, "")
            response.headers["Location"] = f"ws://127.0.0.1:{target_port}/t"
            return response

        async def unused_handler(_ws):
            pass

        async with serve(unused_handler, "127.0.0.1", 0, process_request=redirect) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://{_TLS_HOST}:{port}/t"
            async with _forwarding_proxy(port) as (proxy_port, authorities):
                sock = await open_proxy_connect_socket(
                    f"http://127.0.0.1:{proxy_port}", url, timeout=5
                )
                with pytest.raises(ValueError, match="preexisting socket"):
                    await connect(url, sock=sock, open_timeout=5)
    assert authorities == [f"{_TLS_HOST}:{port}"]
    assert redirect_target_hits == []
