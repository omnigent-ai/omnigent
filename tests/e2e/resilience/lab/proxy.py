"""Fault-injecting TCP proxy for network-interruption scenarios.

Each :class:`FaultProxy` forwards one listener to one upstream and tags every
accepted connection by its first HTTP request line (for example
``runner.tunnel`` or ``client.sse``), so a fault can target one logical link
while others keep flowing. Faults model what a real network does to TCP:

- ``blackhole``: bytes and closes stop flowing in both directions but both
  sockets stay open, like a half-open connection; held bytes flush when the
  fault clears, as TCP retransmission would deliver them.
- ``refuse``: new connections fail. Without tags the listener stops, so the
  kernel refuses; with tags matching connections are reset after accept.
- ``delay`` / ``throttle``: latency per chunk and a bandwidth cap.
- ``reset`` / ``close``: RST or FIN every matching live connection now.
- ``recycle``: periodically FIN connections older than a lifetime, like an
  ingress recycling long-lived streams.
- ``sever_held``: cut requests that waited too long for a response, like a
  front door with a request cap (504 before any response bytes).
- ``flap``: alternate a blackhole (or reset + refuse) with healthy periods.

With a :class:`~tests.e2e.resilience.lab.tls.TlsInterceptor`, ``CONNECT``
tunnels (a harness honoring ``HTTPS_PROXY``) are terminated locally and only
model API requests are forwarded, so faults apply to HTTPS model traffic too.

An upstream that refuses connections is reported to the client either as a
reset (``upstream_down="reset"``, like a directly reachable server) or as an
HTTP 502 (``upstream_down="gateway"``, like a load balancer in front of it).

Fidelity limits: a blackholed new connection is accepted rather than left in
SYN-SENT, so clients see a read or write timeout instead of a connect timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import re
import socket
import struct
import threading
import time
from collections.abc import Callable, Coroutine, Iterable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.tls import TlsInterceptor, valid_hostname

T = TypeVar("T")

#: Maps an HTTP request's method and path to a link tag, e.g. ``"runner.tunnel"``.
LinkClassifier = Callable[[str, str], str]

UpstreamDown = Literal["reset", "gateway"]

_CHUNK = 64 * 1024
_FIRST_LINE_LIMIT = 16 * 1024
_FIRST_LINE_TIMEOUT_S = 10.0
_QUEUE_CHUNKS = 64
_WATCH_INTERVAL_S = 0.1
_BAD_GATEWAY = (
    b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n"
    b"Content-Length: 11\r\nConnection: close\r\n\r\nBad Gateway"
)
_GATEWAY_TIMEOUT = (
    b"HTTP/1.1 504 Gateway Timeout\r\nContent-Type: text/plain\r\n"
    b"Content-Length: 15\r\nConnection: close\r\n\r\nGateway Timeout"
)
_FORBIDDEN = (
    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
    b"Content-Length: 9\r\nConnection: close\r\n\r\nForbidden"
)
_MODEL_API_PATH = re.compile(r"/v1/(messages|models|responses|chat/completions)(/|$)")
_RUNNER_TUNNEL = re.compile(r"^/v1/runners/[^/]+/tunnel$")
_HOST_TUNNEL = re.compile(r"^/v1/hosts/[^/]+/tunnel$")
_TERMINAL_ATTACH = re.compile(r"^/v1/sessions/[^/]+/resources/terminals/[^/]+/attach$")


def client_link(method: str, path: str) -> str:
    """Tag a browser or API-client connection.

    :param method: HTTP method, e.g. ``"GET"``.
    :param path: Request path without query, e.g. ``"/v1/sessions/conv_1/stream"``.
    :returns: ``client.ws``, ``client.sse`` or ``client.http``.
    """
    if path == "/v1/sessions/updates" or _TERMINAL_ATTACH.match(path):
        return "client.ws"
    if method == "GET" and path.endswith("/stream"):
        return "client.sse"
    return "client.http"


def host_link(method: str, path: str) -> str:
    """Tag a connection from the host daemon, runner, or harness hooks.

    :param method: HTTP method, e.g. ``"GET"``.
    :param path: Request path without query, e.g. ``"/v1/runners/r1/tunnel"``.
    :returns: ``host.tunnel``, ``runner.tunnel`` or ``host.http``.
    """
    del method
    if _HOST_TUNNEL.match(path):
        return "host.tunnel"
    if _RUNNER_TUNNEL.match(path):
        return "runner.tunnel"
    return "host.http"


def fixed_link(tag: str) -> LinkClassifier:
    """Tag every connection with *tag*, e.g. ``"model"``.

    :param tag: The tag to return.
    :returns: A classifier ignoring the request.
    """
    return lambda _method, _path: tag


def tag_matches(tag: str, tags: Iterable[str] | None) -> bool:
    """Whether *tag* is selected by *tags*.

    ``None`` selects everything; ``"client"`` selects ``"client.sse"``.

    :param tag: A connection tag, e.g. ``"client.sse"``.
    :param tags: Selectors, e.g. ``{"client"}``, or ``None``.
    :returns: Whether the tag is selected.
    """
    if tags is None:
        return True
    return any(tag == wanted or tag.startswith(f"{wanted}.") for wanted in tags)


class LoopThread:
    """Own one asyncio loop on a daemon thread for synchronous callers."""

    def __init__(self, name: str = "resilience-lab") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The running loop; :meth:`start` must have been called."""
        if self._loop is None:
            raise RuntimeError("loop thread not started")
        return self._loop

    def start(self) -> None:
        """Start the loop thread and wait until it is running."""
        if self._thread is not None:
            return

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            loop.call_soon(self._ready.set)
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._thread = threading.Thread(target=_run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("lab loop thread did not start")

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """Schedule *coro* on the loop without waiting.

        :param coro: Coroutine to run.
        :returns: A future resolving to its result.
        """
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
        """Run *coro* on the loop and wait for its result.

        :param coro: Coroutine to run.
        :param timeout: Seconds to wait, e.g. ``30.0``.
        :returns: The coroutine's result.
        """
        return self.submit(coro).result(timeout=timeout)

    def stop(self) -> None:
        """Stop the loop and join its thread."""
        if self._loop is None or self._thread is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._thread = None
        self._loop = None


@dataclass(frozen=True)
class ConnectionInfo:
    """Snapshot of one proxied connection.

    :param id: Proxy-local connection number, e.g. ``7``.
    :param proxy: Proxy name, e.g. ``"host"``.
    :param tag: Link tag, e.g. ``"runner.tunnel"``.
    :param request_line: First request line, e.g. ``"GET /v1/runners/r1/tunnel HTTP/1.1"``.
    :param opened_wall: Wall-clock open time.
    :param age_s: Seconds since the connection opened.
    :param c2s_bytes: Bytes forwarded client to upstream.
    :param s2c_bytes: Bytes forwarded upstream to client.
    :param upgraded: Whether the upstream answered ``101 Switching Protocols``.
    """

    id: int
    proxy: str
    tag: str
    request_line: str
    opened_wall: float
    age_s: float
    c2s_bytes: int
    s2c_bytes: int
    upgraded: bool


@dataclass(eq=False)
class _Connection:
    id: int
    client_writer: asyncio.StreamWriter
    opened_mono: float = field(default_factory=time.monotonic)
    opened_wall: float = field(default_factory=time.time)
    tag: str = "unclassified"
    request_line: str = ""
    upstream_writer: asyncio.StreamWriter | None = None
    c2s_bytes: int = 0
    s2c_bytes: int = 0
    last_c2s_mono: float = 0.0
    last_s2c_mono: float = 0.0
    upgraded: bool = False
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    end_reason: str | None = None

    def info(self, proxy: str) -> ConnectionInfo:
        return ConnectionInfo(
            id=self.id,
            proxy=proxy,
            tag=self.tag,
            request_line=self.request_line,
            opened_wall=self.opened_wall,
            age_s=time.monotonic() - self.opened_mono,
            c2s_bytes=self.c2s_bytes,
            s2c_bytes=self.s2c_bytes,
            upgraded=self.upgraded,
        )

    def awaiting_response(self) -> bool:
        return not self.upgraded and self.last_c2s_mono > self.last_s2c_mono


@dataclass(frozen=True)
class _Rule:
    id: int
    kind: Literal["blackhole", "refuse", "delay", "throttle"]
    tags: frozenset[str] | None
    value: float = 0.0


class Fault:
    """Handle for an active fault. Clear it, or use it as a context manager.

    :param proxy: Proxy that owns the fault.
    :param description: Human-readable summary, e.g. ``"blackhole runner.tunnel"``.
    """

    def __init__(self, proxy: FaultProxy, description: str) -> None:
        self._proxy = proxy
        self.description = description
        self._rules: list[int] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._cleared = False

    @property
    def active(self) -> bool:
        """Whether the fault is still applied."""
        return not self._cleared

    def clear(self) -> None:
        """Remove the fault; safe to call more than once."""
        if self._cleared:
            return
        self._cleared = True
        with contextlib.suppress(RuntimeError):  # the lab already stopped
            self._proxy._run(self._proxy._clear_fault(self))

    def __enter__(self) -> Fault:
        return self

    def __exit__(self, *exc: object) -> None:
        self.clear()


class FaultProxy:
    """Transparent TCP proxy with scriptable network faults.

    Control methods are synchronous and thread-safe; the proxy itself runs on
    *loop*.

    :param name: Proxy name used in tags and events, e.g. ``"host"``.
    :param upstream: ``(host, port)`` to forward to.
    :param loop: Loop thread the proxy runs on.
    :param events: Event log for connections and faults.
    :param classify: Maps the first request line to a tag. Defaults to *name*.
    :param upstream_down: How an unreachable upstream looks to the client.
    :param listen_port: Fixed port to listen on, or ``0`` for any free port.
    :param intercept: When set, ``CONNECT`` tunnels are terminated with a lab
        certificate and their model API requests forwarded to *upstream*;
        without it ``CONNECT`` is refused with ``403``.
    """

    def __init__(
        self,
        name: str,
        upstream: tuple[str, int],
        *,
        loop: LoopThread,
        events: EventLog,
        classify: LinkClassifier | None = None,
        upstream_down: UpstreamDown = "reset",
        listen_port: int = 0,
        intercept: TlsInterceptor | None = None,
    ) -> None:
        self.name = name
        self.upstream = upstream
        self.upstream_down: UpstreamDown = upstream_down
        self._loop = loop
        self._events = events
        self._classify = classify or fixed_link(name)
        self._listen_port = listen_port
        self._intercept = intercept
        self._server: asyncio.base_events.Server | None = None
        self._connections: dict[int, _Connection] = {}
        self._rules: dict[int, _Rule] = {}
        self._ids = itertools.count(1)
        self._changed: asyncio.Event | None = None
        self._listener_refusals = 0

    # ── lifecycle ────────────────────────────────────────────────

    @property
    def port(self) -> int:
        """The bound listen port."""
        if not self._listen_port:
            raise RuntimeError(f"proxy {self.name} not started")
        return self._listen_port

    @property
    def url(self) -> str:
        """``http://127.0.0.1:<port>`` for clients."""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        """Bind the listener and begin forwarding."""
        self._run(self._start())

    def stop(self) -> None:
        """Close the listener and every live connection."""
        self._run(self._stop())

    # ── faults ───────────────────────────────────────────────────

    def blackhole(self, tags: Iterable[str] | None = None) -> Fault:
        """Hold all bytes and closes on matching connections until cleared.

        :param tags: Link selectors, e.g. ``{"runner.tunnel"}``; ``None`` for all.
        :returns: The active fault.
        """
        return self._run(self._add_rules("blackhole", tags))

    def refuse(self, tags: Iterable[str] | None = None) -> Fault:
        """Refuse new connections until cleared; live ones are untouched.

        :param tags: Link selectors, or ``None`` to stop listening entirely.
        :returns: The active fault.
        """
        return self._run(self._add_rules("refuse", tags))

    def delay(self, seconds: float, tags: Iterable[str] | None = None) -> Fault:
        """Add one-way latency to every forwarded chunk.

        :param seconds: Added latency, e.g. ``0.25``.
        :param tags: Link selectors, or ``None`` for all.
        :returns: The active fault.
        """
        return self._run(self._add_rules("delay", tags, seconds))

    def throttle(self, bytes_per_s: float, tags: Iterable[str] | None = None) -> Fault:
        """Cap forwarding bandwidth per connection direction.

        :param bytes_per_s: Bandwidth, e.g. ``4096``.
        :param tags: Link selectors, or ``None`` for all.
        :returns: The active fault.
        """
        if bytes_per_s <= 0:
            raise ValueError("bytes_per_s must be positive")
        return self._run(self._add_rules("throttle", tags, bytes_per_s))

    def reset(self, tags: Iterable[str] | None = None) -> int:
        """RST every matching live connection now.

        :param tags: Link selectors, or ``None`` for all.
        :returns: How many connections were reset.
        """
        return self._run(self._end_matching(tags, rst=True, reason="reset"))

    def close(self, tags: Iterable[str] | None = None) -> int:
        """FIN every matching live connection now.

        :param tags: Link selectors, or ``None`` for all.
        :returns: How many connections were closed.
        """
        return self._run(self._end_matching(tags, rst=False, reason="close"))

    def recycle(self, lifetime_s: float, tags: Iterable[str] | None = None) -> Fault:
        """Close matching connections once they live longer than *lifetime_s*.

        :param lifetime_s: Maximum connection age, e.g. ``300.0``.
        :param tags: Link selectors, or ``None`` for all.
        :returns: The active fault.
        """
        return self._run(self._add_watch("recycle", tags, lifetime_s))

    def sever_held(self, after_s: float, tags: Iterable[str] | None = None) -> Fault:
        """Cut non-upgraded requests left waiting for a response past *after_s*.

        The client gets ``504 Gateway Timeout`` and the connection closes.

        :param after_s: Request cap, e.g. ``300.0``.
        :param tags: Link selectors, or ``None`` for all.
        :returns: The active fault.
        """
        return self._run(self._add_watch("sever_held", tags, after_s))

    def flap(
        self,
        *,
        up_s: float,
        down_s: float,
        tags: Iterable[str] | None = None,
        mode: Literal["blackhole", "reset"] = "blackhole",
    ) -> Fault:
        """Alternate *down_s* outages with *up_s* healthy periods, starting down.

        :param up_s: Healthy period, e.g. ``5.0``.
        :param down_s: Outage period, e.g. ``2.0``.
        :param tags: Link selectors, or ``None`` for all.
        :param mode: ``blackhole`` holds traffic; ``reset`` RSTs and refuses.
        :returns: The active fault.
        """
        return self._run(self._add_flap(up_s, down_s, tags, mode))

    # ── inspection ───────────────────────────────────────────────

    def connections(self, tags: Iterable[str] | None = None) -> list[ConnectionInfo]:
        """Snapshot live connections.

        :param tags: Link selectors, or ``None`` for all.
        :returns: Matching connections, oldest first.
        """
        return self._run(self._snapshot(tags))

    def wait_for_connection(
        self,
        tags: Iterable[str],
        *,
        opened_after: float,
        timeout: float = 60.0,
        min_s2c_bytes: int = 1,
    ) -> ConnectionInfo:
        """Wait for a matching connection opened after a wall-clock time.

        :param tags: Link selectors, e.g. ``{"runner.tunnel"}``.
        :param opened_after: Wall-clock threshold, e.g. ``time.time()`` before a fault.
        :param timeout: Seconds to wait.
        :param min_s2c_bytes: Upstream bytes required, so a refused attempt
            does not count as a reconnect.
        :returns: The first matching connection.
        :raises TimeoutError: If none appears in time.
        """
        selectors = frozenset(tags)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for conn in self.connections(selectors):
                if conn.opened_wall > opened_after and conn.s2c_bytes >= min_s2c_bytes:
                    return conn
            time.sleep(_WATCH_INTERVAL_S)
        raise TimeoutError(
            f"no {sorted(selectors)} connection through proxy {self.name} within {timeout}s"
        )

    # ── loop-side implementation ─────────────────────────────────

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        return self._loop.run(coro)

    def _emit(self, kind: str, **fields: Any) -> None:
        self._events.emit(f"proxy:{self.name}", kind, **fields)

    def _signal_change(self) -> None:
        if self._changed is not None:
            self._changed.set()
        self._changed = asyncio.Event()

    async def _start(self) -> None:
        self._changed = asyncio.Event()
        await self._listen()
        self._emit(
            "listen", port=self._listen_port, upstream=f"{self.upstream[0]}:{self.upstream[1]}"
        )

    async def _listen(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._listen_port, reuse_address=True
        )
        sock = self._server.sockets[0]
        self._listen_port = int(sock.getsockname()[1])

    async def _unlisten(self) -> None:
        # Close only the listening socket; accepted connections stay alive.
        server, self._server = self._server, None
        if server is not None:
            server.close()

    async def _stop(self) -> None:
        for rule_id in list(self._rules):
            self._rules.pop(rule_id, None)
        await self._unlisten()
        await self._end_matching(None, rst=False, reason="proxy_stop")
        self._signal_change()

    async def _add_rules(
        self,
        kind: Literal["blackhole", "refuse", "delay", "throttle"],
        tags: Iterable[str] | None,
        value: float = 0.0,
    ) -> Fault:
        selectors = None if tags is None else frozenset(tags)
        rule = _Rule(next(self._ids), kind, selectors, value)
        description = f"{kind} {self._describe(selectors)}" + (f" {value}" if value else "")
        fault = Fault(self, description)
        fault._rules.append(rule.id)
        await self._install_rule(rule)
        self._emit("fault_start", fault=description)
        return fault

    async def _install_rule(self, rule: _Rule) -> None:
        """Activate *rule*; an untagged refusal closes the listener (undone by ``_drop_rule``)."""
        self._rules[rule.id] = rule
        if rule.kind == "refuse" and rule.tags is None:
            self._listener_refusals += 1
            if self._listener_refusals == 1:
                await self._unlisten()
        self._signal_change()

    async def _add_watch(
        self, kind: Literal["recycle", "sever_held"], tags: Iterable[str] | None, limit_s: float
    ) -> Fault:
        selectors = None if tags is None else frozenset(tags)
        description = f"{kind} {self._describe(selectors)} {limit_s}"
        fault = Fault(self, description)
        fault._tasks.append(asyncio.create_task(self._watch(kind, selectors, limit_s)))
        self._emit("fault_start", fault=description)
        return fault

    async def _add_flap(
        self,
        up_s: float,
        down_s: float,
        tags: Iterable[str] | None,
        mode: Literal["blackhole", "reset"],
    ) -> Fault:
        selectors = None if tags is None else frozenset(tags)
        description = f"flap {mode} {self._describe(selectors)} up={up_s} down={down_s}"
        fault = Fault(self, description)
        fault._tasks.append(asyncio.create_task(self._flap(fault, selectors, up_s, down_s, mode)))
        self._emit("fault_start", fault=description)
        return fault

    async def _clear_fault(self, fault: Fault) -> None:
        for task in fault._tasks:
            task.cancel()
        for task in fault._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for rule_id in fault._rules:
            await self._drop_rule(rule_id)
        fault._rules.clear()
        self._emit("fault_end", fault=fault.description)

    async def _drop_rule(self, rule_id: int) -> None:
        rule = self._rules.pop(rule_id, None)
        if rule is None:
            return
        if rule.kind == "refuse" and rule.tags is None:
            self._listener_refusals -= 1
            if self._listener_refusals == 0 and self._server is None:
                await self._listen()
        self._signal_change()

    async def _watch(
        self, kind: Literal["recycle", "sever_held"], tags: frozenset[str] | None, limit_s: float
    ) -> None:
        interval = max(0.05, min(1.0, limit_s / 4))
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for conn in list(self._connections.values()):
                if conn.ended.is_set() or not tag_matches(conn.tag, tags):
                    continue
                if kind == "recycle" and now - conn.opened_mono >= limit_s:
                    self._end(conn, rst=False, reason="recycle")
                elif (
                    kind == "sever_held"
                    and conn.awaiting_response()
                    and now - conn.last_c2s_mono >= limit_s
                ):
                    with contextlib.suppress(Exception):
                        conn.client_writer.write(_GATEWAY_TIMEOUT)
                    self._end(conn, rst=False, reason="sever_held")

    async def _flap(
        self,
        fault: Fault,
        tags: frozenset[str] | None,
        up_s: float,
        down_s: float,
        mode: Literal["blackhole", "reset"],
    ) -> None:
        kind: Literal["blackhole", "refuse"] = "blackhole" if mode == "blackhole" else "refuse"
        while True:
            rule = _Rule(next(self._ids), kind, tags)
            await self._install_rule(rule)
            fault._rules.append(rule.id)
            if mode == "reset":
                await self._end_matching(tags, rst=True, reason="flap")
            self._emit("flap_down", fault=fault.description)
            try:
                await asyncio.sleep(down_s)
            finally:
                fault._rules.remove(rule.id)
                await self._drop_rule(rule.id)
            self._emit("flap_up", fault=fault.description)
            await asyncio.sleep(up_s)

    def _describe(self, tags: frozenset[str] | None) -> str:
        return "*" if tags is None else ",".join(sorted(tags))

    def _active(self, kind: str, tag: str) -> list[_Rule]:
        return [r for r in self._rules.values() if r.kind == kind and tag_matches(tag, r.tags)]

    async def _snapshot(self, tags: Iterable[str] | None) -> list[ConnectionInfo]:
        selectors = None if tags is None else frozenset(tags)
        return [
            conn.info(self.name)
            for conn in self._connections.values()
            if not conn.ended.is_set() and tag_matches(conn.tag, selectors)
        ]

    async def _end_matching(self, tags: Iterable[str] | None, *, rst: bool, reason: str) -> int:
        selectors = None if tags is None else frozenset(tags)
        ended = 0
        for conn in list(self._connections.values()):
            if not conn.ended.is_set() and tag_matches(conn.tag, selectors):
                self._end(conn, rst=rst, reason=reason)
                ended += 1
        self._emit(reason, tags=self._describe(selectors), connections=ended)
        return ended

    def _end(self, conn: _Connection, *, rst: bool, reason: str) -> None:
        if conn.ended.is_set():
            return
        conn.end_reason = reason
        conn.ended.set()
        for writer in (conn.client_writer, conn.upstream_writer):
            if writer is None:
                continue
            if rst:
                _abort_with_rst(writer)
            else:
                with contextlib.suppress(Exception):
                    writer.close()

    async def _wait_open(self, conn: _Connection) -> None:
        """Block while a blackhole covers *conn*; return early if it ends."""
        while self._active("blackhole", conn.tag) and not conn.ended.is_set():
            changed = self._changed
            assert changed is not None
            ended = asyncio.ensure_future(conn.ended.wait())
            change = asyncio.ensure_future(changed.wait())
            try:
                await asyncio.wait({ended, change}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                ended.cancel()
                change.cancel()

    async def _read_request_line(self, reader: asyncio.StreamReader) -> bytes:
        buffer = b""
        deadline = time.monotonic() + _FIRST_LINE_TIMEOUT_S
        while b"\r\n" not in buffer and len(buffer) < _FIRST_LINE_LIMIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(_CHUNK), timeout=remaining)
            except TimeoutError:
                break
            if not chunk:
                break
            buffer += chunk
        return buffer

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        conn = _Connection(next(self._ids), client_writer)
        self._connections[conn.id] = conn
        try:
            await self._serve(conn, client_reader)
        except (ConnectionError, OSError):
            pass
        finally:
            self._end(conn, rst=False, reason=conn.end_reason or "eof")
            self._connections.pop(conn.id, None)
            self._emit(
                "disconnect",
                conn=conn.id,
                tag=conn.tag,
                reason=conn.end_reason,
                age_s=round(time.monotonic() - conn.opened_mono, 3),
                c2s_bytes=conn.c2s_bytes,
                s2c_bytes=conn.s2c_bytes,
            )

    async def _serve(self, conn: _Connection, client_reader: asyncio.StreamReader) -> None:
        first = await self._read_request_line(client_reader)
        first_read_at = time.monotonic()
        method, target, line = _request_line(first)
        conn.request_line = line
        if method:
            conn.tag = self._classify(method, target.split("?", 1)[0])
        self._emit("connect", conn=conn.id, tag=conn.tag, request=line[:200])
        if self._active("refuse", conn.tag):
            conn.end_reason = "refused"
            self._end(conn, rst=True, reason="refused")
            return
        await self._wait_open(conn)
        if conn.ended.is_set():
            return
        if method == "CONNECT":
            first = await self._intercept_connect(conn, client_reader, target)
            if first is None:
                return
            first_read_at = time.monotonic()
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self.upstream)
        except OSError as exc:
            self._emit(
                "upstream_unreachable", conn=conn.id, tag=conn.tag, error=type(exc).__name__
            )
            if self.upstream_down == "gateway":
                conn.client_writer.write(_BAD_GATEWAY)
                with contextlib.suppress(Exception):
                    await conn.client_writer.drain()
                self._end(conn, rst=False, reason="upstream_unreachable")
            else:
                self._end(conn, rst=True, reason="upstream_unreachable")
            return
        conn.upstream_writer = upstream_writer
        if conn.ended.is_set():
            _abort_with_rst(upstream_writer)
            return
        if first:
            delays = self._active("delay", conn.tag)
            if delays:
                wait = first_read_at + max(r.value for r in delays) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            upstream_writer.write(first)
            conn.c2s_bytes += len(first)
            conn.last_c2s_mono = time.monotonic()
        await asyncio.gather(
            self._pump(conn, client_reader, upstream_writer, upstream=True),
            self._pump(conn, upstream_reader, conn.client_writer, upstream=False),
        )

    async def _intercept_connect(
        self, conn: _Connection, client_reader: asyncio.StreamReader, target: str
    ) -> bytes | None:
        """Terminate an HTTPS proxy tunnel locally and return the inner first request.

        Only model API paths are served; anything else gets ``403`` so no
        intercepted request (telemetry, auth, updates) reaches a real service.
        """
        writer = conn.client_writer
        if self._intercept is None:
            writer.write(_FORBIDDEN)
            self._end(conn, rst=False, reason="connect_forbidden")
            return None
        host, _, port = target.rpartition(":")
        if not port.isdigit() or not valid_hostname(host):
            self._emit("intercept", conn=conn.id, host=target[:200], allowed=False)
            writer.write(_FORBIDDEN)
            self._end(conn, rst=False, reason="connect_forbidden")
            return None
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await writer.start_tls(self._intercept.server_context(host))
        first = await self._read_request_line(client_reader)
        method, inner_target, line = _request_line(first)
        conn.request_line = f"{line} (via CONNECT {host})"
        path = inner_target.split("?", 1)[0]
        allowed = bool(method) and _MODEL_API_PATH.search(path) is not None
        self._emit("intercept", conn=conn.id, host=host, request=line[:200], allowed=allowed)
        if not allowed:
            writer.write(_FORBIDDEN)
            self._end(conn, rst=False, reason="intercept_forbidden")
            return None
        return first

    async def _pump(
        self,
        conn: _Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        upstream: bool,
    ) -> None:
        """Forward one direction through a latency queue and the blackhole gate."""
        queue: asyncio.Queue[tuple[float, bytes | None]] = asyncio.Queue(_QUEUE_CHUNKS)

        async def _read() -> None:
            try:
                while True:
                    data = await reader.read(_CHUNK)
                    await queue.put((time.monotonic(), data))
                    if not data:
                        return
            except (ConnectionError, OSError):
                await queue.put((time.monotonic(), None))

        async def _write() -> None:
            while True:
                read_at, data = await queue.get()
                await self._wait_open(conn)
                if conn.ended.is_set():
                    return
                delays = self._active("delay", conn.tag)
                if delays:
                    wait = read_at + max(r.value for r in delays) - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    await self._wait_open(conn)
                    if conn.ended.is_set():
                        return
                if data is None:
                    self._end(conn, rst=True, reason="peer_reset")
                    return
                if not data:
                    # Propagate a half-close; a TLS-intercepted client cannot
                    # half-close, so end the whole connection instead of leaving
                    # it open with no upstream behind it.
                    if writer.can_write_eof():
                        with contextlib.suppress(Exception):
                            writer.write_eof()
                    else:
                        self._end(conn, rst=False, reason="eof")
                    return
                throttles = self._active("throttle", conn.tag)
                if throttles:
                    await asyncio.sleep(len(data) / min(r.value for r in throttles))
                if not upstream and conn.s2c_bytes == 0 and data.startswith(b"HTTP/1.1 101"):
                    conn.upgraded = True
                writer.write(data)
                now = time.monotonic()
                if upstream:
                    conn.c2s_bytes += len(data)
                    conn.last_c2s_mono = now
                else:
                    conn.s2c_bytes += len(data)
                    conn.last_s2c_mono = now
                try:
                    await writer.drain()
                except (ConnectionError, OSError):
                    self._end(conn, rst=True, reason="write_failed")
                    return

        reader_task = asyncio.create_task(_read())
        try:
            writer_task = asyncio.create_task(_write())
            ended = asyncio.create_task(conn.ended.wait())
            await asyncio.wait({writer_task, ended}, return_when=asyncio.FIRST_COMPLETED)
            for task in (writer_task, ended):
                task.cancel()
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError, OSError):
                await reader_task


def _request_line(buffer: bytes) -> tuple[str, str, str]:
    """Split the first HTTP request line into method, target and the full line."""
    line = buffer.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    parts = line.split(" ")
    if len(parts) < 2:
        return "", "", line
    return parts[0], parts[1], line


def _abort_with_rst(writer: asyncio.StreamWriter) -> None:
    """Close *writer*'s socket with RST instead of FIN."""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(Exception):
        writer.transport.abort()
