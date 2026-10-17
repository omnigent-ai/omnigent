"""Record whether a turn failed by a runner disconnect was actually lost.

A sleeping or offline host fails its running turn as ``runner_disconnected``
after the disconnect grace, but the agent often keeps working on the host and
the same turn resumes once the runner reconnects. Reliability metrics should
only count the turns that were lost.

When a passive reconnect clears an identified disconnect decision, its exact
turn and binding are watched:

- owned progress before an explicit new user message logs
  ``session_turn_failure_outcome`` with ``outcome=turn_resumed``;
- an explicit user-turn notification first logs ``outcome=superseded_by_user_turn``, and the
  failure stands.

A failure that is never cleared, or whose session never resumes, gets no
outcome row and keeps counting. Watches are per process; an outcome observed
on another replica is simply not recorded, which also keeps the failure
counted.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from omnigent.debug_logging import debug_event
from omnigent.entities.session_observation import RunnerOfflineDecision, SessionObservation

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
    decision: RunnerOfflineDecision
    recovered: SessionObservation


# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally unique session id
_watches: dict[str, _Watch] = {}
# custom-lint: disable-next=workspace-scoped-cache -- tracks only active registration calls
_registrations: dict[str, object] = {}
_watch_lock = threading.RLock()


def note_disconnect_failure_cleared(
    session_id: str,
    *,
    failure_code: str,
    runner_id: str | None = None,
    decision: RunnerOfflineDecision | None = None,
    recovered_observation: SessionObservation | None = None,
) -> None:
    """Start watching a session whose disconnect failure a reconnect cleared.

    :param session_id: Session whose ``failed`` status was cleared,
        e.g. ``"conv_abc123"``.
    :param failure_code: Code of the cleared failure, e.g.
        ``"runner_disconnected"``.
    :param runner_id: Runner that reconnected, when known.
    :param decision: Captured transport failure; label-only failures are not evidence.
    :param recovered_observation: Successful, still-current recovery projection.
    :returns: None.
    """
    registration = object()
    with _watch_lock:
        previous = _watches.get(session_id)
        _registrations[session_id] = registration
    try:
        watch = _captured_watch(failure_code, runner_id, decision, recovered_observation)
        installed = False
        with _watch_lock:
            if (
                _registrations.get(session_id) is registration
                and _watches.get(session_id) is previous
            ):
                if watch is None:
                    _watches.pop(session_id, None)
                elif _registration_follows(watch, previous):
                    if len(_watches) >= _MAX_WATCHES:
                        _drop_expired(watch.cleared_at)
                        if len(_watches) >= _MAX_WATCHES:
                            _watches.pop(next(iter(_watches)), None)
                    _watches[session_id] = watch
                    installed = True
    finally:
        with _watch_lock:
            if _registrations.get(session_id) is registration:
                del _registrations[session_id]
    _logger.info(
        "disconnect failure cleared on reconnect for session=%s; outcome tracking=%s",
        session_id,
        installed,
        extra=debug_event(
            "session_turn_failure_cleared",
            session_id=session_id,
            failure_code=failure_code,
            runner_id=watch.decision.observation.runner_id
            if watch is not None
            else runner_id or "",
            turn_id=watch.decision.observation.response_id if watch is not None else None,
            runner_binding_epoch=(
                watch.decision.observation.binding_epoch if watch is not None else None
            ),
            runner_offline_episode_id=watch.decision.episode_id if watch is not None else "",
            outcome_tracking=installed,
        ),
    )


def note_user_turn(session_id: str) -> None:
    """Resolve a watch as lost because the user started a new turn first.

    :param session_id: Session receiving a new user message.
    :returns: None.
    """
    with _watch_lock:
        _registrations.pop(session_id, None)
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
    with _watch_lock:
        watch = _watches.get(session_id)
    if watch is None:
        return
    if not _matches_observation(watch, event):
        return
    event_type = event.get("type")
    if event_type == "session.status":
        if event.get("status") not in _IN_FLIGHT_STATUSES:
            return
    elif event_type not in _PROGRESS_EVENT_TYPES or not _has_progress(event):
        return
    with _watch_lock:
        if _watches.get(session_id) is not watch:
            return
        del _watches[session_id]
    if time.monotonic() - watch.cleared_at > _WATCH_TTL_S:
        return
    _log_outcome(session_id, watch, "turn_resumed", signal=str(event_type))


def reset_for_tests() -> None:
    """Forget every watch."""
    with _watch_lock:
        _watches.clear()
        _registrations.clear()


def _drop_expired(now: float) -> None:
    with _watch_lock:
        for session_id in [s for s, w in _watches.items() if now - w.cleared_at > _WATCH_TTL_S]:
            del _watches[session_id]


def _captured_watch(
    failure_code: str,
    runner_id: str | None,
    decision: RunnerOfflineDecision | None,
    recovered: SessionObservation | None,
) -> _Watch | None:
    if decision is None or recovered is None:
        return None
    owner = decision.observation
    if (
        failure_code != "runner_disconnected"
        or decision.error_labels.get("omnigent.last_task_error_code") != failure_code
        or not decision.episode_id
        or not owner.runner_id
        or not owner.response_id
        or not owner.observation_id
        or not recovered.observation_id
        or owner.status not in _IN_FLIGHT_STATUSES
        or recovered.status != "idle"
        or (runner_id is not None and runner_id != owner.runner_id)
        or recovered.runner_id != owner.runner_id
        or recovered.binding_epoch != owner.binding_epoch
        or recovered.revision <= owner.revision
        or recovered.response_id not in (None, owner.response_id)
    ):
        return None
    return _Watch(time.monotonic(), failure_code, decision.model_copy(deep=True), recovered)


def _registration_follows(watch: _Watch, previous: _Watch | None) -> bool:
    if previous is None:
        return True
    before, after = previous.recovered, watch.recovered
    before_order = (before.binding_epoch, before.revision)
    after_order = (after.binding_epoch, after.revision)
    if after_order == before_order:
        return after == before and watch.decision == previous.decision
    return after_order > before_order and (
        after.binding_epoch != before.binding_epoch or after.runner_id == before.runner_id
    )


def _matches_observation(watch: _Watch, event: dict[str, Any]) -> bool:
    raw = event.get("session_observation")
    if raw is None or event.get("runner_offline_episode_id") is not None:
        return False
    try:
        observed = SessionObservation.model_validate(raw)
    except (ValueError, TypeError):
        return False
    owner = watch.decision.observation
    if (
        observed.runner_id != owner.runner_id
        or not observed.observation_id
        or observed.binding_epoch != owner.binding_epoch
        or observed.response_id != owner.response_id
        or observed.revision < watch.recovered.revision
        or (observed.revision == watch.recovered.revision and observed != watch.recovered)
    ):
        return False
    if event.get("type") == "session.status" and event.get("status") != observed.status:
        return False
    item = event.get("item")
    response = event.get("response")
    response_ids = [
        event.get("response_id"),
        item.get("response_id") if isinstance(item, dict) else None,
        response.get("id") if isinstance(response, dict) else None,
    ]
    return all(value is None or value == owner.response_id for value in response_ids)


def _has_progress(event: dict[str, Any]) -> bool:
    if event.get("type") != "response.output_item.done":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta.strip()) and not event.get("final")
    item = event.get("item")
    if not isinstance(item, dict):
        return False
    kind = item.get("type")
    if kind == "message":
        content = item.get("content")
        return (
            item.get("role") == "assistant"
            and not item.get("is_meta")
            and isinstance(content, list)
            and any(
                isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and bool(block["text"].strip())
                for block in content
            )
        )
    if kind == "function_call":
        return (
            isinstance(item.get("call_id"), str)
            and bool(item["call_id"].strip())
            and isinstance(item.get("name"), str)
            and bool(item["name"].strip())
        )
    if kind == "function_call_output":
        output = item.get("output")
        return (
            isinstance(item.get("call_id"), str)
            and bool(item["call_id"].strip())
            and isinstance(output, str)
            and bool(output.strip())
        )
    return False


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
            runner_id=watch.decision.observation.runner_id or "",
            runner_binding_epoch=watch.decision.observation.binding_epoch,
            turn_id=watch.decision.observation.response_id,
            runner_offline_episode_id=watch.decision.episode_id,
            signal=signal,
            seconds_since_cleared=round(time.monotonic() - watch.cleared_at, 3),
        ),
    )
