"""Record project usage events without ever failing the request that caused them.

Recording is on by default and turned off with ``OMNIGENT_USAGE_EVENTS=off``.
Events older than :data:`RETENTION_DAYS` are pruned at most once a day, by the
first recording after that day passes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from omnigent.db.utils import now_epoch

logger = logging.getLogger(__name__)

RETENTION_DAYS = 180
USAGE_EVENTS_ENV = "OMNIGENT_USAGE_EVENTS"
_PRUNE_EVERY_SECONDS = 86_400
_prune_lock = threading.Lock()
_last_prune: dict[int, int] = {}


#: One priced turn's cost, so spend can be split by when it happened.
COST_EVENT = "cost.recorded"
_turn_cost_store: Any = None


def set_turn_cost_store(store: Any) -> None:
    """Where :func:`record_turn_cost` writes; the server sets it at startup."""
    global _turn_cost_store
    _turn_cost_store = store


def record_turn_cost(
    conversation_store: Any, session_id: str, root_id: str, project_id: str | None, micros: int
) -> Any:
    """Queue one turn's cost on the background writer; it never blocks or fails the turn.

    :returns: The queued write's future, or ``None`` when nothing was queued.
    """
    store = _turn_cost_store
    if store is None or micros <= 0 or not usage_events_enabled():
        return None
    created_at = now_epoch()

    def write() -> None:
        project = project_id
        if project is None and root_id != session_id:
            root = conversation_store.get_conversation(root_id)
            project = getattr(root, "project_id", None)
        if project is None:
            return
        store.record(
            COST_EVENT,
            project_id=project,
            session_id=session_id,
            props={"micros": micros},
            created_at=created_at,
        )

    try:
        from omnigent.server import session_live_state

        return session_live_state.submit("usage_event_turn_cost", write)
    except Exception:
        logger.warning("Couldn't queue the turn's cost event", exc_info=True)
        return None


def usage_events_enabled() -> bool:
    return os.environ.get(USAGE_EVENTS_ENV, "").strip().lower() not in ("0", "off", "false", "no")


def usage_event_store(app: Any) -> Any:
    """The app's usage-event store, or ``None`` when recording is off or unavailable."""
    if not usage_events_enabled():
        return None
    return getattr(getattr(app, "state", None), "usage_events", None)


def record_usage_event_sync(app: Any, name: str, **fields: Any) -> None:
    """Record one event; any failure is logged and swallowed."""
    store = usage_event_store(app)
    if store is None:
        return
    try:
        store.record(name, **fields)
        _maybe_prune(store)
    except Exception:
        logger.warning("Couldn't record usage event %s", name, exc_info=True)


async def record_usage_event(app: Any, name: str, **fields: Any) -> None:
    """Record one event off the event loop; never raises."""
    if usage_event_store(app) is None:
        return
    await asyncio.to_thread(record_usage_event_sync, app, name, **fields)


def _maybe_prune(store: Any) -> None:
    now = now_epoch()
    with _prune_lock:
        if now - _last_prune.get(id(store), 0) < _PRUNE_EVERY_SECONDS:
            return
        _last_prune[id(store)] = now
    store.prune(now - RETENTION_DAYS * 86_400)
