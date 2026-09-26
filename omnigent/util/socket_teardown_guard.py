"""Quiet anyio's raw-socket teardown race on the UDS transports.

anyio's asyncio backend parks a pending ``UNIXSocketStream`` read/write on a
bare ``loop.add_reader(sock, future.set_result, None)`` callback, and its
``_RawSocketMixin.aclose()`` completes the same future without a ``done()``
check. When the socket becomes readable on the same event-loop tick the
stream is closed, both sides complete the future and the loser raises
``InvalidStateError`` inside an asyncio callback — surfaced through the loop
exception handler as ``asyncio: Exception in callback Future.set_result(None)``
and logged at ERROR by the runner. The httpx UDS transports built by the
server, the runner, and the harness process manager wrap exactly these
streams, so an ordinary session or harness teardown with an in-flight read
can emit that noise on every close.

:func:`install_socket_teardown_guard` replaces the three racy methods with
equivalents that only complete a still-pending future. It patches only the
known racy shape: once an anyio release guards the race itself, the shape
check stops matching and the install becomes a no-op, retiring this shim.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

_logger = logging.getLogger(__name__)

# One-shot process flag: the patch is class-level, so a single install (or a
# deliberate skip) covers every stream for the process lifetime.
_installed = False


def install_socket_teardown_guard() -> None:
    """Guard anyio's raw-socket teardown against a double ``set_result``.

    Idempotent and cheap to call from any UDS transport factory: patches
    ``anyio._backends._asyncio._RawSocketMixin`` at most once per process,
    and only while the installed anyio still has the unguarded shape.

    :returns: ``None``.
    """
    global _installed
    if _installed:
        return
    _installed = True
    try:
        from anyio._backends import _asyncio as anyio_asyncio

        mixin = anyio_asyncio._RawSocketMixin
        if not _has_unguarded_teardown(mixin):
            return
        mixin._wait_until_readable = _wait_until_readable
        mixin._wait_until_writable = _wait_until_writable
        mixin.aclose = _aclose
    except Exception:  # noqa: BLE001 — best-effort: an unpatched teardown only logs noise
        _logger.debug("anyio raw-socket teardown guard not installed", exc_info=True)


def _has_unguarded_teardown(mixin: type) -> bool:
    """Whether ``mixin`` still has the racy shape this guard understands.

    :param mixin: anyio's ``_RawSocketMixin`` class.
    :returns: ``True`` when ``aclose`` completes futures without a ``done()``
        check and both waiters register a bare ``set_result`` I/O callback.
    """
    aclose_source = inspect.getsource(mixin.aclose)
    if ".done()" in aclose_source or ".set_result(None)" not in aclose_source:
        return False
    return all(
        "f.set_result, None" in inspect.getsource(method)
        for method in (mixin._wait_until_readable, mixin._wait_until_writable)
    )


def _complete_if_pending(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _wait_until_readable(self: Any, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
    """``_RawSocketMixin._wait_until_readable`` with a done-guarded callback."""

    def callback(_future: object) -> None:
        del self._receive_future
        loop.remove_reader(self._raw_socket)

    f: asyncio.Future[None] = asyncio.Future()
    self._receive_future = f
    loop.add_reader(self._raw_socket, _complete_if_pending, f)
    f.add_done_callback(callback)
    return f


def _wait_until_writable(self: Any, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
    """``_RawSocketMixin._wait_until_writable`` with a done-guarded callback."""

    def callback(_future: object) -> None:
        del self._send_future
        loop.remove_writer(self._raw_socket)

    f: asyncio.Future[None] = asyncio.Future()
    self._send_future = f
    loop.add_writer(self._raw_socket, _complete_if_pending, f)
    f.add_done_callback(callback)
    return f


async def _aclose(self: Any) -> None:
    """``_RawSocketMixin.aclose`` that only completes still-pending waiters."""
    if not self._closing:
        self._closing = True
        if self._raw_socket.fileno() != -1:
            self._raw_socket.close()

        for future in (self._receive_future, self._send_future):
            if future is not None and not future.done():
                future.set_result(None)
