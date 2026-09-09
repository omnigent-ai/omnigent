"""Force-close asyncio subprocess transports before loop teardown.

``asyncio.subprocess.Process.wait()`` returns when the subprocess exits,
but the transport is only marked ``_closed`` when something calls
``transport.close()`` explicitly. If the test event loop closes first,
GC later calls ``BaseSubprocessTransport.__del__`` which does
``self._loop.call_soon(...)`` on a closed loop and raises
``RuntimeError('Event loop is closed')``.

``_transport`` is a stable private attr on
``asyncio.subprocess.Process`` across CPython 3.10+.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from . import _proc

logger = logging.getLogger(__name__)


async def terminate_subprocess(
    proc: Any,  # type: ignore[explicit-any]
    *,
    terminate_timeout: float,
    kill_timeout: float,
    label: str,
    terminate_tree: Callable[[Any], None] = _proc.terminate_tree,  # type: ignore[explicit-any]
    kill_tree: Callable[[Any], None] = _proc.kill_tree,  # type: ignore[explicit-any]
) -> bool:
    """Terminate a process tree and wait only within explicit bounds.

    Returns whether the direct child was reaped. A false result means SIGKILL
    was sent but the child did not become waitable before the final deadline;
    callers must close its transport and continue cleanup rather than hang.
    """
    if getattr(proc, "returncode", None) is None:
        terminate_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=terminate_timeout)
        except asyncio.TimeoutError:
            kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
            except asyncio.TimeoutError:
                logger.error(
                    "%s pid=%s did not reap within %.2fs after SIGKILL",
                    label,
                    getattr(proc, "pid", None),
                    kill_timeout,
                )
                return False
        else:
            # The direct child exiting does not prove its process group or
            # detached descendants are empty.
            kill_tree(proc)
    else:
        # A dead leader does not prove that descendants in its owned process
        # group exited. Keep the tree broadcast as a best-effort final sweep.
        kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
        except asyncio.TimeoutError:
            logger.error(
                "%s pid=%s remained unreaped after leader exit",
                label,
                getattr(proc, "pid", None),
            )
            return False
    return True


def close_subprocess_transport(proc: Any) -> None:  # type: ignore[explicit-any]
    """Force-close ``proc._transport``. Safe on missing/already-closed."""
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    is_closing = getattr(transport, "is_closing", None)
    if callable(is_closing) and is_closing():
        return
    with contextlib.suppress(Exception):
        transport.close()


def close_anyio_subprocess_transport(anyio_proc: Any) -> None:  # type: ignore[explicit-any]
    """Unwrap an anyio ``Process`` to its underlying asyncio process and close its transport."""
    inner = getattr(anyio_proc, "_process", None)
    if inner is None:
        return
    close_subprocess_transport(inner)


__all__ = [
    "close_anyio_subprocess_transport",
    "close_subprocess_transport",
    "terminate_subprocess",
]
