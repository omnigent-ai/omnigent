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

When the relay itself sees the written-off turn in flight or finishing, the
clear records the outcome at once (``signal=turn_running`` or
``turn_completed``), unless a user turn was just started for the session, in
which case that turn supersedes the failed one.

A failure that is never cleared, or whose session never resumes, gets no
outcome row and keeps counting. Watches are per process; an outcome observed
on another replica is simply not recorded, which also keeps the failure
counted.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

# A turn that resumes later than this is not plausibly the same turn.
_WATCH_TTL_S = 12 * 3600.0
# A running edge this soon after a new user turn belongs to that turn.
_USER_TURN_ATTRIBUTION_S = 300.0
_MAX_WATCHES = 10_000

_PROGRESS_DELTA_TYPES = frozenset(
    {"response.reasoning_text.delta", "response.function_call_output.delta"}
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
# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally unique session id
_recent_user_turns: dict[str, float] = {}
# publish() observes events from worker threads while the server loop arms watches.
_lock = threading.Lock()


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
    with _lock:
        _make_room(_watches, now, lambda watch: watch.cleared_at, _WATCH_TTL_S)
        # Pop first so a re-armed watch takes the newest insertion slot.
        _watches.pop(session_id, None)
        _watches[session_id] = _Watch(
            cleared_at=now, failure_code=failure_code, runner_id=runner_id
        )


def note_user_turn(session_id: str) -> None:
    """Record that the user started a new turn, superseding any watched failure.

    Callers note the turn after rejection and de-duplication checks but before
    forwarding it. Noting it only after the runner accepted the input could let
    the new turn's first output pass for the old turn resuming, which would
    hide a lost turn; a failed forward only leaves the failure counted.

    :param session_id: Session receiving a new user message or skill command.
    :returns: None.
    """
    now = time.monotonic()
    with _lock:
        _make_room(_recent_user_turns, now, lambda started: started, _USER_TURN_ATTRIBUTION_S)
        _recent_user_turns.pop(session_id, None)
        _recent_user_turns[session_id] = now
        watch = _watches.pop(session_id, None)
    if watch is None or now - watch.cleared_at > _WATCH_TTL_S:
        return
    _log_outcome(session_id, watch, "superseded_by_user_turn", signal="user_turn")


def note_turn_resumed(
    session_id: str, *, failure_code: str, runner_id: str | None = None, signal: str
) -> None:
    """Record a failed turn's outcome when the relay saw it running or finishing.

    That observation is the progress, so no watch is armed and any earlier
    watch for the session is resolved by it. A running edge that follows a
    user turn started moments ago belongs to that new turn, so the failed turn
    is recorded as superseded rather than resumed.

    :param session_id: Session whose ``failed`` status was cleared.
    :param failure_code: Code of the cleared failure, e.g.
        ``"runner_disconnected"``.
    :param runner_id: Runner bound to the session, when known.
    :param signal: What the relay saw, ``"turn_running"`` or ``"turn_completed"``.
    :returns: None.
    """
    now = time.monotonic()
    with _lock:
        _watches.pop(session_id, None)
        user_turn_at = _recent_user_turns.pop(session_id, None)
    watch = _Watch(cleared_at=now, failure_code=failure_code, runner_id=runner_id)
    if user_turn_at is not None and now - user_turn_at <= _USER_TURN_ATTRIBUTION_S:
        _log_outcome(session_id, watch, "superseded_by_user_turn", signal="user_turn")
        return
    _log_outcome(session_id, watch, "turn_resumed", signal=signal)


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
    with _lock:
        watch = _watches.pop(session_id, None)
    if watch is None or time.monotonic() - watch.cleared_at > _WATCH_TTL_S:
        return
    _log_outcome(session_id, watch, "turn_resumed", signal=str(event.get("type")))


def reset_for_tests() -> None:
    """Forget every watch and noted user turn."""
    with _lock:
        _watches.clear()
        _recent_user_turns.clear()


def _make_room(
    registry: dict[str, Any], now: float, started_at: Callable[[Any], float], ttl: float
) -> None:
    """Keep *registry* under the cap by dropping expired, then oldest, entries.

    The caller holds ``_lock``.
    """
    if len(registry) < _MAX_WATCHES:
        return
    for key in [k for k, value in list(registry.items()) if now - started_at(value) > ttl]:
        registry.pop(key, None)
    if len(registry) >= _MAX_WATCHES:
        oldest = next(iter(registry), None)
        if oldest is not None:
            registry.pop(oldest, None)


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
