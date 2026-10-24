"""Quiet anyio's raw-socket teardown race on the UDS transports.

anyio's asyncio backend parks a pending ``UNIXSocketStream`` read/write on a
bare ``loop.add_reader(sock, future.set_result, None)`` callback, and before
4.15 ``_RawSocketMixin.aclose()`` completes that future without a ``done()``
check. When the socket becomes readable on the same event-loop tick the stream
closes, both sides complete the future and the loser raises
``InvalidStateError`` inside an asyncio callback. The server, runner, and
harness-process httpx UDS transports all wrap these streams.

:func:`install_socket_teardown_guard` replaces each racy method with one that
completes only a still-pending future, checking the three methods
independently so an already-guarded ``aclose()`` (anyio 4.15) is left alone
while its bare readiness callbacks are still patched. Methods whose shape is
unrecognized are left unchanged, and once a release guards all of them the
install becomes a no-op.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

_logger = logging.getLogger(__name__)

# One-shot process flag: the patch is class-level, so a single install covers
# every stream for the process lifetime.
_installed = False
# Set when the backend source is unreadable (a frozen/zipapp build): the shape
# can never be inspected, so give up permanently instead of re-probing on every
# transport. A transient import error does not set this, so a retry stays open.
_gave_up = False


class _BackendSourceUnavailable(Exception):
    """Raised when a backend method's source cannot be read for shape detection."""


def install_socket_teardown_guard() -> None:
    """Guard anyio's raw-socket teardown against a double ``set_result``.

    Idempotent and cheap to call from any UDS transport factory: patches
    ``anyio._backends._asyncio._RawSocketMixin`` at most once per process,
    replacing only the methods that still have the unguarded shape.

    :returns: ``None``.
    """
    global _installed, _gave_up
    if _installed or _gave_up:
        return
    try:
        from anyio._backends import _asyncio as anyio_asyncio

        _patch_unguarded_methods(anyio_asyncio._RawSocketMixin)
    except _BackendSourceUnavailable:
        # A method's source is unreadable (a frozen/zipapp build), so the shape
        # can never be inspected here: give up for the process rather than
        # re-probe on every transport. The cost is the original log noise.
        _logger.warning(
            "anyio raw-socket teardown guard not installed: backend source unavailable",
            exc_info=True,
        )
        _gave_up = True
        return
    except Exception:  # noqa: BLE001 — best-effort: an unpatched teardown only logs noise
        # A transient import error (e.g. partial interpreter init): leave both
        # flags unset so a later transport can retry.
        _logger.debug("anyio raw-socket teardown guard not installed", exc_info=True)
        return
    _installed = True


def _patch_unguarded_methods(mixin: type) -> tuple[str, ...]:
    """Replace each of ``mixin``'s methods that still has the racy shape.

    :param mixin: anyio's ``_RawSocketMixin`` class.
    :returns: Names of the methods that were replaced, in declaration order.
    """
    replacements = (
        ("_wait_until_readable", _registers_bare_set_result, _wait_until_readable),
        ("_wait_until_writable", _registers_bare_set_result, _wait_until_writable),
        ("aclose", _completes_without_done_check, _aclose),
    )
    patched: list[str] = []
    unrecognized: list[str] = []
    for name, is_racy, replacement in replacements:
        current = getattr(mixin, name)
        if current is replacement:
            continue
        try:
            source = inspect.getsource(current)
        except (OSError, TypeError) as exc:
            raise _BackendSourceUnavailable(name) from exc
        if is_racy(source):
            setattr(mixin, name, replacement)
            patched.append(name)
        elif ".done()" not in source:
            unrecognized.append(name)
    if unrecognized:
        # Neither the known racy shape nor a done()-guarded one: upstream changed
        # and the shim can no longer tell whether the race is still live, so the
        # noise it exists to prevent may return. Warn rather than whisper at info.
        _logger.warning(
            "anyio raw-socket teardown guard left %s unpatched: unrecognized shape",
            ", ".join(unrecognized),
        )
    return tuple(patched)


def _registers_bare_set_result(source: str) -> bool:
    """Whether a waiter hands ``f.set_result`` straight to the loop's I/O callback."""
    return "f.set_result, None" in source


def _completes_without_done_check(source: str) -> bool:
    """Whether ``aclose`` completes pending futures without checking ``done()``.

    Matches only the known shape, whose ``aclose`` does no readiness
    deregistration of its own. A future ``aclose`` that added such cleanup falls
    through to the unrecognized path rather than being replaced and losing it.
    """
    return (
        ".set_result(None)" in source
        and ".done()" not in source
        and "remove_reader" not in source
        and "remove_writer" not in source
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
