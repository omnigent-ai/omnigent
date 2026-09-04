"""In-process index of persisted-but-unconsumed steered user messages.

Backs the "delivered, awaiting the harness" intermediate state for a user
message steered into an already-running turn. On that path the Omnigent
server persists the item at POST time (invariant I1) and the runner parks
it in the active turn's message buffer — the agent loop has verifiably
NOT seen it yet. The server publishes ``session.input.delivered`` (not
``session.input.consumed``) and records the item id here; when the runner
reports the buffered message was actually drained into a turn
(``session.input.drained`` on the relay stream), the entry resolves and
the canonical ``session.input.consumed`` goes out.

Same shape and limitations as the codebase's other transient recovery
indexes (:mod:`pending_inputs`, :mod:`pending_elicitations`):

* populated by the route layer when the runner answers a message forward
  with ``{"status": "buffered"}`` (steered into an active turn);
* replayed into the cold-load snapshot (``GET /v1/sessions/{id}``) via
  :func:`snapshot_for`, so a (re)connecting client renders the still-
  unconsumed message in its intermediate state instead of full-strength;
* drained via :func:`resolve` when the relay observes the runner's
  drain marker, or wholesale via :func:`clear` when the session reaches
  a terminal status (an ``idle``/``failed`` edge means no live turn is
  holding a buffered message any more — either it was consumed or the
  turn that held it is gone).

In-memory only and process-affine, riding the same affinity as
``session_stream``. Entries a dead runner never drains are bounded by
:data:`_TTL_S` (evicted lazily) and by the terminal-status clear.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

# A delivered entry is evicted this many seconds after it was recorded if
# it was never drained or cleared. Deliberately long: a steered message
# legitimately stays unconsumed for as long as the running turn does, and
# turns can run for hours. The terminal-status clear is the primary
# cleanup; the TTL only bounds a leak when that edge is lost too.
_TTL_S: float = 6 * 3600.0


def _now() -> float:
    """
    Return the current monotonic clock reading for TTL bookkeeping.

    Indirection point (not ``time.monotonic`` directly) so tests can
    advance the clock to exercise stale-entry eviction without a real
    sleep.

    :returns: ``time.monotonic()`` seconds.
    """
    return time.monotonic()


@dataclass
class _Entry:
    """
    One delivered-but-unconsumed steered item.

    :param item: The persisted conversation item exactly as appended at
        POST time (a :class:`ConversationItem`; typed loosely so this
        transient index stays entity-agnostic like its siblings). Held
        so the drain marker can republish the canonical
        ``session.input.consumed`` without a store lookup.
    :param recorded_at: ``time.monotonic()`` at record time, used only
        for TTL eviction.
    """

    item: Any
    # Lambda (not ``_now`` directly) so a monkeypatched ``_now`` is
    # resolved at construction time rather than bound at class def.
    recorded_at: float = field(default_factory=lambda: _now())


# Per-conversation mapping conversation_id → {item_id: entry}. The
# inner dict is insertion-ordered (FIFO delivery order). Empty inner
# dicts are popped eagerly so the index doesn't accrete stale keys.
_unconsumed: dict[str, dict[str, _Entry]] = {}
_lock = threading.Lock()


def _evict_stale_locked(conversation_id: str, now: float) -> None:
    """
    Drop entries older than :data:`_TTL_S` for one conversation.

    Caller must hold :data:`_lock`.

    :param conversation_id: Conversation/session id to sweep,
        e.g. ``"conv_abc123"``.
    :param now: Current ``time.monotonic()`` value to compare against.
    """
    entries = _unconsumed.get(conversation_id)
    if entries is None:
        return
    stale = [item_id for item_id, entry in entries.items() if now - entry.recorded_at > _TTL_S]
    for item_id in stale:
        entries.pop(item_id, None)
    if not entries:
        _unconsumed.pop(conversation_id, None)


def record(conversation_id: str, item_id: str, item: Any) -> None:
    """
    Record a persisted item delivered into a running turn's buffer.

    Called by the route layer after the runner acknowledged a message
    forward with ``{"status": "buffered"}`` — the item exists in
    conversation history but the agent loop has not consumed it.

    :param conversation_id: Conversation/session id the message was
        posted to, e.g. ``"conv_abc123"``.
    :param item_id: Store-assigned id of the persisted item,
        e.g. ``"msg_xyz789"``.
    :param item: The persisted conversation item itself, held so the
        eventual drain marker can republish it as the canonical
        ``session.input.consumed`` without a store lookup.
    """
    entry = _Entry(item=item)
    with _lock:
        _evict_stale_locked(conversation_id, entry.recorded_at)
        _unconsumed.setdefault(conversation_id, {})[item_id] = entry


def resolve(conversation_id: str, item_id: str) -> Any | None:
    """
    Drop an entry because the runner drained it into a turn.

    Idempotent: resolving an unknown id is a no-op returning ``None``
    (e.g. the entry was already cleared by a terminal status edge).

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    :param item_id: The persisted item id the runner reported drained,
        e.g. ``"msg_xyz789"``.
    :returns: The recorded conversation item when an entry was present,
        else ``None``.
    """
    with _lock:
        entries = _unconsumed.get(conversation_id)
        if entries is None:
            return None
        entry = entries.pop(item_id, None)
        if not entries:
            _unconsumed.pop(conversation_id, None)
        return entry.item if entry is not None else None


def snapshot_for(conversation_id: str) -> list[str]:
    """
    Item ids still awaiting harness consumption, in delivery order.

    Replayed into the ``GET /v1/sessions/{id}`` snapshot so a cold-
    loading client renders those messages in the intermediate state.

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    :returns: Item ids, oldest first. Empty list when none.
    """
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        entries = _unconsumed.get(conversation_id)
        return list(entries) if entries else []


def clear(conversation_id: str) -> None:
    """
    Drop every entry for one conversation.

    Called on a terminal session status (``idle``/``failed``): no live
    turn is holding a buffered message any more, so anything still
    recorded here was either consumed (its drain marker raced or was
    lost) or died with the turn — in both cases the persisted item in
    history is now the truth and the intermediate state must not stick.

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    """
    with _lock:
        _unconsumed.pop(conversation_id, None)


def reset_for_tests() -> None:
    """
    Clear the entire index. For test isolation only.

    The index is process-global; a leaked entry would change the replay
    behavior of a later test. Not for production callers — there is no
    legitimate runtime use case for wiping it.
    """
    with _lock:
        _unconsumed.clear()
