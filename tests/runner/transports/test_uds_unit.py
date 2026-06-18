"""Unit tests for UDS transport helper functions (no subprocess spawning).

Tests the pure-logic helpers in ``omnigent.runner.transports.uds``:
socket probing, client factory, path construction, and subprocess
configuration — all without launching real uvicorn.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile

import httpx
import pytest

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
