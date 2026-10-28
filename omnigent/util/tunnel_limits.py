"""Size limits and keepalive budget shared by the omnigent WebSocket tunnels.

Canonical home for the tunnel budget, read by all three sides: the server that
terminates the tunnels (``omnigent server``, the OSS Docker entrypoint, and any
launcher that serves ``create_app`` under its own ``uvicorn.run``), the runner
that dials in, and the host. It lives outside ``server/``, ``runner/`` and
``host/`` because none of them owns it — the wire protocol does.
"""

from __future__ import annotations

from typing import TypedDict

RUNNER_TUNNEL_MAX_MESSAGE_BYTES = 100 * 1024 * 1024

# Protocol-level WebSocket keepalive budget for the runner<->server tunnel —
# the websockets library's own PING/PONG (set on the runner's ``connect`` and the
# server's uvicorn). This is DISTINCT from, and a backstop to, the app-level
# liveness loop the server runs (``_ping_loop``: ``PING_INTERVAL_S=30`` x
# ``PING_MISS_THRESHOLD=3`` = 90 s before a peer is declared dead).
#
# Protocol PING/PONG budget: must not be tighter than the server's app-level
# liveness window (PING_INTERVAL_S * PING_MISS_THRESHOLD = 90 s), or healthy-but-busy
# tunnels get dropped mid-stall. test_tunnel_limits.py enforces the >= invariant.
TUNNEL_KEEPALIVE_PING_INTERVAL_S = 30.0
TUNNEL_KEEPALIVE_PING_TIMEOUT_S = 90.0

# Two missed application heartbeats recover idle/subscription-only tunnels
# within an old server's 90-second grace. Request-owned work keeps its tolerance.
RUNNER_TUNNEL_SILENCE_TIMEOUT_S = 2 * TUNNEL_KEEPALIVE_PING_INTERVAL_S


class UvicornTunnelKwargs(TypedDict):
    """The uvicorn settings that terminate omnigent tunnels; see :func:`uvicorn_tunnel_kwargs`."""

    ws_max_size: int
    ws_ping_interval: float
    ws_ping_timeout: float


def uvicorn_tunnel_kwargs() -> UvicornTunnelKwargs:
    """
    Return the uvicorn settings a server needs to terminate omnigent tunnels.

    Every launcher that serves :func:`omnigent.server.app.create_app` must spread
    these into its ``uvicorn.run`` / ``uvicorn.Config``, or uvicorn's defaults
    apply: a 16 MiB frame cap and a 20 s/20 s keepalive that closes a
    busy-but-healthy tunnel with ``1011 keepalive ping timeout`` after a ~20 s
    client-path stall, while the runner and host tolerate 90 s.

    Uvicorn reads none of this from the environment (``UVICORN_WS_*`` resolves
    only through its own click CLI, which these launchers bypass), so passing
    them in code is the only way to apply the budget.

    :returns: Keyword arguments for ``uvicorn.run`` / ``uvicorn.Config``, e.g.
        ``{"ws_max_size": 104857600, "ws_ping_interval": 30.0,
        "ws_ping_timeout": 90.0}``.
    """
    return UvicornTunnelKwargs(
        ws_max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        ws_ping_interval=TUNNEL_KEEPALIVE_PING_INTERVAL_S,
        ws_ping_timeout=TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
    )
