"""Shared WebSocket framing and tmux liveness helpers for terminal bridges."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fastapi import WebSocket

_logger = logging.getLogger(__name__)

# Default per-frame cap: merge queued terminal chunks into bounded sends so
# huge bursts stream without delaying interactive output.
_WS_COALESCE_MAX_BYTES: Final[int] = 64 * 1024
# Keep these in sync with web's synchronous-echo limits.
_INTERACTIVE_WS_COALESCE_MAX_BYTES: Final[int] = 2048
_INTERACTIVE_ECHO_WINDOW_S: Final[float] = 0.75

# Bound queued terminal output by bytes and objects. The byte budget absorbs
# ordinary bursts; the item budget protects against floods of tiny chunks.
_OUTPUT_QUEUE_MAX_BYTES: Final[int] = 16 * 1024 * 1024
_OUTPUT_QUEUE_MAX_ITEMS: Final[int] = 32768
_OUTPUT_DROP_WARN_INTERVAL_S: Final[float] = 30.0
# CAN aborts a partial CSI and ST terminates a dangling OSC/DCS string.
_OUTPUT_GAP_RESYNC: Final[bytes] = b"\x18\x1b\\"
_REPAINT_MIN_INTERVAL_S: Final[float] = 2.0

# Application-level WebSocket close codes (RFC 6455 reserves 4xxx).
WS_CLOSE_TERMINAL_NOT_FOUND: Final[int] = 4404
WS_CLOSE_TERMINAL_DETACHED: Final[int] = 4405
# The runner tunnel exists on another replica; the client retries keyless.
WS_CLOSE_WRONG_REPLICA: Final[int] = 4400
WS_CLOSE_INTERNAL_ERROR: Final[int] = 4500

# A local tmux liveness probe should never stall bridge teardown.
_TMUX_HAS_SESSION_TIMEOUT_S: Final[float] = 2.0


def _monotonic() -> float:
    """Return a monotonic clock reading for terminal bridge timing."""
    return time.monotonic()


class _ByteBoundedOutputQueue(asyncio.Queue[bytes | None]):
    """Terminal-output queue that drops new chunks past its byte/item budget.

    Already-queued output remains contiguous with bytes sent to the terminal,
    so saturation rejects an incoming chunk whole. The next accepted chunk is
    prefixed with parser-resynchronization bytes. Producer loss invokes
    ``on_drop`` so the bridge can request a full repaint; snapshot-priority
    eviction is healed by the snapshot being inserted. EOF is always kept.
    """

    def __init__(
        self,
        max_bytes: int = _OUTPUT_QUEUE_MAX_BYTES,
        max_items: int = _OUTPUT_QUEUE_MAX_ITEMS,
        identity: str = "unknown terminal",
    ) -> None:
        super().__init__()
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.identity = identity
        self.queued_bytes = 0
        self.dropped_chunks = 0
        self.dropped_bytes = 0
        self.on_drop: Callable[[], None] | None = None
        self._in_gap = False
        self._warned_at: float | None = None

    def _put(self, item: bytes | None) -> None:
        super()._put(item)
        if item is not None:
            self.queued_bytes += len(item)

    def _get(self) -> bytes | None:
        item = super()._get()
        if item is not None:
            self.queued_bytes -= len(item)
        return item

    def record_dropped_output(self, num_bytes: int, *, request_repaint: bool = True) -> None:
        """Record lost output, open a parser gap, and optionally request a repaint."""
        self.dropped_chunks += 1
        self.dropped_bytes += num_bytes
        self._in_gap = True
        now = _monotonic()
        if self._warned_at is None or now - self._warned_at >= _OUTPUT_DROP_WARN_INTERVAL_S:
            self._warned_at = now
            _logger.warning(
                "terminal output queue saturated or producer shed output "
                "for %s (%d bytes / %d chunks queued); %d chunks (%d bytes) "
                "dropped so far",
                self.identity,
                self.queued_bytes,
                self.qsize(),
                self.dropped_chunks,
                self.dropped_bytes,
            )
        if request_repaint and self.on_drop is not None:
            self.on_drop()

    def put_nowait(self, item: bytes | None) -> None:
        if item is None:
            super().put_nowait(item)
            return
        gap_bytes = len(_OUTPUT_GAP_RESYNC) if self._in_gap else 0
        added_items = 2 if self._in_gap else 1
        if (
            gap_bytes + len(item) > self.max_bytes
            or self.queued_bytes + gap_bytes + len(item) > self.max_bytes
            or self.qsize() + added_items > self.max_items
        ):
            self.record_dropped_output(len(item))
            return
        if self._in_gap:
            super().put_nowait(_OUTPUT_GAP_RESYNC)
        self._in_gap = False
        super().put_nowait(item)

    def put_snapshot_nowait(self, snapshot: bytes) -> bool:
        """Prioritize a full repaint, returning whether it was retained."""
        while True:
            gap_bytes = len(_OUTPUT_GAP_RESYNC) if self._in_gap else 0
            added_items = 2 if self._in_gap else 1
            if gap_bytes + len(snapshot) > self.max_bytes or added_items > self.max_items:
                self.record_dropped_output(len(snapshot), request_repaint=False)
                return False
            if (
                self.queued_bytes + gap_bytes + len(snapshot) <= self.max_bytes
                and self.qsize() + added_items <= self.max_items
            ):
                if self._in_gap:
                    super().put_nowait(_OUTPUT_GAP_RESYNC)
                self._in_gap = False
                super().put_nowait(snapshot)
                return True
            discarded = self.get_nowait()
            if discarded is None:
                super().put_nowait(None)
                return False
            # The snapshot being inserted heals these priority evictions.
            self.record_dropped_output(len(discarded), request_repaint=False)

    def log_drop_summary(self) -> None:
        """Log final loss counters for this attached client."""
        if self.dropped_chunks == 0:
            return
        _logger.warning(
            "terminal output queue closed for %s; %d chunks (%d bytes) dropped; "
            "%d bytes / %d chunks remain queued",
            self.identity,
            self.dropped_chunks,
            self.dropped_bytes,
            self.queued_bytes,
            self.qsize(),
        )


class _GapRepainter:
    """Coalesce repaint requests and pace them across repeated drop gaps."""

    def __init__(
        self,
        repaint: Callable[[], Coroutine[object, object, None]],
        min_interval_s: float = _REPAINT_MIN_INTERVAL_S,
    ) -> None:
        self._repaint = repaint
        self._min_interval_s = min_interval_s
        self._task: asyncio.Task[None] | None = None
        self._last_started_at: float | None = None
        self._capturing = False
        self._trailing = False
        self._failure: BaseException | None = None
        self._failed = asyncio.Event()

    def request(self) -> None:
        """Schedule at most one active repaint and one trailing repaint."""
        if self._failure is not None:
            return
        if self._task is not None and not self._task.done():
            if self._capturing:
                self._trailing = True
            return
        self._task = asyncio.create_task(self._run(self._cooldown()))

    def _cooldown(self) -> float:
        if self._last_started_at is None:
            return 0.0
        return max(0.0, self._last_started_at + self._min_interval_s - _monotonic())

    async def _run(self, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self._capturing = True
        self._last_started_at = _monotonic()
        try:
            await self._repaint()
        except Exception as exc:
            self._failure = exc
            self._trailing = False
            self._failed.set()
            _logger.error("terminal gap repaint failed permanently", exc_info=True)
            return
        finally:
            self._capturing = False
        if self._trailing:
            self._trailing = False
            self._task = asyncio.create_task(self._run(self._cooldown()))

    async def wait_failed(self) -> None:
        """Wait until repaint recovery has failed irrecoverably."""
        await self._failed.wait()

    async def flush(self, timeout_s: float) -> None:
        """Wait briefly for pending repaint output to land before EOF."""
        deadline = _monotonic() + timeout_s
        while True:
            task = self._task
            if task is None or task.done():
                return
            remaining = deadline - _monotonic()
            if remaining <= 0:
                return
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            if self._task is task:
                return

    async def cancel(self) -> None:
        """Cancel and join pending repaint work during bridge teardown."""
        self._trailing = False
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._task is task:
            self._task = None
        self._trailing = False


async def _tmux_session_alive(socket_path: str, tmux_target: str) -> bool:
    """Return whether the targeted tmux pane still has a live process.

    The pane-dead flag, rather than bare session existence, handles terminals
    configured with ``remain-on-exit``. Probe errors fail closed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            socket_path,
            "list-panes",
            "-t",
            tmux_target,
            "-F",
            "#{pane_dead}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        _logger.debug("tmux pane-dead probe spawn failed", exc_info=True)
        return False
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=_TMUX_HAS_SESSION_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, OSError):
        _logger.debug("tmux pane-dead probe timed out", exc_info=True)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return False
    panes = stdout.decode().split()
    return proc.returncode == 0 and bool(panes) and "1" not in panes


async def _check_pane_dead_definitive(socket_path: str, tmux_target: str) -> bool | None:
    """Return pane liveness, or ``None`` when the probe is inconclusive."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            socket_path,
            "list-panes",
            "-t",
            tmux_target,
            "-F",
            "#{pane_dead}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        _logger.debug("tmux pane-dead probe spawn failed", exc_info=True)
        return None
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=_TMUX_HAS_SESSION_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, OSError):
        _logger.debug("tmux pane-dead probe timed out", exc_info=True)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    if proc.returncode != 0:
        _logger.debug("tmux pane-dead probe got non-zero rc=%s", proc.returncode)
        return None
    return "1" in stdout.decode().split()


async def _forward_terminal_to_ws(
    websocket: WebSocket,
    output_chunks: asyncio.Queue[bytes | None],
    *,
    max_coalesce_bytes: int | Callable[[], int] = _WS_COALESCE_MAX_BYTES,
    send_lock: asyncio.Lock | None = None,
) -> None:
    """Forward queued terminal output as bounded binary WebSocket frames."""
    from fastapi import WebSocketDisconnect

    pending = bytearray()
    eof_seen = False
    while True:
        if not pending:
            chunk = await output_chunks.get()
            if chunk is None:
                return
            pending.extend(chunk)

        limit = _current_coalesce_limit(max_coalesce_bytes)
        while len(pending) < limit:
            try:
                nxt = output_chunks.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nxt is None:
                eof_seen = True
                break
            pending.extend(nxt)
            limit = _current_coalesce_limit(max_coalesce_bytes)

        while pending:
            limit = _current_coalesce_limit(max_coalesce_bytes)
            frame = bytes(pending[:limit])
            del pending[:limit]
            try:
                if send_lock is None:
                    await websocket.send_bytes(frame)
                else:
                    async with send_lock:
                        await websocket.send_bytes(frame)
            except (RuntimeError, WebSocketDisconnect):
                return
        if eof_seen:
            return


def _current_coalesce_limit(max_coalesce_bytes: int | Callable[[], int]) -> int:
    """Resolve and validate the active terminal-output frame cap."""
    raw = max_coalesce_bytes() if callable(max_coalesce_bytes) else max_coalesce_bytes
    if raw <= 0:
        raise ValueError("max_coalesce_bytes must be positive")
    return raw


def _coalesce_limit_after_input(last_client_input_at: float | None) -> int:
    """Return a low-latency frame cap immediately after client input."""
    if last_client_input_at is None:
        return _WS_COALESCE_MAX_BYTES
    if _monotonic() - last_client_input_at < _INTERACTIVE_ECHO_WINDOW_S:
        return _INTERACTIVE_WS_COALESCE_MAX_BYTES
    return _WS_COALESCE_MAX_BYTES
