"""Tracks persisted steered messages until the runner consumes them."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache

# Terminal status normally clears entries; this bounds leaks from lost edges.
_TTL_S: float = 6 * 3600.0

# Covers a drain marker racing ahead of the forward acknowledgment.
_PRE_DRAINED_TTL_S: float = 60.0


def _now() -> float:
    return time.monotonic()


@dataclass
class _Entry:
    item: Any
    # Resolve a monkeypatched clock when the entry is created.
    recorded_at: float = field(default_factory=lambda: _now())


_unconsumed: WorkspaceScopedCache[str, dict[str, _Entry]] = WorkspaceScopedCache()
_pre_drained: WorkspaceScopedCache[str, dict[str, float]] = WorkspaceScopedCache()
_lock = threading.Lock()


def _evict_stale_locked(conversation_id: str, now: float) -> None:
    entries = _unconsumed.get(conversation_id)
    if entries is not None:
        stale = [item_id for item_id, entry in entries.items() if now - entry.recorded_at > _TTL_S]
        for item_id in stale:
            entries.pop(item_id, None)
        if not entries:
            _unconsumed.pop(conversation_id, None)
    pre = _pre_drained.get(conversation_id)
    if pre is not None:
        expired = [item_id for item_id, at in pre.items() if now - at > _PRE_DRAINED_TTL_S]
        for item_id in expired:
            pre.pop(item_id, None)
        if not pre:
            _pre_drained.pop(conversation_id, None)


def record(conversation_id: str, item_id: str, item: Any) -> bool:
    """Return false when this item was already reported as drained."""
    entry = _Entry(item=item)
    with _lock:
        _evict_stale_locked(conversation_id, entry.recorded_at)
        pre = _pre_drained.get(conversation_id)
        if pre is not None and pre.pop(item_id, None) is not None:
            if not pre:
                _pre_drained.pop(conversation_id, None)
            return False
        _unconsumed.setdefault(conversation_id, {})[item_id] = entry
        return True


def resolve(conversation_id: str, item_id: str) -> Any | None:
    """Resolve an item, remembering unknown IDs for a racing record."""
    with _lock:
        entries = _unconsumed.get(conversation_id)
        entry = entries.pop(item_id, None) if entries is not None else None
        if entries is not None and not entries:
            _unconsumed.pop(conversation_id, None)
        if entry is not None:
            return entry.item
        _pre_drained.setdefault(conversation_id, {})[item_id] = _now()
        return None


def snapshot_for(conversation_id: str) -> list[str]:
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        entries = _unconsumed.get(conversation_id)
        return list(entries) if entries else []


def clear(conversation_id: str) -> None:
    with _lock:
        _unconsumed.pop(conversation_id, None)
        _pre_drained.pop(conversation_id, None)


def reset_for_tests() -> None:
    with _lock:
        _unconsumed.clear()
        _pre_drained.clear()
