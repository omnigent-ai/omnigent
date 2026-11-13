"""Record whether a turn failed by a runner disconnect was actually lost.

A sleeping or offline host fails its running turn as ``runner_disconnected``
after the disconnect grace, but the agent often keeps working on the host and
the same turn resumes once the runner reconnects. Reliability metrics should
only count the turns that were lost.

When a passive reconnect clears a disconnect failure, the session is watched:

- progress for it (agent output, or the runner reporting the turn still
  running) before any new user message logs ``session_turn_failure_outcome``
  with ``outcome=turn_resumed``;
- a new user message first logs ``outcome=superseded_by_user_turn``, and the
  failure stands.

A failure that is never cleared, or whose session never resumes, gets no
outcome row and keeps counting. Watches are per process; an outcome observed
on another replica is simply not recorded, which also keeps the failure
counted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

# A turn that resumes later than this is not plausibly the same turn.
_WATCH_TTL_S = 12 * 3600.0
_MAX_WATCHES = 10_000

_PROGRESS_EVENT_TYPES = frozenset(
    {
        "response.output_text.delta",
        "response.reasoning_text.delta",
        "response.function_call_output.delta",
        "response.output_item.done",
    }
)
_IN_FLIGHT_STATUSES = frozenset({"running", "waiting"})


@dataclass(frozen=True)
class _Watch:
    cleared_at: float
    failure_code: str
    runner_id: str | None


# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally unique session id
_watches: dict[str, _Watch] = {}


def note_disconnect_failure_cleared(
    session_id: str, *, failure_code: str, runner_id: str | None = None
) -> None:
    """Start watching a session whose disconnect failure a reconnect cleared.

    :param session_id: Session whose ``failed`` status was cleared,
        e.g. ``"conv_abc123"``.
    :param failure_code: Code of the cleared failure, e.g.
        ``"runner_disconnected"``.
    :param runner_id: Runner that reconnected, when known.
    :returns: None.
    """
    now = time.monotonic()
    if len(_watches) >= _MAX_WATCHES:
        _drop_expired(now)
        if len(_watches) >= _MAX_WATCHES:
            _watches.pop(next(iter(_watches)), None)
    _watches[session_id] = _Watch(cleared_at=now, failure_code=failure_code, runner_id=runner_id)
    _logger.info(
        "disconnect failure cleared on reconnect for session=%s; watching for the turn to resume",
        session_id,
        extra=debug_event(
            "session_turn_failure_cleared",
            session_id=session_id,
            failure_code=failure_code,
            runner_id=runner_id or "",
        ),
    )


def note_user_turn(session_id: str) -> None:
    """Resolve a watch as lost because the user started a new turn first.

    :param session_id: Session receiving a new user message.
    :returns: None.
    """
    watch = _watches.pop(session_id, None)
    if watch is not None:
        _log_outcome(session_id, watch, "superseded_by_user_turn", signal="user_turn")


def observe_event(session_id: str, event: dict[str, Any]) -> None:
    """Resolve a watch as resumed when the session makes progress on its own.

    Called for every published session event; a dict lookup when unwatched.

    :param session_id: Session the event belongs to.
    :param event: The published event, e.g.
        ``{"type": "response.output_text.delta", "delta": "Hi"}``.
    :returns: None.
    """
    watch = _watches.get(session_id)
    if watch is None:
        return
    event_type = event.get("type")
    if event_type == "session.status":
        if event.get("status") not in _IN_FLIGHT_STATUSES:
            return
    elif event_type not in _PROGRESS_EVENT_TYPES:
        return
    if event_type == "response.output_text.delta" and event.get("final"):
        return
    # publish() also runs on worker threads; pop so only one caller resolves it.
    if _watches.pop(session_id, None) is None:
        return
    if time.monotonic() - watch.cleared_at > _WATCH_TTL_S:
        return
    _log_outcome(session_id, watch, "turn_resumed", signal=str(event_type))


def reset_for_tests() -> None:
    """Forget every watch."""
    _watches.clear()


def _drop_expired(now: float) -> None:
    for session_id in [s for s, w in list(_watches.items()) if now - w.cleared_at > _WATCH_TTL_S]:
        _watches.pop(session_id, None)


def _log_outcome(session_id: str, watch: _Watch, outcome: str, *, signal: str) -> None:
    _logger.info(
        "disconnect failure outcome for session=%s: %s",
        session_id,
        outcome,
        extra=debug_event(
            "session_turn_failure_outcome",
            session_id=session_id,
            outcome=outcome,
            failure_code=watch.failure_code,
            runner_id=watch.runner_id or "",
            signal=signal,
            seconds_since_cleared=round(time.monotonic() - watch.cleared_at, 3),
        ),
    )
