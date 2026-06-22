"""Tests for omnigent.inner.egress.relay (TCP-to-Unix bridge)."""

from __future__ import annotations

import asyncio
import shutil
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.inner.egress.relay import start_relay


def _pick_free_port() -> int:
    """Return a free ephemeral TCP port. Hardcoded ports collide with TIME_WAIT."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def short_tmp_parent() -> Iterator[Path]:
    """Short-pathed tmpdir under ``/tmp``. ``tmp_path`` overflows AF_UNIX on macOS."""
    parent = Path("/tmp") / f"omni-relay-{uuid.uuid4().hex[:8]}"
    parent.mkdir(mode=0o700)
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_relay_bridges_tcp_to_unix(short_tmp_parent: Path) -> None:
    """Data sent to the relay's TCP port arrives at the Unix socket."""
    sock_path = short_tmp_parent / "test.sock"
    received: list[bytes] = []
    server_ready = asyncio.Event()

    async def _echo_server() -> None:
        """Echo server on the Unix socket (simulates parent proxy)."""

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(4096)
            received.append(data)
            writer.write(b"ECHO:" + data)
            await writer.drain()
            writer.close()

        srv = await asyncio.start_unix_server(_handle, str(sock_path))
        server_ready.set()
        async with srv:
            await srv.serve_forever()

    # Start the echo server on the Unix socket
    server_task = asyncio.create_task(_echo_server())
    await asyncio.wait_for(server_ready.wait(), timeout=5)

    # Start the relay (background daemon thread). The returned event
    # fires once the relay's asyncio.start_server() has bound the TCP
    # listener, so no time-based sleep is needed.
    relay_port = _pick_free_port()
    relay_ready = start_relay(relay_port, str(sock_path))
    loop = asyncio.get_running_loop()
    fired = await loop.run_in_executor(None, relay_ready.wait, 10.0)
    assert fired, "Relay thread did not signal ready within 10s"

    try:
        # Connect to the relay via TCP and send data
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"HELLO FROM CLIENT")
        await writer.drain()
        writer.write_eof()

        # ``read(-1)`` reads until EOF rather than a single chunk,
        # which is what the test actually wants. Safety-net 30s
        # timeout in case forwarding is genuinely broken.
        response = await asyncio.wait_for(reader.read(-1), timeout=30)
        writer.close()

        # The echo server received our data
        assert received == [b"HELLO FROM CLIENT"], (
            "Data from TCP client did not reach the Unix socket server"
        )
        # The relay forwarded the response back to the TCP client
        assert response == b"ECHO:HELLO FROM CLIENT", (
            "Response from Unix socket did not reach the TCP client"
        )
    finally:
        server_task.cancel()
        try:  # noqa: SIM105
            await server_task
        except asyncio.CancelledError:
            pass


def test_c1_start_relay_raises_when_port_already_bound(
    short_tmp_parent: Path,
) -> None:
    """
    C1: ``start_relay`` MUST raise :class:`OSError` synchronously
    when the requested TCP port is already bound by another
    process. Pre-fix the bind happened in the background thread
    and any error was silently logged; helper traffic then flowed
    to whatever was already on the port (same-user-process MITM
    risk on macOS where there's no network namespace isolation).

    This is the LOAD-BEARING defense after the ``Proxy-Authorization``
    mechanism was removed (S3) — the only thing preventing a same-
    host attacker from MITM-ing helper egress is the relay refusing
    to start when something else already owns the port.
    """
    sock_path = short_tmp_parent / "test.sock"
    squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        squat.bind(("127.0.0.1", 0))
        squat.listen(1)
        bound_port = squat.getsockname()[1]
        with pytest.raises(OSError) as exc:
            start_relay(bound_port, str(sock_path))
        msg = str(exc.value)
        assert "port-squat" in msg or "bind" in msg.lower(), (
            "OSError on bind collision should mention port-squat or "
            "bind in the message so the operator can diagnose."
        )
    finally:
        squat.close()


@pytest.mark.asyncio
async def test_s3_relay_forwards_plain_connection_without_proxy_auth(
    short_tmp_parent: Path,
) -> None:
    """
    S3: after the ``Proxy-Authorization`` mechanism was removed, the
    relay must forward connections that carry no auth header — the
    parent no longer embeds a token in the ``HTTP_PROXY`` URL so
    requiring one would block every legitimate helper request.

    This is the explicit positive regression. The previous revision
    had two tests that the relay REJECTED unauth'd connections with
    HTTP 407 and STRIPPED the auth header on forwarding; both
    behaviors were removed when the token left the system. See the
    docstring at :func:`omnigent.inner.egress.relay.start_relay`
    for why removing the token is strictly safer than keeping it
    (it leaked via argv to any same-UID process).
    """
    sock_path = short_tmp_parent / "test.sock"
    upstream_received: list[bytes] = []

    async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(4096)
        upstream_received.append(data)
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await writer.drain()
        writer.close()

    srv = await asyncio.start_unix_server(_upstream, str(sock_path))
    try:
        port = _pick_free_port()
        ready = start_relay(port, str(sock_path))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ready.wait, 5.0)
        # Connect with NO Proxy-Authorization header; pre-S3 this
        # would have gotten a 407.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
        assert response.startswith(b"HTTP/1.1 200"), (
            f"Unauthenticated CONNECT got {response[:60]!r}; expected "
            "200 OK now that the Proxy-Authorization gate was removed."
        )
        assert len(upstream_received) == 1, (
            "Relay should have forwarded the unauth'd CONNECT to upstream exactly once."
        )
    finally:
        srv.close()
        await srv.wait_closed()
