"""Loopback TCP proxy that imposes an absolute lifetime on WebSocket connections.

Stands in for an intermediary (load balancer, ingress) between a runner and
the server: connections carrying a WebSocket upgrade are severed a fixed
number of seconds after acceptance without a WebSocket close frame, so the
client observes close code 1006. Plain HTTP connections pass through; selected
request paths can be stalled before forwarding to simulate a slow backend.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from tests._helpers.live_server import find_free_port

_HEAD_READ_TIMEOUT_S = 5.0
_HEAD_MAX_BYTES = 64 * 1024


class LifetimeProxy:
    """Forward loopback TCP traffic to *upstream* with a WebSocket lifetime cap.

    :param upstream_host: Upstream server host, e.g. ``"127.0.0.1"``.
    :param upstream_port: Upstream server port.
    :param ws_lifetime_s: Seconds after acceptance at which an upgraded
        connection is aborted; ``None`` never aborts.
    :param max_aborts: Cap on how many upgraded connections are aborted;
        ``None`` aborts every one.
    :param stall_paths: Request paths (first request on a connection) whose
        forwarding is delayed by the mapped number of seconds.
    :param stall_window_after_abort_s: When set, stalls apply only within this
        many seconds after the most recent abort (none before the first abort).
    :param stall_max_requests: Cap on how many requests are stalled in total.
    :param event_log: Optional JSON-lines file receiving one record per
        connection event (``accept``, ``stall``, ``abort``, ``close``).
    """

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        *,
        ws_lifetime_s: float | None,
        max_aborts: int | None = None,
        stall_paths: Mapping[str, float] | None = None,
        stall_window_after_abort_s: float | None = None,
        stall_max_requests: int | None = None,
        event_log: Path | None = None,
    ) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._ws_lifetime_s = ws_lifetime_s
        self._max_aborts = max_aborts
        self._aborts = 0
        self._last_abort_at: float | None = None
        self._stall_paths = dict(stall_paths or {})
        self._stall_window_after_abort_s = stall_window_after_abort_s
        self._stall_max_requests = stall_max_requests
        self._stalled = 0
        self._event_log = event_log
        self.port = find_free_port()
        self.events: list[dict[str, object]] = []
        self._events_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._conn_seq = 0

    @property
    def url(self) -> str:
        """HTTP base URL clients should use instead of the upstream."""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        """Start serving on a background event loop thread."""
        self._thread = threading.Thread(target=self._run, name="ws-lifetime-proxy", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise RuntimeError("lifetime proxy did not start within 10s")

    def stop(self) -> None:
        """Stop serving and join the loop thread."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def snapshot(self) -> list[dict[str, object]]:
        """Return a copy of the recorded connection events."""
        with self._events_lock:
            return list(self.events)

    def _record(self, **event: object) -> None:
        event.setdefault("t", time.time())
        with self._events_lock:
            self.events.append(event)
        if self._event_log is not None:
            with self._event_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")

    def _stall_allowed(self) -> bool:
        if self._stall_max_requests is not None and self._stalled >= self._stall_max_requests:
            return False
        if self._stall_window_after_abort_s is None:
            return True
        if self._last_abort_at is None:
            return False
        return time.time() - self._last_abort_at <= self._stall_window_after_abort_s

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(
            asyncio.start_server(self._handle_client, "127.0.0.1", self.port)
        )
        self._server = server
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            server.close()
            loop.run_until_complete(server.wait_closed())
            # Cancel and reap in-flight handlers so their piped sockets close
            # before the loop does, rather than leaking when stop() is called.
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        self._conn_seq += 1
        conn = self._conn_seq
        accepted_at = time.time()
        head = b""
        try:
            while b"\r\n\r\n" not in head and len(head) < _HEAD_MAX_BYTES:
                chunk = await asyncio.wait_for(
                    client_reader.read(4096), timeout=_HEAD_READ_TIMEOUT_S
                )
                if not chunk:
                    break
                head += chunk
        except (TimeoutError, OSError):
            client_writer.transport.abort()
            return
        request_line, _, header_blob = head.partition(b"\r\n")
        parts = request_line.decode("latin-1", errors="replace").split(" ")
        path = parts[1] if len(parts) >= 2 else ""
        headers = header_blob.decode("latin-1", errors="replace").lower()
        is_upgrade = "upgrade: websocket" in headers
        self._record(kind="accept", conn=conn, path=path, upgrade=is_upgrade, t=accepted_at)

        stall_s = self._stall_paths.get(path.split("?", 1)[0])
        if stall_s and self._stall_allowed():
            self._stalled += 1
            self._record(kind="stall", conn=conn, path=path, seconds=stall_s)
            await asyncio.sleep(stall_s)

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self._upstream)
        except OSError as exc:
            self._record(kind="upstream_error", conn=conn, error=str(exc))
            client_writer.transport.abort()
            return

        upstream_writer.write(head)
        await upstream_writer.drain()

        abort_handle: asyncio.TimerHandle | None = None
        if (
            is_upgrade
            and self._ws_lifetime_s is not None
            and (self._max_aborts is None or self._aborts < self._max_aborts)
        ):
            loop = asyncio.get_running_loop()
            remaining = max(0.0, accepted_at + self._ws_lifetime_s - time.time())

            def _abort() -> None:
                # Spend the budget only when an abort actually fires: a make-
                # before-break cutover cancels this timer, and a cancelled abort
                # must not consume a later connection's budget.
                if self._max_aborts is not None and self._aborts >= self._max_aborts:
                    return
                self._aborts += 1
                self._last_abort_at = time.time()
                self._record(
                    kind="abort", conn=conn, path=path, age_s=round(time.time() - accepted_at, 3)
                )
                client_writer.transport.abort()
                upstream_writer.transport.abort()

            abort_handle = loop.call_later(remaining, _abort)

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except asyncio.CancelledError:
                raise
            except OSError:
                pass
            finally:
                if not dst.is_closing():
                    dst.close()

        try:
            await asyncio.gather(
                pipe(client_reader, upstream_writer),
                pipe(upstream_reader, client_writer),
            )
        finally:
            if abort_handle is not None:
                abort_handle.cancel()
            self._record(
                kind="close", conn=conn, path=path, age_s=round(time.time() - accepted_at, 3)
            )
