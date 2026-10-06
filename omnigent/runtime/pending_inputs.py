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
  message (via :func:`resolve_matching_text`, falling back to
  :func:`resolve_oldest`), so the now-committed item doesn't
  double-render alongside a stale pending entry.

Unlike :mod:`pending_elicitations` / :mod:`inflight_text`, this index
is NOT populated through the :func:`session_stream.publish` chokepoint:
recording needs to return the new entry id to the POST handler (so the
sender can adopt it and dedupe cleanly), and draining needs to run at
the persist site so the ``session.input.consumed`` event can carry the
cleared id. Both are caller-driven, so the access is explicit.

Draining matches the mirrored text to its entry first and falls back to
FIFO order (oldest first). Native gives no id channel back through the
TUI to correlate the forwarded POST with the mirrored transcript item.
Position alone is not safe either: a pasted message the TUI never
recorded (a host that died mid-paste, a hook that failed closed) leaves
its entry at the head of the queue, and every later message would then
drain the wrong entry — the receipt names the previous message, clients
settle the wrong bubble, and the new message renders twice. So the
persist site drains the oldest entry whose text equals the mirror
(whitespace collapsed; for a message with attachments, the executor's
generated marker lines — one per file block — are dropped from the mirror
first) and reports the older entries it skipped, which the caller
persists as undelivered. Two queued messages with identical text drain in
queue order: text alone cannot tell them apart, so if the older one was
lost the receipt names it and the later one is surfaced as undelivered
at the next match — the only ambiguity this scheme accepts. When
no entry matches — the transcript may still reformat text in ways not
normalized here — the oldest entry is drained, as before, and every entry
still queued is marked uncertain (:func:`mark_uncertain`): the drained
receipt may really have belonged to one of them, so a later match that
jumps over them drains them quietly instead of recording them as
undelivered.

The one imperfect case is interleaving a web-composer message with a
message typed directly in the TUI: the TUI message (which has no pending
entry and matches none) drains the oldest web entry, so that web bubble
briefly disappears and reappears once it persists. It self-heals; the
committed bubble always renders the just-persisted content regardless.

Limitations (identical to :mod:`pending_elicitations`):

* In-memory only; multi-replica Omnigent deploys would each see their own
  slice. Session events are already process-affine (``session_stream``
  is in-process with no replay, and a session's runner relay + SSE
  subscribers live on one process), so this rides the same affinity.
* Entries do not survive an AP-server restart — acceptable, the loss
  is one in-flight message, same as every other AP-side transient.
* At most :data:`_MAX_ENTRIES_PER_CONVERSATION` unheld entries per
  conversation; :func:`record` evicts the oldest unheld entries beyond that
  and never the entry it just recorded. Entries a persist in progress has
  drained with ``hold=True`` keep their slot until :func:`release` (it
  landed) or :func:`restore` (it did not) settles them, so a queue that
  refills during the persist can never discard the entries a failed append
  has to put back; they are a transient overlay of at most one drain, so
  the queue never exceeds twice the cap. A drain reports at most a cap's
  worth of skipped entries, so the persist site's append stays bounded
  even right after a rolled-back drain left the queue over the cap.
* An image-only message has no text to match, so it drains by position;
  behind a stale head
  entry its image can land on the wrong message. This is the positional
  behavior that predates text matching, kept as a known limitation.

A forwarded message the vendor TUI never accepts (runner crash, dropped
keystrokes) is never persisted, so no mirror drains its entry. The next
text-matched mirror skips it (see :func:`resolve_matching_text`) and the
persist site records it as undelivered; :data:`_TTL_S` bounds a ghost no
later message follows: stale entries are evicted lazily on the next
:func:`record` / :func:`snapshot_for` / :func:`resolve_oldest` for the
same conversation.
"""

from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.inner.native_attachments import ATTACHMENT_MARKER_STRIP_PATTERN

# A pending entry is evicted this many seconds after it was recorded
# if it was never drained by a matching persisted message. Covers the
# vendor-TUI-never-accepted-the-message ghost; long enough that a slow
# transcript round-trip on a busy session still drains normally.
_TTL_S: float = 600.0

# Hard cap on unheld queued entries per conversation, enforced by
# :func:`record`: the oldest unheld entries are evicted when a new one would
# exceed it (never the new one itself). Bounds the snapshot replay and the
# persist site's append (each skipped entry becomes two rows). Far above any
# real queue: nobody sends this many messages within the TTL with none echoed
# back.
_MAX_ENTRIES_PER_CONVERSATION = 64

# One attachment reference line a native executor prepends to a pasted message
# ("[Attached: /tmp/x.png]", "[Attached file: …]", "[Attachment x could not be
# loaded]"), anchored to the start of the text. The matcher removes exactly as
# many of these as the queued message has file blocks, so a marker-like phrase
# the person typed is never mistaken for a generated one.
_ONE_LEADING_ATTACHMENT_MARKER_RE = re.compile(rf"^\s*(?:{ATTACHMENT_MARKER_STRIP_PATTERN})\s*")
_ATTACHMENT_BLOCK_TYPES = frozenset({"input_image", "input_file"})


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
    stable_id: str | None = None
    background_titles_enabled: bool = True


@dataclass
class MatchedDrain:
    """
    Result from draining pending inputs up to a text-matched entry.

    :param matched: The entry whose text the mirror carried, or ``None``.
    :param skipped: Older entries the match jumped over that are known to be
        lost: no mirror of theirs can still arrive, so the caller records them
        as undelivered.
    :param uncertain: Older entries the match jumped over that were queued
        when an unmatched mirror drained by position (see
        :func:`mark_uncertain`). That mirror may have been theirs, so they are
        drained without being declared undelivered.
    """

    matched: DrainedInput | None
    skipped: list[DrainedInput]
    uncertain: list[DrainedInput] = field(default_factory=list)


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
    :param held: ``True`` while a persist in progress has drained this entry
        with ``hold=True``: it keeps its slot and order, other drains skip
        it, and cap eviction leaves it alone until :func:`restore` (the
        persist did not land) or :func:`release` (it did) settles it.
    :param uncertain: ``True`` once an unmatched mirror drained by position
        while this entry was queued. That mirror may have been this entry's
        own, so a later match that jumps over it must not call it undelivered.
    """

    pending_id: str
    content: list[dict[str, Any]]
    created_by: str | None = None
    stable_id: str | None = None
    background_titles_enabled: bool = True
    # Lambda (not ``_now`` directly) so a monkeypatched ``_now`` is
    # resolved at construction time rather than bound at class def.
    created_at: float = field(default_factory=lambda: _now())
    held: bool = False
    uncertain: bool = False


# Per-conversation mapping conversation_id → {pending_id: entry}. The
# inner dict is insertion-ordered (FIFO), which :func:`resolve_oldest`
# relies on to drain the oldest matching message first. Empty inner
# dicts are popped eagerly so the index doesn't accrete stale keys.
_pending: WorkspaceScopedCache[str, dict[str, _Entry]] = WorkspaceScopedCache()
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
    stable_id: str | None = None,
    *,
    background_titles_enabled: bool = True,
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
    :param stable_id: Stable 32-char hex id assigned by the web client to this
        logical message submit. When set, the transcript forwarder uses it
        directly as the persisted item's id so the store-level append is
        idempotent across client retries. ``None`` for clients that do not
        send one.
    :returns: The index-assigned pending id, e.g. ``"pending_a1b2c3"``.

    Beyond :data:`_MAX_ENTRIES_PER_CONVERSATION` unheld entries the oldest
    unheld one is evicted (never this new one), so the queue (and everything
    sized by it) stays bounded without touching entries a persist in progress
    must be able to put back.
    """
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        # If a live entry already carries this stable_id, return its pending_id
        # without creating a new entry — the runner already received this message
        # and re-dispatching it would duplicate the turn.
        if stable_id is not None:
            for existing in _pending.get(conversation_id, {}).values():
                if existing.stable_id == stable_id:
                    return existing.pending_id
        pending_id = f"pending_{uuid.uuid4().hex}"
        entry = _Entry(
            pending_id=pending_id,
            content=content,
            created_by=created_by,
            stable_id=stable_id,
            background_titles_enabled=background_titles_enabled,
        )
        entries = _pending.setdefault(conversation_id, {})
        entries[pending_id] = entry
        _evict_beyond_cap(entries)
    return pending_id


def _evict_beyond_cap(entries: dict[str, _Entry]) -> None:
    """
    Drop the oldest unheld entries until at most the cap remain unheld.

    Caller must hold :data:`_lock`. Insertion order is age order, so the first
    unheld keys go first and the entry just recorded (the newest) is never the
    victim, even when every other entry is held. Held entries belong to a
    persist in progress and keep their slot: discarding one would break the
    rollback that puts it back. They are a transient overlay of at most one
    persist's drain, so the queue never exceeds twice the cap.

    :param entries: One conversation's ``{pending_id: entry}`` map.
    """
    unheld = [pid for pid, entry in entries.items() if not entry.held]
    excess = len(unheld) - _MAX_ENTRIES_PER_CONVERSATION
    if excess <= 0:
        return
    for pending_id in unheld[:excess]:
        entries.pop(pending_id, None)


def pending_id_for_stable_id(conversation_id: str, stable_id: str) -> str | None:
    """
    Return the live pending id recorded for a web client's stable message id.

    Called by the native message route before it records a send. A stable id
    that is already queued is a client retry of a message the runner already
    received (the first response was lost in flight); forwarding it again
    would run the prompt twice, so the route answers with the queued entry.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param stable_id: The client's stable 32-char hex message id.
    :returns: The matching entry's pending id, or ``None`` when the message
        is not queued (never sent, already mirrored, or settled after a failure).
    """
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        for entry in _pending.get(conversation_id, {}).values():
            if entry.stable_id == stable_id:
                return entry.pending_id
    return None


def resolve(conversation_id: str, pending_id: str) -> DrainedInput | None:
    """
    Drop a pending entry by id and return it.

    Called to roll back a :func:`record` whose runner forward failed (so a
    never-delivered message doesn't replay as a ghost bubble), and to settle
    the exact entry a failed native turn named. Idempotent: dropping an
    unknown id is a no-op.

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    :param pending_id: The id returned by :func:`record`, e.g.
        ``"pending_a1b2c3"``.
    :returns: The dropped entry, or ``None`` when no entry had that id.
    """
    with _lock:
        entries = _pending.get(conversation_id)
        if entries is None:
            return None
        entry = entries.pop(pending_id, None)
        if not entries:
            _pending.pop(conversation_id, None)
        return _drained_input(entry) if entry is not None else None


def resolve_oldest(conversation_id: str, *, hold: bool = False) -> DrainedInput | None:
    """
    Drain the oldest pending entry (FIFO) and return it.

    Called at the persist site when a native user message mirrored back
    from the transcript matches no entry by text (see
    :func:`resolve_matching_text`): the transcript can reformat text in
    ways the match does not normalize, and leaving such a message pending
    would double-render it alongside its stale entry. Draining here is by
    insertion order — the oldest entry is the best remaining guess.

    Returns the drained entry (id + content) so the caller can echo the
    id to clients AND merge its file blocks into the durable item — the
    transcript is text-only, so the image would otherwise vanish from
    history. Returns ``None`` when nothing is pending — e.g. a message
    typed directly in the TUI on a session with no queued web messages;
    the caller then renders it as a plain committed item.

    :param conversation_id: Conversation/session id the message was
        persisted on, e.g. ``"conv_abc123"``.
    :param hold: Keep the entry in place, marked held, instead of removing it;
        the caller settles it with :func:`release` once the persist landed or
        :func:`restore` if it did not. Entries already held are skipped.
    :returns: The drained :class:`DrainedInput`, or ``None`` when no
        entry was pending.
    """
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        entries = _pending.get(conversation_id)
        if entries is None:
            return None
        # Insertion order = FIFO; the first unheld key is the oldest entry.
        oldest_id = next((pid for pid, entry in entries.items() if not entry.held), None)
        if oldest_id is None:
            return None
        entry = entries[oldest_id]
        if hold:
            entry.held = True
        else:
            entries.pop(oldest_id)
            if not entries:
                _pending.pop(conversation_id, None)
        return _drained_input(entry)


def mark_uncertain(conversation_id: str) -> None:
    """
    Flag every queued, unheld entry as possibly already mirrored.

    Called right after a mirror that matched no entry drained the oldest one
    by position. The mirror's true owner may be any entry still queued (its
    text was reformatted beyond what matching normalizes), so those entries
    can no longer be declared undelivered with confidence: a later match that
    jumps over them drains them as ``uncertain`` instead of ``skipped``.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    """
    with _lock:
        for entry in _pending.get(conversation_id, {}).values():
            if not entry.held:
                entry.uncertain = True


def restore(conversation_id: str, drained: DrainedInput) -> None:
    """
    Put a drained entry back into the pending queue.

    Compensation for a drain whose persist did not land (a deduplicated
    retry, or an append that raised): the entry belongs to a LATER mirror.
    An entry drained with ``hold=True`` never left the queue, so it is
    simply unheld in place, keeping its slot and order. One that is gone
    (drained without ``hold``, or evicted by the TTL meanwhile) returns to
    the FRONT, since it was the oldest when drained. Compensation never
    evicts: a queue that refilled meanwhile may exceed the cap by the
    restored entries until the next :func:`record` trims unheld ones.

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    :param drained: The entry returned by :func:`resolve_oldest` or
        :func:`resolve_matching_text`.
    """
    entry = _Entry(
        pending_id=drained.pending_id,
        content=copy.deepcopy(drained.content),
        created_by=drained.created_by,
        stable_id=drained.stable_id,
        background_titles_enabled=drained.background_titles_enabled,
    )
    with _lock:
        entries = _pending.get(conversation_id, {})
        current = entries.get(drained.pending_id)
        if current is not None:
            current.held = False
            return
        _pending[conversation_id] = {drained.pending_id: entry, **entries}


def release(conversation_id: str, drained: DrainedInput) -> None:
    """
    Drop a held entry whose persist landed.

    Idempotent: an entry already gone (evicted by the TTL, or drained without
    ``hold``) is a no-op.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param drained: The entry returned by :func:`resolve_oldest` or
        :func:`resolve_matching_text` with ``hold=True``.
    """
    with _lock:
        entries = _pending.get(conversation_id)
        if entries is None:
            return
        entries.pop(drained.pending_id, None)
        if not entries:
            _pending.pop(conversation_id, None)


def resolve_matching_text(conversation_id: str, text: str, *, hold: bool = False) -> MatchedDrain:
    """
    Drain through the first pending entry whose text matches ``text``.

    If an earlier web message was injected but the TUI never recorded it (it
    errored, the host died mid-paste, a hook failed closed), FIFO draining
    would consume that lost entry when the next recorded message is mirrored
    and name it in ``session.input.consumed`` — every client would settle the
    wrong bubble. Matching the mirrored text selects the right entry and
    returns the older skipped entries so the caller can surface them as
    undelivered web messages.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param text: User-message text mirrored from the native transcript.
    :param hold: Keep the matched and skipped entries in place, marked held,
        instead of removing them; the caller settles each with
        :func:`release` or :func:`restore`. Entries already held are skipped.
    :returns: Matched entry plus the older entries it jumped over — at most
        :data:`_MAX_ENTRIES_PER_CONVERSATION` of them, oldest first, split
        into ``skipped`` (known lost) and ``uncertain`` (queued when a
        positional drain happened, see :func:`mark_uncertain`); any beyond
        the cap stay queued for a later drain — or no match with empty lists
        when nothing carries this text (e.g. it was typed directly in the
        TUI).
    """
    exact_needle = _collapse_whitespace(text)
    if not exact_needle:
        return MatchedDrain(matched=None, skipped=[])
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        entries = _pending.get(conversation_id)
        if entries is None:
            return MatchedDrain(matched=None, skipped=[])
        ordered = [(pid, entry) for pid, entry in entries.items() if not entry.held]
        texts = [_collapse_whitespace(_content_text(entry.content)) for _pid, entry in ordered]
        # Two passes. An exact (whitespace-collapsed) match first, so two
        # messages that differ only in a marker-like phrase the person typed
        # at the front stay distinct. Then, for entries carrying attachments:
        # the executor pastes one generated marker line per file block ahead
        # of the text, so drop exactly that many from the mirror and compare
        # with the entry's own text — typed marker-like text still counts.
        match_index = _first_match(texts, exact_needle)
        if match_index is None:
            for index, (_pid, entry) in enumerate(ordered):
                attachments = _attachment_count(entry.content)
                if attachments == 0 or not texts[index]:
                    continue
                if (
                    _collapse_whitespace(_strip_generated_markers(text, attachments))
                    == texts[index]
                ):
                    match_index = index
                    break
        if match_index is None:
            return MatchedDrain(matched=None, skipped=[])
        # Bound one drain's work: report at most a cap's worth of skipped
        # entries (oldest first) and leave the rest queued for later drains, so
        # a queue that overflowed after a rolled-back append never yields an
        # unbounded append downstream. The matched entry itself always drains.
        skipped_entries = ordered[:match_index][:_MAX_ENTRIES_PER_CONVERSATION]
        matched_id, matched_entry = ordered[match_index]
        for pending_id, entry in [*skipped_entries, (matched_id, matched_entry)]:
            if hold:
                entry.held = True
            else:
                entries.pop(pending_id, None)
        if not entries:
            _pending.pop(conversation_id, None)
        return MatchedDrain(
            matched=_drained_input(matched_entry),
            skipped=[
                _drained_input(entry)
                for _pending_id, entry in skipped_entries
                if not entry.uncertain
            ],
            uncertain=[
                _drained_input(entry) for _pending_id, entry in skipped_entries if entry.uncertain
            ],
        )


def has_pending(conversation_id: str) -> bool:
    """
    Report whether a session still holds an un-consumed message.

    Cheap check for the session list: while a message waits for its runner
    to boot, the session is working even though no turn has started yet.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :returns: ``True`` iff at least one non-stale pending entry exists.
    """
    with _lock:
        _evict_stale_locked(conversation_id, _now())
        return bool(_pending.get(conversation_id))


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
        stable_id=entry.stable_id,
        background_titles_enabled=entry.background_titles_enabled,
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


def _first_match(texts: list[str], needle: str) -> int | None:
    """
    Index of the first non-empty text equal to ``needle``.

    Equality only: an unanchored suffix check (``"noyes".endswith("yes")``)
    can pick an unrelated queued entry whenever its text happens to trail a
    different accepted prompt, handing that entry's file attachments to the
    wrong persisted message.

    :param texts: Whitespace-collapsed queued entry texts in queue order.
    :param needle: The whitespace-collapsed mirrored text.
    :returns: The matching index, or ``None``.
    """
    if not needle:
        return None
    for index, text in enumerate(texts):
        if text and text == needle:
            return index
    return None


def _collapse_whitespace(text: str) -> str:
    """Collapse whitespace runs so paste and mirror spacing differences cancel out."""
    return " ".join(text.split())


def _attachment_count(content: list[dict[str, Any]]) -> int:
    """Number of file blocks in queued content — one generated marker line each."""
    return sum(
        1
        for block in content
        if isinstance(block, dict) and block.get("type") in _ATTACHMENT_BLOCK_TYPES
    )


def _strip_generated_markers(text: str, count: int) -> str:
    """
    Drop up to ``count`` generated attachment marker lines from the front of a mirror.

    Stops early when the text has fewer leading marker lines, so nothing but
    the executor's own prefix is ever removed.

    :param text: Mirrored transcript text, e.g.
        ``"[Attached: /tmp/x.png]\\n\\nlook at this"``.
    :param count: File blocks on the queued message the mirror is compared to.
    :returns: The text behind the generated markers, e.g. ``"look at this"``.
    """
    for _ in range(count):
        stripped, removed = _ONE_LEADING_ATTACHMENT_MARKER_RE.subn("", text, count=1)
        if not removed:
            break
        text = stripped
    return text


def reset_for_tests() -> None:
    """
    Clear the entire index. For test isolation only.

    The index is process-global; a leaked entry would change the replay
    behavior of a later test. Not for production callers — there is no
    legitimate runtime use case for wiping it.
    """
    with _lock:
        _pending.clear()
