"""Deprecated alias for :mod:`omnigent.util.tunnel_limits`; removal target 0.16.0.

The budget moved out of the runner package because the server and the host read
it too. Kept importable for the copy of omnigent vendored into universe, whose
launcher imports this path and cannot name the new one until its next resync.
No in-tree module imports this — new code imports :mod:`omnigent.util.tunnel_limits`.
"""

from __future__ import annotations

from omnigent.util.tunnel_limits import (
    RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
    TUNNEL_KEEPALIVE_PING_INTERVAL_S,
    TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
    UvicornTunnelKwargs,
    uvicorn_tunnel_kwargs,
)

__all__ = [
    "RUNNER_TUNNEL_MAX_MESSAGE_BYTES",
    "TUNNEL_KEEPALIVE_PING_INTERVAL_S",
    "TUNNEL_KEEPALIVE_PING_TIMEOUT_S",
    "UvicornTunnelKwargs",
    "uvicorn_tunnel_kwargs",
]
