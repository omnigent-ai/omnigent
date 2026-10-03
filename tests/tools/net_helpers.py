"""Loopback helpers for tests that spawn a local MCP server subprocess."""

from __future__ import annotations

import socket
import time


def free_port() -> int:
    """Reserve an ephemeral localhost port and return it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_listen(port: int, timeout_s: float = 30.0) -> None:
    """Poll until ``127.0.0.1:port`` accepts a TCP connection."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout_s}s")
