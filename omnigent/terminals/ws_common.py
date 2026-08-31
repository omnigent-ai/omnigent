"""Shared WebSocket framing and tmux liveness helpers for terminal bridges."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
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
