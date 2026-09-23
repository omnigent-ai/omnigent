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
# It MUST NOT be tighter than that 90 s app-level budget. Left unset, the library
# default is 20 s/20 s — 4.5x stricter — so a *healthy* tunnel is dropped with
# ``1011 keepalive ping timeout`` the instant its event loop is stalled for ~20 s
# (a synchronous/CPU-bound dispatch), pre-empting the deliberate 90 s policy and
# triggering reconnect churn + relay-subscribe timeouts. See issue #1116.
#
# A PING every 30 s keeps the connection warm and is the runner's ONLY detector
# of a silently-dead server (the app-level ``_ping_loop`` only runs server->client),
# while the 90 s PONG timeout tolerates loop stalls up to the same window the
# app-level loop already allows. A peer that dies right after a successful PONG is
# detected at worst ~120 s later (30 s interval before the next PING + 90 s waiting
# for its PONG) — the deliberate tradeoff for not false-dropping a busy-but-healthy
# tunnel. ``test_tunnel_limits.py`` asserts the >= invariant so a future tightening
# below the app-level budget fails CI.
TUNNEL_KEEPALIVE_PING_INTERVAL_S = 30.0
TUNNEL_KEEPALIVE_PING_TIMEOUT_S = 90.0


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
