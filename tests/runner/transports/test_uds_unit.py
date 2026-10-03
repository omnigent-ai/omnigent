"""Unit tests for UDS transport helper functions (no subprocess spawning).

Tests the pure-logic helpers in ``omnigent.runner.transports.uds``:
socket probing, client factory, path construction, and subprocess
configuration — all without launching real uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import sys
import tempfile

import anyio
import httpx
import pytest

from omnigent.runner import _entry
from omnigent.runner.transports.uds import (
    RunnerSubprocess,
    _is_socket_listening,
    create_uds_client,
)

_REQUIRES_UDS = pytest.mark.skipif(
    sys.platform == "win32", reason="Unix domain sockets are POSIX-only"
)


# ── _is_socket_listening ────────────────────────────────


@_REQUIRES_UDS
def test_is_socket_listening_returns_false_for_nonexistent_path() -> None:
    """No socket file means not listening."""
    assert _is_socket_listening("/tmp/no-such-socket-ever.sock") is False


@_REQUIRES_UDS
def test_is_socket_listening_returns_false_for_regular_file(tmp_path) -> None:
    """A regular file at the path is not a listening socket."""
    fake = tmp_path / "not-a-socket.sock"
    fake.write_text("not a socket")
    assert _is_socket_listening(str(fake)) is False


@_REQUIRES_UDS
def test_is_socket_listening_returns_true_for_bound_socket() -> None:
    """A bound and listening UDS is detected."""
    with tempfile.TemporaryDirectory(prefix="uds-test-") as tdir:
        sock_path = os.path.join(tdir, "test.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        try:
            assert _is_socket_listening(sock_path) is True
        finally:
            server.close()


@_REQUIRES_UDS
def test_is_socket_listening_returns_false_for_unbound_socket_file() -> None:
    """A socket file that exists but nothing is listening on it."""
    with tempfile.TemporaryDirectory(prefix="uds-test-") as tdir:
        sock_path = os.path.join(tdir, "stale.sock")
        # Create a socket file then close it without listening.
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.close()
        # The file exists but no one is listening.
        assert _is_socket_listening(sock_path) is False


# ── create_uds_client ──────────────────────────────────


@_REQUIRES_UDS
@pytest.mark.asyncio
async def test_create_uds_client_returns_async_client() -> None:
    """Factory returns a correctly-configured httpx.AsyncClient."""
    client = create_uds_client("/tmp/fake.sock")
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert str(client.base_url) == "http://runner"
    finally:
        await client.aclose()


@_REQUIRES_UDS
@pytest.mark.asyncio
async def test_create_uds_client_custom_base_url() -> None:
    """Custom base_url is reflected in the client."""
    client = create_uds_client("/tmp/fake.sock", base_url="http://my-runner")
    try:
        assert str(client.base_url) == "http://my-runner"
    finally:
        await client.aclose()


# ── RunnerSubprocess config ─────────────────────────────


def test_runner_subprocess_defaults() -> None:
    """Default field values are sensible."""
    sub = RunnerSubprocess()
    assert sub.socket_path is None
    assert sub.startup_timeout_s == 30.0
    assert sub._process is None
    assert sub._tmp_dir is None


def test_runner_subprocess_kill_noop_when_no_process() -> None:
    """_kill is safe to call before __enter__."""
    sub = RunnerSubprocess()
    sub._kill()  # Should not raise.


def test_runner_subprocess_exit_cleans_tmp_dir() -> None:
    """__exit__ cleans up the temporary directory even without a process."""
    sub = RunnerSubprocess()
    sub._tmp_dir = tempfile.TemporaryDirectory(prefix="test-cleanup-")
    tmp_name = sub._tmp_dir.name
    assert os.path.isdir(tmp_name)
    sub.__exit__(None, None, None)
    assert not os.path.exists(tmp_name)


# ── UDS teardown race ────────────────────────────────────────────

# anyio completes a parked read's future from both the selector callback and
# ``aclose()``; a stream readable on the tick it closes double-completes it and
# the runner logs the resulting InvalidStateError via ``_handle_loop_exception``.


async def _count_invalid_state_on_uds_teardown(iterations: int) -> int:
    """Drive the readable-on-close UDS teardown race and count the
    InvalidStateError failures the runner's loop handler attributes to it."""

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    cap = _Capture()
    logger = logging.getLogger(_entry.__name__)
    logger.addHandler(cap)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    loop = asyncio.get_running_loop()
    prior_handler = loop.get_exception_handler()
    loop.set_exception_handler(_entry._handle_loop_exception)
    try:
        for i in range(iterations):
            with tempfile.TemporaryDirectory(prefix="uds-teardown-") as tdir:
                await _drive_teardown_once(os.path.join(tdir, f"u{i}.sock"), loop)
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(prior_handler)
        logger.removeHandler(cap)
        logger.setLevel(prior_level)

    def _attr(record: logging.LogRecord, key: str) -> object:
        attrs = getattr(record, "attributes", None)
        return attrs.get(key) if isinstance(attrs, dict) else None

    return sum(
        1
        for r in cap.records
        if r.levelno >= logging.ERROR and _attr(r, "exception_type") == "InvalidStateError"
    )


async def _drive_teardown_once(path: str, loop: asyncio.AbstractEventLoop) -> None:
    listener = await anyio.create_unix_listener(path)
    server_box: dict[str, object] = {}
    async with listener, anyio.create_task_group() as tg:

        async def _accept() -> None:
            server_box["stream"] = await listener.accept()

        tg.start_soon(_accept)
        client_stream = await anyio.connect_unix(path)
        while "stream" not in server_box:
            await asyncio.sleep(0)
        server_stream = server_box["stream"]

        # Park a read, then make the fd readable with no intervening await so
        # the reader callback stays queued for the same tick the stream closes.
        client_stream._wait_until_readable(loop)
        server_stream._raw_socket.send(b"x")

        def _close_now() -> None:
            coro = client_stream.aclose()
            try:
                coro.send(None)  # aclose sets the result before its first await
            except StopIteration:
                return
            coro.close()

        loop.call_soon(_close_now)
        await asyncio.sleep(0)

        with contextlib.suppress(Exception):
            await server_stream.aclose()
        tg.cancel_scope.cancel()


@_REQUIRES_UDS
def test_uds_teardown_does_not_double_complete_receive_future() -> None:
    """A UDS stream torn down while a read is parked must not fire
    ``set_result`` twice on its receive future."""

    async def _run() -> int:
        # Building the runner's UDS client is the product path that installs
        # the process-wide teardown guard.
        with tempfile.TemporaryDirectory(prefix="uds-teardown-") as tdir:
            await create_uds_client(os.path.join(tdir, "runner.sock")).aclose()
        return await _count_invalid_state_on_uds_teardown(20)

    invalid_state = asyncio.run(_run())
    assert invalid_state == 0, (
        f"{invalid_state}/20 UDS teardowns raised InvalidStateError: a parked read's "
        "set_result reader double-completed the receive future on close"
    )
