"""Correlate reconnect progress with an identified interrupted runner turn.

Unknown identities remain unattributed. Watches are local to the server process;
missing an observation keeps the failure counted rather than guessing recovery.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from cachetools import TTLCache

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)
_WATCH_TTL_S = 12 * 3600.0
_MAX_WATCHES = 10_000
_lock = threading.Lock()


@dataclass(frozen=True)
class Failure:
    """Identity captured when a disconnect actually fails a turn."""

    episode_id: str
    runner_id: str | None
    response_id: str | None


@dataclass(frozen=True)
class _Watch:
    failure: Failure
    failure_code: str
    cleared_at: float


_failures: WorkspaceScopedCache[str, Failure] = WorkspaceScopedCache(
    lambda: TTLCache(maxsize=_MAX_WATCHES, ttl=_WATCH_TTL_S, timer=lambda: time.monotonic())
)
_watches: WorkspaceScopedCache[str, _Watch] = WorkspaceScopedCache(
    lambda: TTLCache(maxsize=_MAX_WATCHES, ttl=_WATCH_TTL_S, timer=lambda: time.monotonic())
)


def note_disconnect_failure(
    session_id: str, *, runner_id: str | None, response_id: str | None
) -> Failure:
    """Capture an episode token, retaining any known runner and turn identity."""
    with _lock:
        _watches.pop(session_id, None)
        _failures.pop(session_id, None)
        failure = Failure(uuid4().hex, runner_id, response_id)
        _failures[session_id] = failure
        return failure


def pending_failure(session_id: str) -> Failure | None:
    """Capture a registration token before recovery performs asynchronous reads."""
    with _lock:
        return _failures.get(session_id)


def note_disconnect_failure_cleared(
    session_id: str,
    *,
    failure_code: str,
    runner_id: str | None = None,
    failure: Failure | None = None,
) -> None:
    """Watch only the captured episode still owned by the reconnected runner."""
    with _lock:
        if (
            failure is None
            or failure_code != "runner_disconnected"
            or not failure.runner_id
            or not failure.response_id
            or failure.runner_id != runner_id
            or _failures.get(session_id) is not failure
        ):
            return
        _watches[session_id] = _Watch(failure, failure_code, time.monotonic())
    _logger.info(
        "disconnect failure cleared on reconnect for session=%s; watching identified turn",
        session_id,
        extra=debug_event(
            "session_turn_failure_cleared",
            session_id=session_id,
            failure_code=failure_code,
            runner_id=runner_id,
            turn_id=failure.response_id,
            disconnect_episode_id=failure.episode_id,
        ),
    )


def note_user_turn(session_id: str) -> None:
    """Invalidate registration as soon as another user turn is accepted."""
    with _lock:
        _failures.pop(session_id, None)
        watch = _watches.pop(session_id, None)
    if watch is not None:
        _log_outcome(session_id, watch, "superseded_by_user_turn", signal="user_turn")


def observe_event(
    session_id: str,
    event: dict[str, Any],
    *,
    runner_id: str | None = None,
    response_id: str | None = None,
) -> None:
    """Observe a trusted runner event without accepting unidentified stream output.

    Both identities come from the bound relay's lifecycle state. Runner
    deltas need not repeat the response identity on the wire.
    """
    if not runner_id or not response_id:
        return
    named_ids = [event.get("response_id")]
    for field, key in (("response", "id"), ("item", "response_id")):
        nested = event.get(field)
        if isinstance(nested, dict):
            named_ids.append(nested.get(key))
    if any(value is not None and value != response_id for value in named_ids):
        return
    with _lock:
        watch = _watches.get(session_id)
        if watch is None or watch.failure.runner_id != runner_id:
            return
        if response_id != watch.failure.response_id:
            return
        event_type = event.get("type")
        if event_type == "session.status":
            progress = event.get("status") in {"running", "waiting"} and not event.get("error")
        elif event_type in {
            "response.output_text.delta",
            "response.reasoning_text.delta",
            "response.function_call_output.delta",
        }:
            delta = event.get("delta")
            progress = isinstance(delta, str) and bool(delta.strip()) and not event.get("final")
        else:
            progress = False
        if not progress:
            return
        _watches.pop(session_id, None)
        if _failures.get(session_id) is not watch.failure:
            return
        _failures.pop(session_id, None)
    _log_outcome(session_id, watch, "turn_resumed", signal=str(event_type))


def reset_for_tests() -> None:
    """Forget every failure episode and watch."""
    with _lock:
        _failures.clear()
        _watches.clear()


def _log_outcome(session_id: str, watch: _Watch, outcome: str, *, signal: str) -> None:
    failure = watch.failure
    _logger.info(
        "disconnect failure outcome for session=%s: %s",
        session_id,
        outcome,
        extra=debug_event(
            "session_turn_failure_outcome",
            session_id=session_id,
            outcome=outcome,
            failure_code=watch.failure_code,
            runner_id=failure.runner_id,
            turn_id=failure.response_id,
            disconnect_episode_id=failure.episode_id,
            signal=signal,
            seconds_since_cleared=round(time.monotonic() - watch.cleared_at, 3),
        ),
    )
