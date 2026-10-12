"""Behavior of the resilience lab's fault-injecting proxy against raw sockets."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.proxy import (
    FaultProxy,
    LoopThread,
    client_link,
    fixed_link,
    host_link,
    tag_matches,
)
from tests.e2e.resilience.lab.tls import TlsInterceptor

_TUNNEL = b"GET /v1/runners/r1/tunnel HTTP/1.1\r\nHost: x\r\n\r\n"
_HTTP = b"POST /v1/sessions/conv_1/events HTTP/1.1\r\nHost: x\r\n\r\n"
_HOLD_S = 0.4


@dataclass
class _Upstream:
    """Echo server that can answer a WebSocket-style upgrade or stay silent."""

    port: int
    server: asyncio.base_events.Server
    received: list[bytes]


async def _start_upstream(mode: str) -> _Upstream:
    received: list[bytes] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upgraded = False
        try:
            while data := await reader.read(65536):
                received.append(data)
                if mode == "silent":
                    continue
                if mode == "upgrade" and not upgraded:
                    upgraded = True
                    writer.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
                    continue
                writer.write(b"echo:" + data)
                await writer.drain()
                if mode == "once":
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    return _Upstream(int(server.sockets[0].getsockname()[1]), server, received)


@pytest.fixture
def loop_thread() -> Iterator[LoopThread]:
    loop = LoopThread("fault-proxy-test")
    loop.start()
    yield loop
    loop.stop()


def _proxy(
    loop: LoopThread,
    upstream: _Upstream,
    *,
    upstream_down: str = "reset",
    classify=host_link,
    intercept: TlsInterceptor | None = None,
) -> FaultProxy:
    proxy = FaultProxy(
        "host",
        ("127.0.0.1", upstream.port),
        loop=loop,
        events=EventLog(),
        classify=classify,
        upstream_down=upstream_down,  # type: ignore[arg-type]
        intercept=intercept,
    )
    proxy.start()
    return proxy


def _upstream(loop: LoopThread, mode: str = "echo") -> _Upstream:
    return loop.run(_start_upstream(mode))


def _connect(proxy: FaultProxy, first: bytes, timeout: float = 2.0) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=timeout)
    sock.sendall(first)
    return sock


def _recv(sock: socket.socket, timeout: float = 2.0) -> bytes:
    sock.settimeout(timeout)
    return sock.recv(65536)


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_tags_connections_by_first_request_line() -> None:
    assert host_link("GET", "/v1/runners/r1/tunnel") == "runner.tunnel"
    assert host_link("GET", "/v1/hosts/h1/tunnel") == "host.tunnel"
    assert host_link("POST", "/v1/sessions/c/events") == "host.http"
    assert client_link("GET", "/v1/sessions/c/stream") == "client.sse"
    assert client_link("GET", "/v1/sessions/updates") == "client.ws"
    assert client_link("GET", "/v1/sessions/c/resources/terminals/t/attach") == "client.ws"
    assert client_link("POST", "/v1/sessions/c/events") == "client.http"
    assert tag_matches("client.sse", {"client"})
    assert not tag_matches("clientx", {"client"})
    assert tag_matches("anything", None)


def test_forwards_bytes_and_reports_the_tag(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    with contextlib.closing(_connect(proxy, _TUNNEL)) as sock:
        assert _recv(sock) == b"echo:" + _TUNNEL
        [conn] = proxy.connections()
        assert conn.tag == "runner.tunnel"
        assert conn.request_line == "GET /v1/runners/r1/tunnel HTTP/1.1"


def test_blackhole_holds_one_link_and_flushes_on_clear(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    tunnel = _connect(proxy, _TUNNEL)
    other = _connect(proxy, _HTTP)
    assert _recv(tunnel) == b"echo:" + _TUNNEL
    assert _recv(other) == b"echo:" + _HTTP

    with proxy.blackhole({"runner.tunnel"}):
        tunnel.sendall(b"held")
        other.sendall(b"flows")
        assert _recv(other) == b"echo:flows"
        with pytest.raises(TimeoutError):
            _recv(tunnel, timeout=_HOLD_S)
        assert b"held" not in b"".join(upstream.received)
    assert _recv(tunnel) == b"echo:held"
    tunnel.close()
    other.close()


def test_blackhole_holds_new_connections_before_dialing_upstream(
    loop_thread: LoopThread,
) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    with proxy.blackhole():
        sock = _connect(proxy, _HTTP)
        with pytest.raises(TimeoutError):
            _recv(sock, timeout=_HOLD_S)
        assert upstream.received == []
    assert _recv(sock) == b"echo:" + _HTTP
    sock.close()


def test_reset_sends_rst_and_close_sends_fin(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    reset_sock = _connect(proxy, _TUNNEL)
    closed_sock = _connect(proxy, _HTTP)
    assert _recv(reset_sock)
    assert _recv(closed_sock)

    assert proxy.reset({"runner.tunnel"}) == 1
    with pytest.raises(ConnectionResetError):
        _recv(reset_sock)
    assert proxy.close({"host.http"}) == 1
    assert _recv(closed_sock) == b""
    reset_sock.close()
    closed_sock.close()


def test_refuse_all_stops_listening_but_keeps_live_connections(
    loop_thread: LoopThread,
) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    live = _connect(proxy, _HTTP)
    assert _recv(live)
    with proxy.refuse():
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
        live.sendall(b"still")
        assert _recv(live) == b"echo:still"
    with contextlib.closing(_connect(proxy, _HTTP)) as again:
        assert _recv(again) == b"echo:" + _HTTP
    live.close()


def test_refuse_by_tag_resets_only_matching_new_connections(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    with proxy.refuse({"runner.tunnel"}):
        refused = _connect(proxy, _TUNNEL)
        with pytest.raises(ConnectionResetError):
            _recv(refused)
        refused.close()
        with contextlib.closing(_connect(proxy, _HTTP)) as allowed:
            assert _recv(allowed) == b"echo:" + _HTTP


def test_unreachable_upstream_is_a_gateway_error_or_a_reset(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    loop_thread.run(_close_server(upstream.server))
    gateway = _proxy(loop_thread, upstream, upstream_down="gateway")
    with contextlib.closing(_connect(gateway, _HTTP)) as sock:
        assert _recv(sock).startswith(b"HTTP/1.1 502 Bad Gateway")
    direct = _proxy(loop_thread, upstream, upstream_down="reset")
    with contextlib.closing(_connect(direct, _HTTP)) as sock, pytest.raises(ConnectionResetError):
        _recv(sock)


async def _close_server(server: asyncio.base_events.Server) -> None:
    server.close()


def test_delay_adds_latency(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    with proxy.delay(0.3, {"host.http"}):
        started = time.monotonic()
        with contextlib.closing(_connect(proxy, _HTTP)) as sock:
            assert _recv(sock) == b"echo:" + _HTTP
        # One delay each way for the request and its echo.
        assert time.monotonic() - started >= 0.55


def test_sever_held_answers_504_but_spares_upgraded_streams(loop_thread: LoopThread) -> None:
    held_upstream = _upstream(loop_thread, "silent")
    held_proxy = _proxy(loop_thread, held_upstream)
    upgrade_upstream = _upstream(loop_thread, "upgrade")
    upgrade_proxy = _proxy(loop_thread, upgrade_upstream)
    tunnel = _connect(upgrade_proxy, _TUNNEL)
    assert _recv(tunnel).startswith(b"HTTP/1.1 101")
    with held_proxy.sever_held(0.3), upgrade_proxy.sever_held(0.3):
        held = _connect(held_proxy, _HTTP)
        assert _recv(held).startswith(b"HTTP/1.1 504 Gateway Timeout")
        tunnel.sendall(b"frame")
        time.sleep(0.6)
        assert _recv(tunnel) == b"echo:frame"
    held.close()
    tunnel.close()


def test_recycle_closes_connections_past_their_lifetime(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    with proxy.recycle(0.3, {"runner.tunnel"}):
        sock = _connect(proxy, _TUNNEL)
        assert _recv(sock) == b"echo:" + _TUNNEL
        _wait(lambda: not proxy.connections({"runner.tunnel"}))
        assert _recv(sock) == b""
    sock.close()


def test_flap_alternates_outages_until_cleared(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    fault = proxy.flap(up_s=0.2, down_s=0.2, mode="reset")
    _wait(lambda: len(proxy._events.events(kind="flap_down")) >= 2)
    fault.clear()
    with contextlib.closing(_connect(proxy, _HTTP)) as sock:
        assert _recv(sock) == b"echo:" + _HTTP
    assert proxy._events.events(kind="fault_end")


def test_wait_for_connection_detects_a_reconnect(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    first = _connect(proxy, _TUNNEL)
    assert _recv(first)
    before = time.time()
    proxy.reset({"runner.tunnel"})
    second = _connect(proxy, _TUNNEL)
    assert _recv(second)
    conn = proxy.wait_for_connection({"runner.tunnel"}, opened_after=before, timeout=2)
    assert conn.tag == "runner.tunnel"
    first.close()
    second.close()


def _tunnel(proxy: FaultProxy, ca: Path, host: str = "gateway.example.test") -> ssl.SSLSocket:
    """Open an HTTPS proxy tunnel through *proxy* and verify its certificate."""
    raw = _connect(proxy, f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    assert _recv(raw).startswith(b"HTTP/1.1 200")
    context = ssl.create_default_context(cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context.wrap_socket(raw, server_hostname=host)


def test_intercepts_https_tunnels_and_serves_only_model_paths(
    loop_thread: LoopThread, tmp_path: Path
) -> None:
    upstream = _upstream(loop_thread)
    tls = TlsInterceptor(tmp_path / "tls")
    proxy = _proxy(loop_thread, upstream, classify=fixed_link("model"), intercept=tls)

    model_call = b"POST /ai-gateway/anthropic/v1/messages?beta=true HTTP/1.1\r\nHost: g\r\n\r\n"
    with contextlib.closing(_tunnel(proxy, tls.ca_path)) as tunnel:
        tunnel.sendall(model_call)
        assert _recv(tunnel) == b"echo:" + model_call
    with contextlib.closing(_tunnel(proxy, tls.ca_path, "telemetry.example.test")) as tunnel:
        tunnel.sendall(b"POST /v1/traces HTTP/1.1\r\nHost: t\r\n\r\n")
        assert _recv(tunnel).startswith(b"HTTP/1.1 403 Forbidden")
    assert upstream.received == [model_call]


def test_connect_without_an_interceptor_is_forbidden(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    with contextlib.closing(
        _connect(proxy, b"CONNECT api.example.test:443 HTTP/1.1\r\n\r\n")
    ) as sock:
        assert _recv(sock).startswith(b"HTTP/1.1 403 Forbidden")
    assert upstream.received == []


def test_intercepted_tunnel_closes_when_its_upstream_closes(
    loop_thread: LoopThread, tmp_path: Path
) -> None:
    upstream = _upstream(loop_thread, "once")
    tls = TlsInterceptor(tmp_path / "tls")
    proxy = _proxy(loop_thread, upstream, classify=fixed_link("model"), intercept=tls)
    request = b"POST /v1/messages HTTP/1.1\r\nHost: g\r\n\r\n"
    with contextlib.closing(_tunnel(proxy, tls.ca_path)) as tunnel:
        tunnel.sendall(request)
        assert _recv(tunnel) == b"echo:" + request
        # A keep-alive upstream that times out must not leave the client a dead tunnel.
        assert _recv(tunnel) == b""


@pytest.mark.parametrize(
    "authority", ["../../escaped:443", "/tmp/escaped:443", "a/b:443", "no-port", "bad host:443"]
)
def test_rejects_connect_authorities_that_are_not_hostnames(
    loop_thread: LoopThread, tmp_path: Path, authority: str
) -> None:
    upstream = _upstream(loop_thread)
    tls = TlsInterceptor(tmp_path / "certs" / "tls")
    proxy = _proxy(loop_thread, upstream, classify=fixed_link("model"), intercept=tls)
    request = f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode()
    with contextlib.closing(_connect(proxy, request)) as sock:
        assert _recv(sock).startswith(b"HTTP/1.1 403 Forbidden")
    written = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    )
    assert written == ["certs/tls/lab-ca.pem"]


def test_flap_reset_leaves_listener_refusal_working(loop_thread: LoopThread) -> None:
    upstream = _upstream(loop_thread)
    proxy = _proxy(loop_thread, upstream)
    flap = proxy.flap(up_s=0.1, down_s=0.1, mode="reset")
    _wait(lambda: len(proxy._events.events(kind="flap_up")) >= 2)
    flap.clear()
    with proxy.refuse(), pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
