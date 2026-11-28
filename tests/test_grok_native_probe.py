"""Unit tests for the grok-native serve bind-readiness probe."""

from __future__ import annotations

import socket

import pytest

from omnigent.harnesses.grok_native.probe import (
    allocate_grok_port,
    wait_for_grok_serve,
)


def test_allocate_port_returns_bindable_port() -> None:
    port = allocate_grok_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


async def test_wait_for_serve_times_out_on_dead_port() -> None:
    with pytest.raises(TimeoutError):
        await wait_for_grok_serve(allocate_grok_port(), timeout_s=0.2, poll_s=0.05)


async def test_wait_for_serve_returns_once_bound() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        await wait_for_grok_serve(port, timeout_s=2.0, poll_s=0.05)
