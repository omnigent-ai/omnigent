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
