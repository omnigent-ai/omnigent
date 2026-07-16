"""In-memory registry of live host WebSocket connections.

Each server replica maintains one :class:`HostRegistry` tracking
hosts with active WebSocket tunnels on this replica. The persistent
``hosts`` DB table (queried by ``HostStore``) is the cross-replica
source of truth for which hosts exist; this registry only tracks
which hosts are live *here*.

Simpler than :class:`TunnelRegistry` because the host tunnel
carries only control frames (launch/stop runner), not HTTP
request/response traffic. No per-request reassembly queues needed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from cachetools import TTLCache

from omnigent.host.frames import HostHelloFrame

_logger = logging.getLogger(__name__)

# How long a runner exit report stays answerable, and how many are kept.
# Reports only matter while a client is still waiting for the runner to
# come online (a 60s window today); 10 minutes covers slow retries with
# margin. Runner ids are unique per launch, so entries never need
# invalidation — the TTL is purely a memory bound.
_EXIT_REPORT_TTL_S = 600.0
_EXIT_REPORT_MAX_ENTRIES = 1024


@dataclass
class RunnerExitReport:
    """A host daemon's report that a spawned runner died unexpectedly.

    :param error: Human-readable cause composed by the daemon (exit
        code, host-side log path, log tail), e.g.
        ``"runner process exited with code 1 (log on host: ~/...)"``.
    :param owner: User who owns the host tunnel the report arrived on,
        e.g. ``"alice@example.com"``. ``None`` when auth is disabled.
        Gates visibility: only the owner may read the report (the log
        tail can contain agent output).
    """

    error: str
    owner: str | None


class RunnerExitReports:
    """Thread-safe, TTL-bounded store of runner exit reports.

    Written by the host tunnel when a ``host.runner_exited`` frame
    arrives; read by the runner status endpoint so a client polling a
    never-connecting runner learns *why* instead of timing out.
    In-memory and per-replica, same posture as :class:`HostRegistry` —
    the report and the status poll meet on the replica holding the
    host tunnel.
    """

    def __init__(self) -> None:
        """Initialize an empty report store."""
        self._lock = threading.Lock()
        self._reports: TTLCache[str, RunnerExitReport] = TTLCache(
            maxsize=_EXIT_REPORT_MAX_ENTRIES,
            ttl=_EXIT_REPORT_TTL_S,
        )

    def record(self, runner_id: str, error: str, owner: str | None) -> None:
        """Store a runner exit report.

        :param runner_id: The dead runner, e.g. ``"runner_abc123"``.
        :param error: Human-readable cause from the host daemon.
        :param owner: Owner of the reporting host tunnel, or ``None``
            when auth is disabled.
        """
        with self._lock:
            self._reports[runner_id] = RunnerExitReport(error=error, owner=owner)

    def get(self, runner_id: str) -> str | None:
        """Look up a report's error without owner scoping.

        For callers that have already authorized access by another
        means (e.g. the session snapshot, gated on session permission):
        the report pertains to that session's own runner, so no
        separate owner check is needed. The runner status endpoint —
        keyed only by ``runner_id`` with no session-level auth — must
        use :meth:`get_visible` instead.

        :param runner_id: Runner id, e.g. ``"runner_abc123"``.
        :returns: The error message, or ``None`` when no report exists.
        """
        with self._lock:
            report: RunnerExitReport | None = self._reports.get(runner_id)
        return report.error if report is not None else None

    def get_visible(self, runner_id: str, user_id: str | None) -> str | None:
        """Look up a report, scoped to its owner.

        :param runner_id: Runner id, e.g. ``"runner_abc123"``.
        :param user_id: The requesting user, or ``None`` when auth is
            disabled.
        :returns: The error message, or ``None`` when no report exists
            or the caller doesn't own it (W6-2 posture: other users'
            runners reveal nothing).
        """
        with self._lock:
            report: RunnerExitReport | None = self._reports.get(runner_id)
        if report is None:
            return None
        if user_id is not None and report.owner is not None and report.owner != user_id:
            return None
        return report.error


class WebSocketLike(Protocol):
    """Minimal WebSocket protocol for the host tunnel.

    Both Starlette's ``WebSocket`` and test fakes implement this.
    """

    async def send_text(self, data: str) -> None:
        """Send a text frame."""
        ...

    async def receive_text(self) -> str:
        """Receive a text frame."""
        ...


@dataclass
class HostConnection:
    """Per-host state while the tunnel is open.

    :param host_id: Stable host identifier, e.g.
        ``"host_a1b2c3d4..."``.
    :param ws: The live WebSocket to this host.
    :param hello: The hello frame the host sent on connect.
    :param owner: Authenticated user who established the tunnel,
        e.g. ``"alice@example.com"``. ``None`` when auth is
        disabled (single-user mode).
    :param outbound_queue: Queue consumed by the WebSocket route's
        sender task. Control frames are enqueued here rather than
        calling ``ws.send_text`` directly, since the caller may
        be on a different thread.
    :param connected_at: Unix epoch float of connect time.
    :param last_frame_at: Unix epoch float of the most recent
        frame from this host.
    :param pending_launches: Per-``request_id`` futures for
        in-flight ``host.launch_runner`` requests. Resolved when
        the host sends ``host.launch_runner_result``.
    :param pending_stops: Per-``request_id`` futures for
        in-flight ``host.stop_runner`` requests. Resolved when
        the host sends ``host.stop_runner_result``.
    :param pending_runner_status: Per-``request_id`` futures for
        in-flight ``host.runner_status`` queries. Resolved when the
        host sends ``host.runner_status_result``. Values carry the
        single ``status`` field (``"alive"`` / ``"dead"`` /
        ``"unknown"``).
    :param pending_stats: Per-``request_id`` futures for in-flight
        ``host.stat`` requests. Resolved when the host sends
        ``host.stat_result``. The dict values carry the full
        stat-result fields (``status``, ``exists``, ``type``,
        ``canonical_path``, ``error``); typed as ``Any`` because
        Python ``dict`` parametric types here would force every
        callsite to cast.
    :param pending_list_dirs: Per-``request_id`` futures for
        in-flight ``host.list_dir`` requests. Resolved when the
        host sends ``host.list_dir_result``. Values carry the
        listing fields (``status``, ``entries`` as list of
        dicts, ``has_more``, ``error``). Same ``Any`` typing
        rationale as ``pending_stats``.
    :param pending_create_worktrees: Per-``request_id`` futures for
        in-flight ``host.create_worktree`` requests. Resolved when
        the host sends ``host.create_worktree_result``. Values
        carry the result fields (``status``, ``worktree_path``,
        ``branch``, ``error``). Same ``Any`` typing rationale as
        ``pending_stats``.
    :param pending_remove_worktrees: Per-``request_id`` futures for
        in-flight ``host.remove_worktree`` requests. Resolved when
        the host sends ``host.remove_worktree_result``. Values
        carry ``status`` and ``error``.
    :param pending_create_dirs: Per-``request_id`` futures for
        in-flight ``host.create_dir`` requests. Resolved when the
        host sends ``host.create_dir_result``. Values carry the
        result fields (``status``, ``path``, ``error``). Same
        ``Any`` typing rationale as ``pending_stats``.
    :param pending_fs_requests: Per-``request_id`` futures for
        in-flight ``host.fs_request`` reads (the workspace file
        panel served from the host while the runner is offline).
        Resolved when the host sends ``host.fs_result``. Values
        carry ``status``, ``payload``, ``error_status``,
        ``error_code``, and ``error``.
    """

    host_id: str
    ws: WebSocketLike
    hello: HostHelloFrame
    owner: str | None
    outbound_queue: asyncio.Queue[str | None]
    connected_at: float
    last_frame_at: float
    pending_launches: dict[str, asyncio.Future[dict[str, str | None]]] = field(
        default_factory=dict,
    )
    pending_stops: dict[str, asyncio.Future[dict[str, str | None]]] = field(
        default_factory=dict,
    )
    pending_runner_status: dict[str, asyncio.Future[dict[str, str | None]]] = field(
        default_factory=dict,
    )
    pending_stats: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )
    pending_list_dirs: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )
    pending_create_worktrees: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )
    pending_remove_worktrees: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )
    pending_list_worktrees: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )
    pending_create_dirs: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )
    pending_fs_requests: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )


class HostRegistry:
    """Thread-safe registry of live host WebSocket connections.

    All public methods acquire ``_lock`` so callers on different
    threads (e.g. REST route handlers vs. WebSocket event loops)
    don't race.
    """

    def __init__(self) -> None:
        """Initialize an empty host registry."""
        self._lock = threading.RLock()
        self._hosts: dict[str, HostConnection] = {}

    def register(
        self,
        host_id: str,
        ws: WebSocketLike,
        hello: HostHelloFrame,
        owner: str | None,
    ) -> HostConnection:
        """Register a host connection (newest wins).

        If ``host_id`` is already registered (stale connection),
        the old connection is replaced and its outbound queue is
        poisoned with ``None`` so the sender loop exits.

        :param host_id: Stable host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param ws: The live WebSocket.
        :param hello: The hello frame from the host.
        :param owner: Authenticated user ID, or ``None``.
        :returns: The new :class:`HostConnection`.
        """
        now = time.time()
        conn = HostConnection(
            host_id=host_id,
            ws=ws,
            hello=hello,
            owner=owner,
            outbound_queue=asyncio.Queue(),
            connected_at=now,
            last_frame_at=now,
        )
        with self._lock:
            old = self._hosts.get(host_id)
            if old is not None:
                _logger.info(
                    "replacing stale host connection: %s",
                    host_id,
                )
                old.outbound_queue.put_nowait(None)
            self._hosts[host_id] = conn
        return conn

    def deregister(self, host_id: str) -> None:
        """Remove a host connection.

        No-op if ``host_id`` is not registered.

        :param host_id: Host identifier to remove.
        """
        with self._lock:
            self._hosts.pop(host_id, None)

    def get(self, host_id: str) -> HostConnection | None:
        """Look up a live host connection.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: The :class:`HostConnection` if online,
            otherwise ``None``.
        """
        with self._lock:
            return self._hosts.get(host_id)

    def online_host_ids(self) -> list[str]:
        """Return IDs of all currently connected hosts.

        :returns: List of host_id strings.
        """
        with self._lock:
            return list(self._hosts.keys())

    def is_host_telemetry_opted_out(self, host_id: str) -> bool:
        """Return whether the host has opted out of telemetry.

        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :returns: ``True`` when the host sent ``telemetry_opt_out=True``
            in its hello frame.  Defaults to ``False`` when the host is
            offline or unknown.
        """
        conn = self.get(host_id)
        if conn is None:
            return False
        return conn.hello.telemetry_opt_out

    def get_host_installation_id(self, host_id: str) -> str | None:
        """Return the installation ID the host advertised in its hello frame.

        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :returns: The host's installation ID, or ``None`` when offline or
            not set.
        """
        conn = self.get(host_id)
        if conn is None:
            return None
        return conn.hello.installation_id

    def send_text(self, conn: HostConnection, data: str) -> None:
        """Enqueue a text frame for sending to the host.

        Must be called on the host WebSocket's owning event loop.
        ``asyncio.Queue`` is coroutine-safe within a single loop, NOT
        thread-safe — ``put_nowait`` mutates the underlying deque
        without a lock. Every current caller (REST handlers, the WS
        receive loop, the ping loop) runs on the uvicorn event loop,
        so the call below is safe. A caller on another thread must use
        ``loop.call_soon_threadsafe(queue.put_nowait, data)`` instead.

        :param conn: The target host connection.
        :param data: JSON-encoded frame text.
        :raises ConnectionError: If the connection has been
            replaced (the outbound queue was poisoned).
        """
        with self._lock:
            current = self._hosts.get(conn.host_id)
            if current is not conn:
                raise ConnectionError(f"host {conn.host_id!r} connection was replaced")

        conn.outbound_queue.put_nowait(data)
