"""Size limits and keepalive budget shared by the runner WebSocket tunnel endpoints."""

from __future__ import annotations

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
# tunnel. ``test_limits.py`` asserts the >= invariant so a future tightening below
# the app-level budget fails CI.
TUNNEL_KEEPALIVE_PING_INTERVAL_S = 30.0
TUNNEL_KEEPALIVE_PING_TIMEOUT_S = 90.0
