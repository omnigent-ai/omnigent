"""anyio raw-socket teardown guard: close-during-I/O stays quiet, streams still work."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from omnigent.util import socket_teardown_guard
from omnigent.util.socket_teardown_guard import install_socket_teardown_guard

_INVALID_STATE_MESSAGE = "Exception in callback Future.set_result(None)"
_REQUIRES_UDS = pytest.mark.skipif(
    sys.platform == "win32", reason="Unix domain sockets are POSIX-only"
)

_Delivery = tuple[str, BaseException | None]


async def _connected_uds_pair(
    socket_path: str,
) -> tuple[Any, socket.socket, socket.socket]:
    """Return (anyio stream, peer socket, listening server socket)."""
    loop = asyncio.get_running_loop()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)
    server.setblocking(False)
    accept_task = asyncio.ensure_future(loop.sock_accept(server))
    stream = await anyio.connect_unix(socket_path)
    peer, _ = await accept_task
    peer.setblocking(False)
    return stream, peer, server


async def _wait_until_parked(stream: Any, future_attr: str = "_receive_future") -> None:
    """Spin until a pending receive/send has registered its readiness future."""
    for _ in range(50):
        await asyncio.sleep(0)
        if getattr(stream, future_attr, None) is not None:
            return
    raise AssertionError(f"stream never parked on {future_attr}")


async def _park_pending_read(stream: Any) -> asyncio.Task[None]:
    """Start a ``receive()`` and wait until it parks on ``add_reader``."""

    async def _receive() -> None:
        # The teardown under test abandons this read; its outcome is irrelevant.
        with contextlib.suppress(Exception):
            await stream.receive()

    task = asyncio.ensure_future(_receive())
    await _wait_until_parked(stream, "_receive_future")
    return task


@contextlib.contextmanager
def _capturing_loop_exceptions(loop: asyncio.AbstractEventLoop) -> Iterator[list[_Delivery]]:
    """Collect what the loop's exception handler receives while the block runs."""
    previous_handler = loop.get_exception_handler()
    delivered: list[_Delivery] = []

    def _capture(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        exc = context.get("exception")
        message = str(context.get("message") or "")
        delivered.append((message, exc if isinstance(exc, BaseException) else None))

    loop.set_exception_handler(_capture)
    try:
        yield delivered
    finally:
        loop.set_exception_handler(previous_handler)


async def _finish(*tasks: asyncio.Task[None]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _close_during_pending_read(tmp: Path, iterations: int) -> list[_Delivery]:
    """Close streams while a read is parked; return what reached the loop handler."""
    with _capturing_loop_exceptions(asyncio.get_running_loop()) as delivered:
        for i in range(iterations):
            stream, peer, server = await _connected_uds_pair(str(tmp / f"r{i}.sock"))
            recv_task = await _park_pending_read(stream)
            # Peer EOF queues the reader callback; aclose() on the same
            # stretch used to double-complete the reader future.
            peer.close()
            aclose_task = asyncio.ensure_future(stream.aclose())
            for _ in range(4):
                await asyncio.sleep(0)
            await _finish(recv_task, aclose_task)
            server.close()
    return delivered


def _drive_aclose(stream: Any) -> None:
    """Run ``aclose()`` synchronously inside a loop callback.

    Driving the coroutine here completes the readiness future on the same loop
    iteration the queued readiness callback fires, which is the teardown race.
    """
    coro = stream.aclose()
    try:
        coro.send(None)
    except StopIteration:
        return
    coro.close()


async def _close_during_pending_write(tmp: Path, iterations: int) -> list[_Delivery]:
    """Park a writer, then close on the same tick; return what reached the handler."""
    loop = asyncio.get_running_loop()
    with _capturing_loop_exceptions(loop) as delivered:
        for i in range(iterations):
            stream, peer, server = await _connected_uds_pair(str(tmp / f"w{i}.sock"))
            # A fresh socket is immediately writable, so parking a writer queues
            # its readiness callback for this tick; aclose() then completes the
            # same _send_future the queued callback is about to complete.
            stream._wait_until_writable(loop)
            loop.call_soon(_drive_aclose, stream)
            await asyncio.sleep(0)
            peer.close()
            server.close()
    return delivered


def _invalid_state_deliveries(delivered: list[_Delivery]) -> list[_Delivery]:
    return [
        (message, exc)
        for message, exc in delivered
        if isinstance(exc, asyncio.InvalidStateError) and _INVALID_STATE_MESSAGE in message
    ]


@_REQUIRES_UDS
async def test_close_during_pending_read_stays_quiet() -> None:
    install_socket_teardown_guard()
    with tempfile.TemporaryDirectory(prefix="omni-uds-guard-") as tmp:
        delivered = await _close_during_pending_read(Path(tmp), 5)
    assert not _invalid_state_deliveries(delivered)


@_REQUIRES_UDS
async def test_close_during_pending_write_stays_quiet() -> None:
    install_socket_teardown_guard()
    with tempfile.TemporaryDirectory(prefix="omni-uds-guard-") as tmp:
        delivered = await _close_during_pending_write(Path(tmp), 5)
    assert not _invalid_state_deliveries(delivered)


async def _upstream_guarded_aclose(self: Any) -> None:
    """``aclose`` as anyio 4.15 ships it: guarded itself, readiness callbacks still bare."""
    if not self._closing:
        self._closing = True
        if self._raw_socket.fileno() != -1:
            self._raw_socket.close()
        if self._receive_future and not self._receive_future.done():
            self._receive_future.set_result(None)
        if self._send_future and not self._send_future.done():
            self._send_future.set_result(None)


@_REQUIRES_UDS
async def test_waiter_guard_alone_quiets_close_during_read(monkeypatch) -> None:
    from anyio._backends import _asyncio as anyio_asyncio

    install_socket_teardown_guard()
    monkeypatch.setattr(anyio_asyncio._RawSocketMixin, "aclose", _upstream_guarded_aclose)
    with tempfile.TemporaryDirectory(prefix="omni-uds-guard-") as tmp:
        delivered = await _close_during_pending_read(Path(tmp), 5)
    assert not _invalid_state_deliveries(delivered)


@_REQUIRES_UDS
async def test_waiter_guard_alone_quiets_close_during_write(monkeypatch) -> None:
    from anyio._backends import _asyncio as anyio_asyncio

    install_socket_teardown_guard()
    monkeypatch.setattr(anyio_asyncio._RawSocketMixin, "aclose", _upstream_guarded_aclose)
    with tempfile.TemporaryDirectory(prefix="omni-uds-guard-") as tmp:
        delivered = await _close_during_pending_write(Path(tmp), 5)
    assert not _invalid_state_deliveries(delivered)


@_REQUIRES_UDS
async def test_stream_roundtrip_still_works_after_guard() -> None:
    install_socket_teardown_guard()
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory(prefix="omni-uds-guard-") as tmp:
        stream, peer, server = await _connected_uds_pair(str(Path(tmp) / "rt.sock"))

        # Park the read first so the patched _wait_until_readable path runs.
        recv_task = asyncio.ensure_future(stream.receive())
        await _wait_until_parked(stream, "_receive_future")
        await loop.sock_sendall(peer, b"ping")
        assert await recv_task == b"ping"

        await stream.send(b"pong")
        assert await loop.sock_recv(peer, 16) == b"pong"

        await stream.aclose()
        peer.close()
        server.close()


async def test_install_is_idempotent(monkeypatch) -> None:
    from anyio._backends import _asyncio as anyio_asyncio

    install_socket_teardown_guard()
    mixin = anyio_asyncio._RawSocketMixin
    before = (mixin._wait_until_readable, mixin._wait_until_writable, mixin.aclose)
    monkeypatch.setattr(socket_teardown_guard, "_installed", False)
    install_socket_teardown_guard()
    assert (mixin._wait_until_readable, mixin._wait_until_writable, mixin.aclose) == before


class _BareWaiters:
    """Readiness waiters as anyio registers them: ``f.set_result`` straight on the loop."""

    def _wait_until_readable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
        f = self._receive_future = asyncio.Future()
        loop.add_reader(self._raw_socket, f.set_result, None)
        return f

    def _wait_until_writable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
        f = self._send_future = asyncio.Future()
        loop.add_writer(self._raw_socket, f.set_result, None)
        return f


class _UnguardedBackend(_BareWaiters):
    """anyio before 4.15: ``aclose`` completes the futures blindly."""

    async def aclose(self) -> None:
        if self._receive_future:
            self._receive_future.set_result(None)


class _GuardedCloseBackend(_BareWaiters):
    """anyio 4.15: ``aclose`` checks ``done()`` but the callbacks are still bare."""

    async def aclose(self) -> None:
        if self._receive_future and not self._receive_future.done():
            self._receive_future.set_result(None)


class _FullyGuardedBackend:
    def _wait_until_readable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
        f: asyncio.Future[None] = asyncio.Future()
        loop.add_reader(self._raw_socket, lambda: f.done() or f.set_result(None))
        return f

    def _wait_until_writable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
        f: asyncio.Future[None] = asyncio.Future()
        loop.add_writer(self._raw_socket, lambda: f.done() or f.set_result(None))
        return f

    async def aclose(self) -> None:
        if self._receive_future and not self._receive_future.done():
            self._receive_future.set_result(None)


class _RenamedWaiters:
    """Bare callbacks under a renamed local: the shim cannot tell whether they are safe."""

    def _wait_until_readable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
        fut = self._receive_future = asyncio.Future()
        loop.add_reader(self._raw_socket, fut.set_result, None)
        return fut

    def _wait_until_writable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
        fut = self._send_future = asyncio.Future()
        loop.add_writer(self._raw_socket, fut.set_result, None)
        return fut

    async def aclose(self) -> None:
        if self._receive_future and not self._receive_future.done():
            self._receive_future.set_result(None)


def test_unguarded_backend_patches_every_method() -> None:
    backend = type("Backend", (_UnguardedBackend,), {})
    patched = socket_teardown_guard._patch_unguarded_methods(backend)
    assert patched == ("_wait_until_readable", "_wait_until_writable", "aclose")
    assert backend.aclose is socket_teardown_guard._aclose


def test_guarded_close_backend_still_patches_waiters() -> None:
    backend = type("Backend", (_GuardedCloseBackend,), {})
    patched = socket_teardown_guard._patch_unguarded_methods(backend)
    assert patched == ("_wait_until_readable", "_wait_until_writable")
    assert backend._wait_until_readable is socket_teardown_guard._wait_until_readable
    assert backend._wait_until_writable is socket_teardown_guard._wait_until_writable
    assert backend.aclose is _GuardedCloseBackend.aclose


def test_fully_guarded_backend_is_left_alone() -> None:
    backend = type("Backend", (_FullyGuardedBackend,), {})
    assert socket_teardown_guard._patch_unguarded_methods(backend) == ()


def test_unrecognized_waiter_shape_is_reported_not_patched(caplog) -> None:
    backend = type("Backend", (_RenamedWaiters,), {})
    with caplog.at_level(logging.INFO, logger=socket_teardown_guard.__name__):
        assert socket_teardown_guard._patch_unguarded_methods(backend) == ()
    assert backend._wait_until_readable is _RenamedWaiters._wait_until_readable
    assert any(
        "_wait_until_readable, _wait_until_writable" in record.getMessage()
        for record in caplog.records
    )


async def test_create_uds_client_installs_guard(monkeypatch) -> None:
    from omnigent.runner.transports import uds

    calls: list[bool] = []
    monkeypatch.setattr(uds, "install_socket_teardown_guard", lambda: calls.append(True))
    client = uds.create_uds_client("/tmp/omni-guard-unused.sock")
    await client.aclose()
    assert calls


async def test_build_uds_runner_installs_guard(monkeypatch) -> None:
    from omnigent.server import _runner_transport

    calls: list[bool] = []
    monkeypatch.setattr(
        _runner_transport, "install_socket_teardown_guard", lambda: calls.append(True)
    )
    client, _ws_factory = _runner_transport.build_uds_runner("/tmp/omni-guard-unused.sock")
    await client.aclose()
    assert calls


def test_harness_endpoint_transport_installs_guard(monkeypatch) -> None:
    from omnigent.runtime.harnesses import process_manager

    calls: list[bool] = []
    monkeypatch.setattr(
        process_manager, "install_socket_teardown_guard", lambda: calls.append(True)
    )
    endpoint = process_manager._HarnessEndpoint(socket_path=Path("/tmp/omni-guard-unused.sock"))
    endpoint.make_transport()
    assert calls
