"""
Process-wide "this server is shutting down" signal.

A server going down closes every runner tunnel itself, and the runners
cannot reconnect to a process that stopped listening. The disconnect
reconciliation paths — the per-runner grace timer in ``omnigent.server.app``
and the relay give-up in the sessions orchestration — would otherwise read
that self-inflicted loss as "the runner is gone" and fail every mid-turn
session, which is what a rolling deploy did.

Two sources set the signal:

* Explicit: every in-tree launcher serves the app through
  :class:`omnigent.server.graceful_shutdown.ShutdownSignalingServer`, whose
  ``shutdown()`` override calls :func:`mark_server_shutting_down` before the
  tunnels close (``omnigent server`` and the Docker/Databricks entrypoints).
* In-band: uvicorn closes WebSockets with close code 1012 ("service
  restart") only while shutting down, so a runner-tunnel disconnect carrying
  that code proves the close was ours. The tunnel route reports every close
  code through :func:`note_tunnel_close_code`, a backstop for any launcher
  that serves the app with a bare ``uvicorn.run(app)``.

The mark is process-wide: while it is fresh, disconnect reconciliation is
suppressed for every runner, so a genuine runner death inside that window
surfaces through liveness (the offline dot) rather than a ``failed`` card.
That is why it is time-bounded rather than latched for the life of the
process: a spurious mark (a client that closed with 1012, which runners never
send — they close with 1001) costs at most :data:`SHUTDOWN_WINDOW_S` of
suppression. A draining replica can outlive its tunnels by more than the
runner-disconnect grace, so the window must cover the whole grace: the
reconciliation it suppresses only decides once that grace runs out.

A per-runner variant covers a replica that is *not* shutting down but
deliberately hands one runner's tunnel to another replica, e.g. a deployment
that rebalances runner tunnels across replicas during a rollout. The same
reasoning applies to just that runner: this replica caused the loss, the
runner re-tunnels to its new replica, and that replica — not this one —
decides whether its turn failed. A caller can set it explicitly with
:func:`mark_runner_handed_off`; the tunnel registry also reports every live
tunnel it retires on this server's behalf (the 1001 "tunnel retired by
server" close) through :func:`note_runner_tunnel_retired`, so a retire path
that never marks first is still covered.
"""

from __future__ import annotations

import logging
import time
from typing import TypedDict, cast

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

# uvicorn's shutdown close code (RFC 6455 "service restart"); a server is
# the only peer that sends it.
SERVER_INITIATED_CLOSE_CODES: frozenset[int] = frozenset({1012})

# Outlast the runner-disconnect grace, with scheduling slack, so shutdown
# suppression is still active when reconciliation runs.
SHUTDOWN_WINDOW_S: float = 150.0

_marked_at: float | None = None


def mark_server_shutting_down() -> None:
    """
    Record that this server process is shutting down now.
    """
    global _marked_at
    # Log the first mark of each window so the disconnects it suppresses can
    # be tied to this replica's shutdown.
    if not server_shutting_down():
        _logger.info(
            "Server shutdown marked; suppressing runner disconnect reconciliation for %.0fs",
            SHUTDOWN_WINDOW_S,
            extra=debug_event("server_shutdown_marked", window_s=SHUTDOWN_WINDOW_S),
        )
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


# runner_id -> (monotonic mark time, reason); entries past SHUTDOWN_WINDOW_S are pruned on write.
# Markers may run outside any workspace scope, so the key must be the bare runner id.
# custom-lint: disable-next=workspace-scoped-cache -- runner_id is globally unique
_handed_off: dict[str, tuple[float, str]] = {}


def mark_runner_handed_off(runner_id: str, *, reason: str, **attributes: object) -> None:
    """
    Record that this replica closed *runner_id*'s tunnel so another replica adopts it.

    Logs a ``runner_tunnel_handed_off`` debug event, which is how a disconnect
    on this replica is attributed to a rollout rather than to the runner.

    :param runner_id: Runner whose tunnel this replica is closing, e.g.
        ``"runner_0123456789abcdef"``.
    :param reason: Why the tunnel moves, e.g. ``"rebalance"``.
    :param attributes: Extra debug-event attributes, e.g. ``slice_key="123"``.
    """
    now = time.monotonic()
    _prune_handoffs(now)
    _handed_off[runner_id] = (now, reason)
    extra = debug_event("runner_tunnel_handed_off")
    # The core fields win over a colliding caller attribute.
    cast("dict[str, object]", extra["attributes"]).update(
        attributes, runner_id=runner_id, handoff_reason=reason
    )
    _logger.info(
        "Runner %s tunnel handed off to another replica (%s)",
        runner_id,
        reason,
        extra=extra,
    )


def clear_runner_handoff(runner_id: str) -> None:
    """
    Forget a handoff once *runner_id* tunnels back to this replica.

    Its next disconnect here is this replica's to reconcile again.

    :param runner_id: Runner that just connected, e.g. ``"runner_0123456789abcdef"``.
    """
    _handed_off.pop(runner_id, None)
    _prune_handoffs(time.monotonic())


def _prune_handoffs(now: float) -> None:
    for stale_id in [rid for rid, (at, _) in _handed_off.items() if now - at >= SHUTDOWN_WINDOW_S]:
        del _handed_off[stale_id]


def note_runner_tunnel_retired(runner_id: str) -> None:
    """
    Record a handoff for a live tunnel this server retired so the runner reconnects.

    The in-band counterpart of :func:`mark_runner_handed_off`: the tunnel
    registry calls it for every deliberate retire, so the disconnect it causes
    is never read as the runner dying. A caller that already recorded a more
    specific reason keeps it.

    :param runner_id: Runner whose tunnel was retired, e.g.
        ``"runner_0123456789abcdef"``.
    """
    if runner_handoff_reason(runner_id) is None:
        mark_runner_handed_off(runner_id, reason="tunnel_retired")


def runner_handoff_reason(runner_id: str | None, now: float | None = None) -> str | None:
    """
    Return why *runner_id* was handed off within :data:`SHUTDOWN_WINDOW_S`, if it was.

    :param runner_id: Runner to check; ``None`` is never handed off.
    :param now: Monotonic clock reading to measure against; defaults to
        :func:`time.monotonic`.
    :returns: The recorded reason, e.g. ``"rebalance"``, or ``None``.
    """
    if runner_id is None:
        return None
    entry = _handed_off.get(runner_id)
    if entry is None:
        return None
    current = time.monotonic() if now is None else now
    marked_at, reason = entry
    return reason if current - marked_at < SHUTDOWN_WINDOW_S else None


class DisconnectAttributes(TypedDict):
    """Disconnect debug-event attributes describing this replica's own actions."""

    server_shutting_down: bool
    runner_handoff_reason: str | None


def disconnect_attributes(runner_id: str | None) -> DisconnectAttributes:
    """
    Debug-event attributes that tie a runner disconnect to this replica's own actions.

    :param runner_id: Runner whose disconnect is being reconciled.
    :returns: ``server_shutting_down`` and ``runner_handoff_reason`` attributes.
    """
    return {
        "server_shutting_down": server_shutting_down(),
        "runner_handoff_reason": runner_handoff_reason(runner_id),
    }


def reset_for_tests() -> None:
    """
    Clear the shutdown and handoff marks (test isolation).
    """
    global _marked_at
    _marked_at = None
    _handed_off.clear()
