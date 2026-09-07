"""
Process-wide "this server is shutting down" signal.

A server going down closes every runner tunnel itself, and the runners
cannot reconnect to a process that stopped listening. The disconnect
reconciliation paths — the per-runner grace timer in ``omnigent.server.app``
and the relay give-up in the sessions orchestration — would otherwise read
that self-inflicted loss as "the runner is gone" and fail every mid-turn
session, which is what a rolling deploy did.

Two sources set the signal:

* Explicit: an entrypoint that owns the uvicorn ``Server`` (``omnigent
  server``) calls :func:`mark_server_shutting_down` from its ``shutdown()``
  override, before the tunnels close.
* In-band: uvicorn closes WebSockets with close code 1012 ("service
  restart") only while shutting down, so a runner-tunnel disconnect carrying
  that code proves the close was ours. The tunnel route reports every close
  code through :func:`note_tunnel_close_code`, which covers entrypoints that
  run a bare ``uvicorn.run(app)`` (the Databricks Apps wrapper).

The mark is process-wide: while it is fresh, disconnect reconciliation is
suppressed for every runner, so a genuine runner death inside that window
surfaces through liveness (the offline dot) rather than a ``failed`` card.
That is why it is time-bounded rather than latched for the life of the
process: a server that really is shutting down is gone within its platform's
termination grace (seconds), so the window never limits it, while a spurious
mark (a client that closed with 1012, which runners never send — they close
with 1001) costs at most :data:`SHUTDOWN_WINDOW_S` of suppression.
"""

from __future__ import annotations

import time

# uvicorn's shutdown close code (RFC 6455 "service restart"); a server is
# the only peer that sends it.
SERVER_INITIATED_CLOSE_CODES: frozenset[int] = frozenset({1012})

# How long a mark counts as "still shutting down". Platform termination
# graces are far shorter (Databricks Apps: 15s; Kubernetes default: 30s),
# and the reconciliation deadlines this suppresses fire ~10s after the drop.
SHUTDOWN_WINDOW_S: float = 60.0

_marked_at: float | None = None


def mark_server_shutting_down() -> None:
    """
    Record that this server process is shutting down now.
    """
    global _marked_at
    _marked_at = time.monotonic()


def note_tunnel_close_code(code: int | None) -> bool:
    """
    Record a runner-tunnel close code; mark shutdown when the server sent it.

    :param code: WebSocket close code observed on a runner tunnel, or
        ``None`` when unknown.
    :returns: ``True`` when the code identifies a server-initiated close and
        the shutdown mark was set.
    """
    if code in SERVER_INITIATED_CLOSE_CODES:
        mark_server_shutting_down()
        return True
    return False


def server_shutting_down(now: float | None = None) -> bool:
    """
    Return whether a shutdown mark was set within :data:`SHUTDOWN_WINDOW_S`.

    :param now: Monotonic clock reading to measure against; defaults to
        :func:`time.monotonic`.
    :returns: ``True`` while the mark is fresh.
    """
    if _marked_at is None:
        return False
    current = time.monotonic() if now is None else now
    return current - _marked_at < SHUTDOWN_WINDOW_S


def reset_for_tests() -> None:
    """
    Clear the shutdown mark (test isolation).
    """
    global _marked_at
    _marked_at = None
