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
  failure stands;
- the runner reporting the failed turn finished, after its relay saw the turn
  make progress, logs ``outcome=turn_resumed`` at once (``signal=turn_completed``).

A failure that is never cleared, or whose session never resumes, gets no
outcome row and keeps counting. Watches are per process; an outcome observed
on another replica is simply not recorded, which also keeps the failure
counted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

# A turn that resumes later than this is not plausibly the same turn.
_WATCH_TTL_S = 12 * 3600.0
_MAX_WATCHES = 10_000

_PROGRESS_DELTA_TYPES = frozenset(
    {
        "response.output_text.delta",
        "response.reasoning_text.delta",
        "response.function_call_output.delta",
    }
)
# Items only an agent produces mid-turn. User input, slash commands and child
# lifecycle notifications also arrive as ``response.output_item.done``.
_AGENT_ITEM_TYPES = frozenset(
    {"function_call", "function_call_output", "native_tool", "reasoning"}
)
_IN_FLIGHT_STATUSES = frozenset({"running", "waiting"})


@dataclass(frozen=True)
class _Watch:
    cleared_at: float
    failure_code: str
    runner_id: str | None


# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally unique session id
_watches: dict[str, _Watch] = {}


def watch_for_resume(session_id: str, *, failure_code: str, runner_id: str | None = None) -> None:
    """Watch a session whose disconnect failure a passive reconnect cleared.

    The caller logs the clear itself (``session_runner_recovered``); this only
    arms the watch that the next progress event or user turn resolves.

    :param session_id: Session whose ``failed`` status was cleared,
        e.g. ``"conv_abc123"``.
    :param failure_code: Code of the cleared failure, e.g.
        ``"runner_disconnected"``.
    :param runner_id: Runner bound to the session, when known.
    :returns: None.
    """
    now = time.monotonic()
    if len(_watches) >= _MAX_WATCHES:
        _drop_expired(now)
        # Other threads may pop entries between the length check and this read.
        oldest = next(iter(_watches), None) if len(_watches) >= _MAX_WATCHES else None
        if oldest is not None:
            _watches.pop(oldest, None)
    _watches[session_id] = _Watch(cleared_at=now, failure_code=failure_code, runner_id=runner_id)


def note_user_turn(session_id: str) -> None:
    """Resolve a watch as lost because the user started a new turn first.

    :param session_id: Session receiving a new user message.
    :returns: None.
    """
    watch = _watches.pop(session_id, None)
    if watch is None or time.monotonic() - watch.cleared_at > _WATCH_TTL_S:
        return
    _log_outcome(session_id, watch, "superseded_by_user_turn", signal="user_turn")


def note_turn_completed(
    session_id: str, *, failure_code: str, runner_id: str | None = None
) -> None:
    """Record a failed turn as resumed because the runner reported it finished.

    The completion is the progress, so no watch is armed and any earlier watch
    for the session is resolved by it.

    :param session_id: Session whose ``failed`` status was cleared.
    :param failure_code: Code of the cleared failure, e.g.
        ``"runner_disconnected"``.
    :param runner_id: Runner bound to the session, when known.
    :returns: None.
    """
    _watches.pop(session_id, None)
    watch = _Watch(cleared_at=time.monotonic(), failure_code=failure_code, runner_id=runner_id)
    _log_outcome(session_id, watch, "turn_resumed", signal="turn_completed")


def is_progress(event: Mapping[str, Any]) -> bool:
    """Whether a published session event shows the turn moving on its own.

    :param event: The published event, e.g.
        ``{"type": "response.output_text.delta", "delta": "Hi"}``.
    :returns: ``True`` for agent output and in-flight status reports; ``False``
        for idle/failed status, final deltas, user items and notifications.
    """
    event_type = event.get("type")
    if event_type == "session.status":
        return event.get("status") in _IN_FLIGHT_STATUSES
    if event_type == "response.output_item.done":
        item = event.get("item")
        if not isinstance(item, Mapping):
            return False
        if item.get("type") == "message":
            return item.get("role") == "assistant"
        return item.get("type") in _AGENT_ITEM_TYPES
    if event_type == "response.output_text.delta":
        return not event.get("final")
    return event_type in _PROGRESS_DELTA_TYPES


def observe_event(session_id: str, event: dict[str, Any]) -> None:
    """Resolve a watch as resumed when the session makes progress on its own.

    Called for every published session event; a dict lookup when unwatched.

    :param session_id: Session the event belongs to.
    :param event: The published event, e.g.
        ``{"type": "response.output_text.delta", "delta": "Hi"}``.
    :returns: None.
    """
    if session_id not in _watches or not is_progress(event):
        return
    # publish() also runs on worker threads; pop so only one caller resolves it,
    # and resolve the watch that was actually popped.
    watch = _watches.pop(session_id, None)
    if watch is None or time.monotonic() - watch.cleared_at > _WATCH_TTL_S:
        return
    _log_outcome(session_id, watch, "turn_resumed", signal=str(event.get("type")))


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
