"""Parent-owned TCP broker for one sandbox's named destination grants."""

from __future__ import annotations

import contextlib
import errno
import hmac
import ipaddress
import json
import logging
import os
import secrets
import select
import socket
import socketserver
import struct
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from . import tcp_egress_wire
from .datamodel import TcpEgressRoute
from .tcp_egress_policy import is_blocked_tcp_address, normalize_tcp_egress

logger = logging.getLogger(__name__)

_SOCKET_NAME = ".tcp-egress.sock"
_MAX_REQUEST = 2048
_MAX_CONNECTIONS = 8
_MAX_BYTES = 2 * 1024 * 1024 * 1024
_SESSION_SECONDS = 1800
_IDLE_SECONDS = 300
_MAX_FRAME = tcp_egress_wire.MAX_FRAME
_DNS_SLOTS = threading.BoundedSemaphore(16)


class TcpEgressDenied(ValueError):
    """The request exceeds this sandbox's TCP route grants."""


def resolve_pinned_address(route: TcpEgressRoute) -> str:
    """Validate every address returned for the exact host before selecting one."""
    if not _DNS_SLOTS.acquire(blocking=False):
        raise TcpEgressDenied("TCP egress DNS resolver is busy")
    try:
        try:
            answers = socket.getaddrinfo(route.host, route.port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise TcpEgressDenied("TCP egress destination could not be resolved") from exc
    finally:
        _DNS_SLOTS.release()
    networks = tuple(ipaddress.ip_network(value) for value in route.allowed_cidrs)
    chosen: str | None = None
    for family, _, _, _, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = ipaddress.ip_address(cast(str, sockaddr[0]).split("%")[0])
        if is_blocked_tcp_address(address):
            raise TcpEgressDenied("TCP egress destination resolved to a blocked address")
        if networks:
            allowed = any(address in network for network in networks)
        else:
            allowed = address.is_global
        if not allowed:
            raise TcpEgressDenied("TCP egress destination resolved outside allowed addresses")
        if chosen is None:
            chosen = str(address)
    if chosen is None:
        raise TcpEgressDenied("TCP egress destination has no usable address")
    return chosen


def _read_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise TcpEgressDenied("TCP egress request ended early")
        data.extend(chunk)
    return bytes(data)


def _read_frame(sock: socket.socket) -> tuple[bytes, bytes]:
    header = _read_exact(sock, 5)
    length = struct.unpack("!I", header[1:])[0]
    if length > _MAX_FRAME:
        raise TcpEgressDenied("TCP egress frame exceeds size limit")
    return header[:1], _read_exact(sock, length)


def _write_frame(sock: socket.socket, kind: bytes, data: bytes) -> None:
    sock.sendall(kind + struct.pack("!I", len(data)) + data)


class _TcpServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    broker: TcpEgressBroker

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.broker._slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.settimeout(1)
                request.sendall(b"\x01TCP egress connection limit reached")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.broker._slots.release()
            raise


class _TcpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        broker = cast(_TcpServer, self.server).broker
        client = cast(socket.socket, self.request)
        with broker._condition:
            if broker._closed:
                broker._slots.release()
                return
            broker._connections.add(client)
        route_name = "unknown"
        transferred = [0]
        accepted = False
        try:
            client.settimeout(10)
            length = struct.unpack("!I", _read_exact(client, 4))[0]
            if not 1 <= length <= _MAX_REQUEST:
                raise TcpEgressDenied("TCP egress request size is invalid")
            request = json.loads(_read_exact(client, length))
            if not isinstance(request, dict) or set(request) != {"token", "route"}:
                raise TcpEgressDenied("TCP egress request must name only one route")
            token = request.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(token, broker.token):
                raise TcpEgressDenied("TCP egress capability is invalid")
            requested_route = request.get("route")
            if not isinstance(requested_route, str) or requested_route not in broker.routes:
                raise TcpEgressDenied("TCP egress route is not allowed")
            route_name = requested_route
            route = broker.routes[route_name]
            pinned_ip = resolve_pinned_address(route)
            upstream = broker._dial_pinned(pinned_ip, route.port)
            with upstream:
                try:
                    client.sendall(b"\x00")
                    accepted = True
                    client.settimeout(10)
                    upstream.settimeout(10)
                    broker._relay(client, upstream, transferred)
                    _write_frame(client, b"X", struct.pack("!i", 0))
                finally:
                    with broker._condition:
                        broker._connections.discard(upstream)
                        broker._condition.notify_all()
        except (TcpEgressDenied, ValueError) as exc:
            decision = "terminated" if accepted else "denied"
            logger.info("TCP egress route %s %s: %s", route_name, decision, exc)
            with contextlib.suppress(OSError):
                if accepted:
                    _write_frame(client, b"D", str(exc).encode()[:1024])
                else:
                    client.sendall(b"\x01" + str(exc).encode()[:1024])
        except OSError:
            logger.exception("TCP egress connection failed for route %s", route_name)
            with contextlib.suppress(OSError):
                if accepted:
                    _write_frame(client, b"D", b"TCP egress connection failed")
                else:
                    client.sendall(b"\x01TCP egress connection failed")
        finally:
            logger.info("TCP egress route %s closed after %s bytes", route_name, transferred[0])
            broker._slots.release()
            with broker._condition:
                broker._connections.discard(client)
                broker._condition.notify_all()


class TcpEgressBroker:
    """Per-sandbox authenticated Unix-socket broker for opaque TCP streams."""

    def __init__(self, routes: Sequence[TcpEgressRoute], socket_path: Path) -> None:
        if sys.platform == "win32":
            raise TcpEgressDenied("TCP egress requires a Unix sandbox backend")
        self.routes = {route.name: route for route in normalize_tcp_egress(routes)}
        self.socket_path = socket_path
        self.token = secrets.token_urlsafe(32)
        self._condition = threading.Condition()
        self._connections: set[socket.socket] = set()
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._closed = False
        self._server = _TcpServer(str(socket_path), _TcpHandler)
        self._server.broker = self
        os.chmod(socket_path, 0o600)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _dial_pinned(self, pinned_ip: str, port: int) -> socket.socket:
        family = (
            socket.AF_INET6 if ipaddress.ip_address(pinned_ip).version == 6 else socket.AF_INET
        )
        upstream = socket.socket(family, socket.SOCK_STREAM)
        with self._condition:
            if self._closed:
                upstream.close()
                raise TcpEgressDenied("TCP egress broker is closed")
            self._connections.add(upstream)
        try:
            upstream.setblocking(False)
            error = upstream.connect_ex((pinned_ip, port))
            if error not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                raise OSError(error, "TCP egress dial failed")
            deadline = time.monotonic() + 10
            while error:
                if self._closed:
                    raise TcpEgressDenied("TCP egress broker is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TCP egress dial timed out")
                if select.select([], [upstream], [], min(remaining, 0.2))[1]:
                    error = upstream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if error:
                        raise OSError(error, "TCP egress dial failed")
            upstream.settimeout(10)
            return upstream
        except BaseException:
            with self._condition:
                self._connections.discard(upstream)
                self._condition.notify_all()
            upstream.close()
            raise

    def _relay(
        self, client: socket.socket, upstream: socket.socket, transferred: list[int]
    ) -> None:
        started = time.monotonic()
        last_activity = [started]
        failure: list[str | None] = [None]
        quota_lock = threading.Lock()
        input_done = threading.Event()

        def account(length: int) -> None:
            with quota_lock:
                transferred[0] += length
                last_activity[0] = time.monotonic()
                if transferred[0] > _MAX_BYTES:
                    raise TcpEgressDenied("TCP egress transfer limit exceeded")

        def pump_input() -> None:
            try:
                while True:
                    if self._closed:
                        raise TcpEgressDenied("TCP egress broker is closed")
                    if not select.select([client], [], [], 1)[0]:
                        continue
                    kind, data = _read_frame(client)
                    if kind == b"Z" and not data:
                        upstream.shutdown(socket.SHUT_WR)
                        return
                    if kind != b"I" or not data:
                        raise TcpEgressDenied("TCP egress input frame is invalid")
                    account(len(data))
                    upstream.sendall(data)
            except (OSError, TcpEgressDenied) as exc:
                failure[0] = str(exc)
                with contextlib.suppress(OSError):
                    upstream.shutdown(socket.SHUT_RDWR)
            finally:
                input_done.set()

        reader = threading.Thread(target=pump_input, daemon=True)
        reader.start()
        try:
            while True:
                if self._closed:
                    raise TcpEgressDenied("TCP egress broker is closed")
                if failure[0] is not None:
                    raise TcpEgressDenied(failure[0])
                now = time.monotonic()
                if now - started > _SESSION_SECONDS or now - last_activity[0] > _IDLE_SECONDS:
                    raise TcpEgressDenied("TCP egress connection timed out")
                try:
                    data = upstream.recv(_MAX_FRAME)
                except TimeoutError:
                    continue
                if not data:
                    _write_frame(client, b"Z", b"")
                    break
                account(len(data))
                _write_frame(client, b"O", data)
            while not input_done.wait(timeout=1):
                if self._closed:
                    raise TcpEgressDenied("TCP egress broker is closed")
                if failure[0] is not None:
                    raise TcpEgressDenied(failure[0])
                now = time.monotonic()
                if now - started > _SESSION_SECONDS or now - last_activity[0] > _IDLE_SECONDS:
                    raise TcpEgressDenied("TCP egress connection timed out")
            if failure[0] is not None:
                raise TcpEgressDenied(failure[0])
        finally:
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RD)
            reader.join(timeout=2)

    def stop(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._connections)
        for sock in active:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        self._server.shutdown()
        self._server.server_close()
        with contextlib.suppress(OSError):
            self.socket_path.unlink(missing_ok=True)
        self._thread.join(timeout=3)
        with self._condition:
            if not self._condition.wait_for(lambda: not self._connections, timeout=5):
                logger.error("TCP egress broker handlers did not stop")


def start_tcp_egress_broker(routes: Sequence[TcpEgressRoute], tmpdir: Path) -> TcpEgressBroker:
    return TcpEgressBroker(routes, tmpdir / _SOCKET_NAME)


def apply_tcp_egress_env(env: dict[str, str], broker: TcpEgressBroker) -> None:
    """Install a standalone connector in the sandbox scratch directory."""
    helper_path = broker.socket_path.parent / "omnigent-egress-connect"
    source = Path(tcp_egress_wire.__file__).read_bytes()
    helper_path.write_bytes(f"#!{sys.executable} -I\n".encode() + source)
    os.chmod(helper_path, 0o700)
    env[tcp_egress_wire.SOCKET_ENV] = str(broker.socket_path)
    env[tcp_egress_wire.TOKEN_ENV] = broker.token
    env[tcp_egress_wire.CONNECT_ENV] = str(helper_path)
    env["PATH"] = f"{helper_path.parent}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
