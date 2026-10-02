"""Tracks persisted steered messages until the runner consumes them."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache

# Idle status and session delete clear entries; the TTL bounds a lost edge.
_TTL_S: float = 6 * 3600.0

# Covers a drain marker racing ahead of the forward acknowledgment.
_PRE_DRAINED_TTL_S: float = 60.0


def _now() -> float:
    return time.monotonic()


@dataclass
class _Entry:
    item: Any
    recorded_at: float


_unconsumed: WorkspaceScopedCache[str, dict[str, _Entry]] = WorkspaceScopedCache()
_pre_drained: WorkspaceScopedCache[str, dict[str, float]] = WorkspaceScopedCache()
_lock = threading.Lock()


def _evict_stale_locked(now: float) -> None:
    """Sweep every conversation in the workspace so untouched ones expire too."""
    for conversation_id, entries in _unconsumed.items():
        for item_id in [i for i, e in entries.items() if now - e.recorded_at > _TTL_S]:
            entries.pop(item_id, None)
        if not entries:
            _unconsumed.pop(conversation_id, None)
    for conversation_id, marks in _pre_drained.items():
        for item_id in [i for i, at in marks.items() if now - at > _PRE_DRAINED_TTL_S]:
            marks.pop(item_id, None)
        if not marks:
            _pre_drained.pop(conversation_id, None)


def record(conversation_id: str, item_id: str, item: Any) -> bool:
    """Return false when this item was already reported as drained."""
    entry = _Entry(item=item, recorded_at=_now())
    with _lock:
        _evict_stale_locked(entry.recorded_at)
        pre = _pre_drained.get(conversation_id)
        if pre is not None and pre.pop(item_id, None) is not None:
            if not pre:
                _pre_drained.pop(conversation_id, None)
            return False
        _unconsumed.setdefault(conversation_id, {})[item_id] = entry
        return True


def resolve(conversation_id: str, item_id: str) -> Any | None:
    """Resolve an item, remembering unknown IDs for a racing record."""
    now = _now()
    with _lock:
        _evict_stale_locked(now)
        entries = _unconsumed.get(conversation_id)
        entry = entries.pop(item_id, None) if entries is not None else None
        if entries is not None and not entries:
            _unconsumed.pop(conversation_id, None)
        if entry is not None:
            return entry.item
        _pre_drained.setdefault(conversation_id, {})[item_id] = now
        return None


def snapshot_for(conversation_id: str) -> list[str]:
    with _lock:
        _evict_stale_locked(_now())
        entries = _unconsumed.get(conversation_id)
        return list(entries) if entries else []


def drain(conversation_id: str) -> list[Any]:
    """Forget every tracked item of a conversation, returning them oldest first."""
    with _lock:
        entries = _unconsumed.pop(conversation_id, None)
        _pre_drained.pop(conversation_id, None)
        return [entry.item for entry in entries.values()] if entries else []


def clear(conversation_id: str) -> None:
    with _lock:
        _unconsumed.pop(conversation_id, None)
        _pre_drained.pop(conversation_id, None)


def reset_for_tests() -> None:
    with _lock:
        _unconsumed.clear()
        _pre_drained.clear()
