"""Unit tests for the grok-native serve bind-readiness probe."""

from __future__ import annotations

import socket

from omnigent.harnesses.grok_native.probe import wait_for_grok_serve


async def test_wait_for_serve_returns_once_bound() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        await wait_for_grok_serve(port, timeout_s=2.0, poll_s=0.05)
