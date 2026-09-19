"""UDS teardown must not raise InvalidStateError into the event loop.

The runner talks to harness subprocesses over a Unix socket via
``httpx.AsyncHTTPTransport(uds=...)`` (``omnigent/server/_runner_transport.py``,
``omnigent/runner/transports/uds.py``, ``omnigent/runtime/harnesses/process_manager.py``).
That transport wraps anyio's asyncio ``UNIXSocketStream``. When a socket read
is parked (``_wait_until_readable`` has registered ``add_reader(sock,
f.set_result, None)``) and the stream is closed at the same event-loop tick the
socket becomes readable, anyio's ``_RawSocketMixin.aclose()`` calls
``set_result(None)`` on the already-queued reader future without a ``done()``
guard. The reader callback then runs ``Future.set_result(None)`` on a
completed future, raising ``InvalidStateError`` inside an asyncio callback,
which the runner's loop exception handler logs as
``asyncio: Exception in callback Future.set_result(None)`` (ERROR).

A benign socket teardown must not raise an unhandled exception into the event
loop. Building the runner's UDS client installs the process-wide teardown
guard, so this drives the same teardown-during-read the runner's UDS transport
performs and asserts the loop exception handler receives no such
``InvalidStateError``. It fails while the double ``set_result`` is possible
and passes once the guard prevents it.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_uds_teardown_invalid_state_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
from pathlib import Path

import anyio

from omnigent.runner.transports.uds import create_uds_client

# The exact asyncio callback signature the runner logs at ERROR.
_INVALID_STATE_MESSAGE = "Exception in callback Future.set_result(None)"


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


async def _drive_uds_teardown_race(socket_path: str) -> None:
    """Reproduce a UDS teardown that races a pending read on the same tick."""
    loop = asyncio.get_running_loop()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)
    server.setblocking(False)

    accept_task = asyncio.ensure_future(loop.sock_accept(server))
    stream = await anyio.connect_unix(socket_path)
    peer, _ = await accept_task

    recv_task = await _park_pending_read(stream)

    # Peer EOF makes the fd readable; closing the stream on the same stretch
    # races the loop's already-queued reader callback against aclose().
    peer.close()
    aclose_task = asyncio.ensure_future(stream.aclose())
    for _ in range(4):
        await asyncio.sleep(0)

    for task in (recv_task, aclose_task, accept_task):
        task.cancel()
        try:
            await task
        except Exception:
            pass

    server.close()
    try:
        os.unlink(socket_path)
    except OSError:
        pass


async def test_uds_teardown_does_not_raise_invalid_state_callback() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    delivered: list[tuple[str, BaseException | None]] = []

    def _capture(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        exc = context.get("exception")
        message = str(context.get("message") or "")
        delivered.append((message, exc if isinstance(exc, BaseException) else None))

    loop.set_exception_handler(_capture)
    try:
        with tempfile.TemporaryDirectory(prefix="omni-uds-teardown-") as tmp:
            # The product path under test: creating the runner's UDS client
            # installs the anyio teardown guard for the whole process.
            client = create_uds_client(str(Path(tmp) / "runner.sock"))
            await client.aclose()
            for i in range(20):
                await _drive_uds_teardown_race(str(Path(tmp) / f"s{i}.sock"))
    finally:
        loop.set_exception_handler(previous_handler)

    invalid_state = [
        (message, exc)
        for message, exc in delivered
        if isinstance(exc, asyncio.InvalidStateError)
        and _INVALID_STATE_MESSAGE in message
    ]
    assert not invalid_state, (
        "UDS teardown raised InvalidStateError into the loop exception handler "
        f"(the runner logs this as an ERROR): {invalid_state[:3]}"
    )
