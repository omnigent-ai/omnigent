"""Quiet anyio's raw-socket teardown race on the UDS transports.

anyio's asyncio backend parks a pending ``UNIXSocketStream`` read/write on a
bare ``loop.add_reader(sock, future.set_result, None)`` callback, and releases
before 4.15 also complete that future from ``_RawSocketMixin.aclose()`` without
a ``done()`` check. When the socket becomes readable on the same event-loop
tick the stream is closed, both sides complete the future and the loser raises
``InvalidStateError`` inside an asyncio callback — surfaced through the loop
exception handler as ``Exception in callback Future.set_result(None)`` and
logged at ERROR by the runner. The httpx UDS transports built by the server,
the runner, and the harness process manager wrap exactly these streams, so an
ordinary session or harness teardown with an in-flight read can emit that
noise on every close.

:func:`install_socket_teardown_guard` replaces each racy method with an
equivalent that only completes a still-pending future. The three methods are
checked independently: anyio 4.15 guards ``aclose()`` but still registers the
bare readiness callbacks, so the waiters stay patched there while the already
safe ``aclose()`` is left alone. Once a release guards all of them, the install
becomes a no-op and this shim retires itself.
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
    replacing only the methods that still have the unguarded shape.

    :returns: ``None``.
    """
    global _installed
    if _installed:
        return
    try:
        from anyio._backends import _asyncio as anyio_asyncio

        _patch_unguarded_methods(anyio_asyncio._RawSocketMixin)
    except Exception:  # noqa: BLE001 — best-effort: an unpatched teardown only logs noise
        # Leave the flag unset so a later transport can retry; a genuinely
        # missing backend keeps logging only debug noise, not an error.
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
        source = inspect.getsource(current)
        if is_racy(source):
            setattr(mixin, name, replacement)
            patched.append(name)
        elif ".done()" not in source:
            unrecognized.append(name)
    if unrecognized:
        # Neither the known racy shape nor a done()-guarded one: upstream changed
        # and the shim can no longer tell whether the race is still live.
        _logger.info(
            "anyio raw-socket teardown guard left %s unpatched: unrecognized shape",
            ", ".join(unrecognized),
        )
    return tuple(patched)


def _registers_bare_set_result(source: str) -> bool:
    """Whether a waiter hands ``f.set_result`` straight to the loop's I/O callback."""
    return "f.set_result, None" in source


def _completes_without_done_check(source: str) -> bool:
    """Whether ``aclose`` completes pending futures without checking ``done()``."""
    return ".set_result(None)" in source and ".done()" not in source


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
