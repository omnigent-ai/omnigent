"""anyio raw-socket teardown guard: close-during-read stays quiet, streams still work."""

from __future__ import annotations

import asyncio
import socket
import tempfile
from pathlib import Path

import anyio

from omnigent.util import socket_teardown_guard
from omnigent.util.socket_teardown_guard import install_socket_teardown_guard

_INVALID_STATE_MESSAGE = "Exception in callback Future.set_result(None)"


async def _connected_uds_pair(
    socket_path: str,
) -> tuple[object, socket.socket, socket.socket]:
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


async def _park_pending_read(stream: object) -> asyncio.Task[None]:
    """Start a ``receive()`` and spin until it parks on ``add_reader``."""

    async def _receive() -> None:
        try:
            await stream.receive()  # type: ignore[attr-defined]
        except Exception:
            pass

    task = asyncio.ensure_future(_receive())
    for _ in range(50):
        await asyncio.sleep(0)
        if getattr(stream, "_receive_future", None) is not None:
            break
    return task


async def test_close_during_pending_read_stays_quiet() -> None:
    install_socket_teardown_guard()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    delivered: list[tuple[str, BaseException | None]] = []

    def _capture(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        exc = context.get("exception")
        message = str(context.get("message") or "")
        delivered.append((message, exc if isinstance(exc, BaseException) else None))

    loop.set_exception_handler(_capture)
    try:
        with tempfile.TemporaryDirectory(prefix="omni-uds-guard-") as tmp:
            for i in range(5):
                stream, peer, server = await _connected_uds_pair(str(Path(tmp) / f"s{i}.sock"))
                recv_task = await _park_pending_read(stream)
                # Peer EOF queues the reader callback; aclose() on the same
                # stretch used to double-complete the reader future.
                peer.close()
                aclose_task = asyncio.ensure_future(stream.aclose())
                for _ in range(4):
                    await asyncio.sleep(0)
                for task in (recv_task, aclose_task):
                    task.cancel()
                    try:
                        await task
                    except Exception:
                        pass
                server.close()
    finally:
        loop.set_exception_handler(previous_handler)

    invalid_state = [
        (message, exc)
        for message, exc in delivered
        if isinstance(exc, asyncio.InvalidStateError) and _INVALID_STATE_MESSAGE in message
    ]
    assert not invalid_state


async def test_stream_roundtrip_still_works_after_guard() -> None:
    install_socket_teardown_guard()
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory(prefix="omni-uds-guard-") as tmp:
        stream, peer, server = await _connected_uds_pair(str(Path(tmp) / "rt.sock"))

        # Park the read first so the patched _wait_until_readable path runs.
        recv_task = asyncio.ensure_future(stream.receive())  # type: ignore[attr-defined]
        await asyncio.sleep(0.05)
        await loop.sock_sendall(peer, b"ping")
        assert await recv_task == b"ping"

        await stream.send(b"pong")  # type: ignore[attr-defined]
        assert await loop.sock_recv(peer, 16) == b"pong"

        await stream.aclose()  # type: ignore[attr-defined]
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


def test_already_guarded_shape_is_left_alone() -> None:
    class _GuardedSocket:
        async def aclose(self) -> None:
            future: asyncio.Future[None] | None = None
            if future is not None and not future.done():
                future.set_result(None)

        def _wait_until_readable(self, _loop: object) -> None: ...

        def _wait_until_writable(self, _loop: object) -> None: ...

    assert not socket_teardown_guard._has_unguarded_teardown(_GuardedSocket)


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
