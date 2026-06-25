"""In-process index of un-consumed web-composer user messages.

Backs the optimistic "queued message" bubble for native-terminal
sessions (claude-native / codex-native) so it survives a client
re-bind. On those sessions the Omnigent server does NOT persist a web-typed
user message at POST time — the message is forwarded into the vendor
TUI and the transcript forwarder later mirrors it back as the single
durable writer (see ``_dispatch_session_event_to_runner``). Until that
round-trip completes the message lives nowhere on the server, so a
client that navigates away and back (or whose SSE pump rebinds
mid-flight via ``ensureBoundSession``) loses the optimistic bubble it
rendered locally — it reappears only once the transcript persists it.

This index closes that window with the same shape the codebase already
uses for transient recovery state (:mod:`pending_elicitations`,
:mod:`inflight_text`):

* populated by the route layer on a native web message POST (via
  :func:`record`, before the runner forward), so the message is known
  server-side immediately;
* replayed into the cold-load snapshot (``GET /v1/sessions/{id}``) via
  :func:`snapshot_for`, so a (re)connecting client re-hydrates the
  bubble instead of showing nothing;
* drained when the transcript forwarder persists the matching user
  message (via :func:`resolve_oldest`), so the now-committed item
  doesn't double-render alongside a stale pending entry.

Unlike :mod:`pending_elicitations` / :mod:`inflight_text`, this index
is NOT populated through the :func:`session_stream.publish` chokepoint:
recording needs to return the new entry id to the POST handler (so the
sender can adopt it and dedupe cleanly), and draining needs to run at
the persist site so the ``session.input.consumed`` event can carry the
cleared id. Both are caller-driven, so the access is explicit.

Draining is by FIFO order (oldest first), NOT by text. Native gives no
id channel back through the TUI to correlate the forwarded POST with the
mirrored transcript item, and the transcript freely reformats the text
(reply-quote ``>`` blockquotes, ``[Attached:]`` markers, whitespace), so
matching on text is unreliable — it would leave a reformatted message
stuck pending and double-rendered. Per-session SSE ordering guarantees
the i-th persisted user message corresponds to the i-th queued one, so
each persisted native user message drains the oldest pending entry.

The one imperfect case is interleaving a web-composer message with a
message typed directly in the TUI: the TUI message (which has no pending
entry) drains the oldest web entry, so that web bubble briefly
disappears and reappears once it persists. It self-heals; the committed
bubble always renders the just-persisted content regardless.

Limitations (identical to :mod:`pending_elicitations`):

* In-memory only; multi-replica Omnigent deploys would each see their own
  slice. Session events are already process-affine (``session_stream``
  is in-process with no replay, and a session's runner relay + SSE
  subscribers live on one process), so this rides the same affinity.
* Entries do not survive an AP-server restart — acceptable, the loss
  is one in-flight message, same as every other AP-side transient.

A forwarded message the vendor TUI never accepts (runner crash, dropped
keystrokes) is never persisted, so :func:`resolve_oldest` never
drains its entry. :data:`_TTL_S` bounds that ghost: stale entries are
evicted lazily on the next :func:`record` / :func:`snapshot_for` /
:func:`resolve_oldest` for the same conversation.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# A pending entry is evicted this many seconds after it was recorded
# if it was never drained by a matching persisted message. Covers the
# vendor-TUI-never-accepted-the-message ghost; long enough that a slow
# transcript round-trip on a busy session still drains normally.
_TTL_S: float = 600.0


def _now() -> float:
    """
    Return the current monotonic clock reading for TTL bookkeeping.

    Indirection point (not ``time.monotonic`` directly) so tests can
    advance the clock to exercise stale-entry eviction without a real
    sleep, and so the :class:`_Entry` default factory resolves the
    patched function at call time rather than binding the original.

    :returns: ``time.monotonic()`` seconds.
    """
    return time.monotonic()


@dataclass
class DrainedInput:
    """
    The pending entry drained by :func:`resolve_oldest`.

    :param pending_id: The drained entry's id, e.g. ``"pending_a1b2c3"``
        — echoed to clients as ``cleared_pending_id`` so they drop the
        matching optimistic bubble by id.
    :param content: The drained entry's message content blocks, e.g.
        ``[{"type": "input_image", "file_id": "file_x", "filename":
        "a.png"}, {"type": "input_text", "text": "hi"}]``. The caller
        merges the file blocks into the durably-persisted item, since
        the native transcript round-trip is text-only and would
        otherwise drop the image from history.
    :param created_by: Authenticated identity of the user who posted
        the message, e.g. ``"alice@example.com"``. ``None`` when the
        entry was recorded before this field was introduced or when
        the posting actor was unknown. Applied to the persisted item
        so ``session.input.consumed`` carries the correct author on
        all clients (including collaborators who never saw the
        optimistic bubble).
    """

    pending_id: str
    content: list[dict[str, Any]]
    created_by: str | None = None


@dataclass
class MatchedDrain:
    """Result from draining pending inputs up to a text-matched entry."""

    matched: DrainedInput | None
    skipped: list[DrainedInput]


@dataclass
class _Entry:
    """
    One un-consumed web-composer user message.

    :param pending_id: Index-assigned id for this entry, e.g.
        ``"pending_a1b2c3"``. Returned by :func:`record`, surfaced in
        :func:`snapshot_for`, and echoed back as the cleared id by
        :func:`resolve_oldest` so the client can drop the bubble by
        id.
    :param content: The message content blocks exactly as POSTed, e.g.
        ``[{"type": "input_text", "text": "hi"}]`` (file blocks carry
        real ``file_id``s, since the client uploads before POSTing).
        Replayed verbatim into the snapshot.
    :param created_by: Authenticated identity of the posting actor,
        e.g. ``"alice@example.com"``. ``None`` when unknown. Persisted
        through to the committed item so ``session.input.consumed``
        carries the correct author on all clients.
    :param created_at: ``time.monotonic()`` timestamp at record time,
        used only for TTL eviction.
    """

    pending_id: str
    content: list[dict[str, Any]]
    created_by: str | None = None
    # Lambda (not ``_now`` directly) so a monkeypatched ``_now`` is
    # resolved at construction time rather than bound at class def.
    created_at: float = field(default_factory=lambda: _now())


# Per-conversation mapping conversation_id → {pending_id: entry}. The
# inner dict is insertion-ordered (FIFO), which :func:`resolve_oldest`
# relies on to drain the oldest matching message first. Empty inner
# dicts are popped eagerly so the index doesn't accrete stale keys.
_pending: dict[str, dict[str, _Entry]] = {}
_lock = threading.Lock()


def _evict_stale_locked(conversation_id: str, now: float) -> None:
    """
    Drop entries older than :data:`_TTL_S` for one conversation.

    Caller must hold :data:`_lock`. Pops the conversation key entirely
    once its last entry is evicted so :func:`snapshot_for` returns an
    empty list cleanly.

    :param conversation_id: Conversation/session id to sweep,
        e.g. ``"conv_abc123"``.
    :param now: Current ``time.monotonic()`` value to compare against.
    """
    entries = _pending.get(conversation_id)
    if entries is None:
        return
    stale = [pid for pid, entry in entries.items() if now - entry.created_at > _TTL_S]
    for pid in stale:
        entries.pop(pid, None)
    if not entries:
        _pending.pop(conversation_id, None)


def record(
    conversation_id: str,
    content: list[dict[str, Any]],
    created_by: str | None = None,
) -> str:
    """
    Record an un-consumed web-composer user message.

    Called by the route layer for a native-terminal session's web
    message POST, before forwarding to the runner, so the message is
    known server-side immediately and a (re)connecting client can
    replay it via :func:`snapshot_for`. Roll back with :func:`resolve`
    if the forward fails.

    :param conversation_id: Conversation/session id the message was
        posted to, e.g. ``"conv_abc123"``.
    :param content: Message content blocks as POSTed, e.g.
        ``[{"type": "input_text", "text": "hi"}]``.
    :param created_by: Authenticated identity of the posting actor,
        e.g. ``"alice@example.com"``. ``None`` when unknown. Stored
        so :func:`resolve_oldest` can apply it to the persisted item
        and broadcast it via ``session.input.consumed``.
    :returns: The index-assigned pending id, e.g. ``"pending_a1b2c3"``.
    """
    pending_id = f"pending_{uuid.uuid4().hex}"
    entry = _Entry(pending_id=pending_id, content=content, created_by=created_by)
    with _lock:
        _evict_stale_locked(conversation_id, entry.created_at)
        _pending.setdefault(conversation_id, {})[pending_id] = entry
    return pending_id


def resolve(conversation_id: str, pending_id: str) -> None:
    """
    Drop a pending entry by id.

    Called to roll back a :func:`record` whose runner forward failed
    (so a never-delivered message doesn't replay as a ghost bubble).
    Idempotent: dropping an unknown id is a no-op.

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    :param pending_id: The id returned by :func:`record`, e.g.
        ``"pending_a1b2c3"``.
    """
    with _lock:
        entries = _pending.get(conversation_id)
        if entries is None:
            return
        entries.pop(pending_id, None)
        if not entries:
            _pending.pop(conversation_id, None)


def resolve_oldest(conversation_id: str) -> DrainedInput | None:
    """
    Drain the oldest pending entry (FIFO) and return it.

    Called at the persist site when a native user message is mirrored
    back from the transcript, so the now-committed item doesn't
    double-render alongside its stale pending entry. Draining is by
    insertion order, NOT text: per-session SSE ordering guarantees the
    i-th persisted user message is the i-th queued one, and the
    transcript reformats text (reply-quote blockquotes, ``[Attached:]``
    markers) in ways a text match can't survive.

    Returns the drained entry (id + content) so the caller can echo the
    id to clients AND merge its file blocks into the durable item — the
    transcript is text-only, so the image would otherwise vanish from
    history. Returns ``None`` when nothing is pending — e.g. a message
    typed directly in the TUI on a session with no queued web messages;
    the caller then renders it as a plain committed item.

    :param conversation_id: Conversation/session id the message was
        persisted on, e.g. ``"conv_abc123"``.
    :returns: The drained :class:`DrainedInput`, or ``None`` when no
        entry was pending.
    """
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        entries = _pending.get(conversation_id)
        if entries is None:
            return None
        # Insertion order = FIFO; the first key is the oldest entry.
        oldest_id = next(iter(entries))
        entry = entries.pop(oldest_id)
        if not entries:
            _pending.pop(conversation_id, None)
        return DrainedInput(
            pending_id=entry.pending_id,
            content=copy.deepcopy(entry.content),
            created_by=entry.created_by,
        )


def resolve_matching_text(conversation_id: str, text: str) -> MatchedDrain:
    """
    Drain through the first pending entry whose text matches ``text``.

    Kiro persists accepted web prompts as structured ``Prompt`` records. If an
    earlier injected web message errors before Kiro records a prompt, FIFO
    draining would consume that failed entry when the next successful prompt is
    mirrored, leaving the successful prompt stuck pending. This resolver lets
    Kiro match the accepted prompt text and returns any older skipped entries so
    the caller can surface them as failed web injections.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param text: Accepted prompt text mirrored from Kiro's structured JSONL.
    :returns: Matched entry plus older skipped entries, or no match with an
        empty skipped list when the text was typed directly in the TUI.
    """
    needle = _normalize_text(text)
    if not needle:
        return MatchedDrain(matched=None, skipped=[])
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        entries = _pending.get(conversation_id)
        if entries is None:
            return MatchedDrain(matched=None, skipped=[])
        ordered = list(entries.items())
        match_index: int | None = None
        for index, (_pending_id, entry) in enumerate(ordered):
            entry_text = _normalize_text(_content_text(entry.content))
            if entry_text and (needle == entry_text or needle.endswith(entry_text)):
                match_index = index
                break
        if match_index is None:
            return MatchedDrain(matched=None, skipped=[])
        skipped_entries = ordered[:match_index]
        _matched_id, matched_entry = ordered[match_index]
        for pending_id, _entry in ordered[: match_index + 1]:
            entries.pop(pending_id, None)
        if not entries:
            _pending.pop(conversation_id, None)
        return MatchedDrain(
            matched=_drained_input(matched_entry),
            skipped=[_drained_input(entry) for _pending_id, entry in skipped_entries],
        )


def snapshot_for(conversation_id: str) -> list[dict[str, Any]]:
    """
    Return un-consumed messages for one session, for snapshot replay.

    Read by ``GET /v1/sessions/{id}`` so a client that (re)connects
    after posting a native web message (or after navigating away and
    back) re-hydrates the optimistic bubble. The live SSE stream has no
    replay buffer, so without this the bubble would show nothing until
    the transcript round-trip persists the message.

    Returns deep copies of the stored content so a caller mutating the
    replayed entry cannot poison the index.

    :param conversation_id: Conversation/session id to query, e.g.
        ``"conv_abc123"``.
    :returns: Insertion-ordered list of dicts, each with ``"pending_id"``
        and ``"content"`` keys, plus an optional ``"created_by"`` key
        when the sender identity was recorded at
        :func:`record` time, e.g. ``[{"pending_id": "pending_a1b2c3",
        "content": [{"type": "input_text", "text": "hi"}],
        "created_by": "alice@example.com"}]``.  ``"created_by"`` is
        omitted (not ``null``) when unknown, keeping the wire shape
        backward-compatible with older clients.  Empty list when the
        session has no un-consumed messages.
    """
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        entries = _pending.get(conversation_id)
        if entries is None:
            return []
        return [
            {
                "pending_id": entry.pending_id,
                "content": copy.deepcopy(entry.content),
                **({"created_by": entry.created_by} if entry.created_by is not None else {}),
            }
            for entry in entries.values()
        ]


def _drained_input(entry: _Entry) -> DrainedInput:
    """Copy a pending entry into the public drained shape."""
    return DrainedInput(
        pending_id=entry.pending_id,
        content=copy.deepcopy(entry.content),
        created_by=entry.created_by,
    )


def _content_text(content: list[dict[str, Any]]) -> str:
    """Extract text blocks from a pending-input content list."""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"input_text", "text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _normalize_text(text: str) -> str:
    """Normalize text enough to compare pending input with Kiro Prompt text."""
    return " ".join(text.split())


def reset_for_tests() -> None:
    """
    Clear the entire index. For test isolation only.

    The index is process-global; a leaked entry would change the replay
    behavior of a later test. Not for production callers — there is no
    legitimate runtime use case for wiping it.
    """
    with _lock:
        _pending.clear()
