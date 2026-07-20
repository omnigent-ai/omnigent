"""In-process index of the assistant text streaming in the current turn.

Lets the Omnigent server replay the text streamed so far when a client
(re)connects mid-turn — fixing the bug where, for non-claude-native
agents (e.g. polly), a cold reload / new tab / navigate-away-and-back
showed only "a few tokens" of an in-flight response and the visible
text differed on every reload.

Why the index exists
--------------------
Scaffold / agent-loop harnesses emit assistant text *only* as
``response.output_text.delta`` events — there is no
``response.output_item.done`` message item until the turn ends. The
AP relay (``_relay_runner_stream``) accumulates those deltas in a
local list and persists assistant ``message`` segments only at
tool-call boundaries and on the turn's terminal ``response.*`` event.
So while a turn is in flight the text lives nowhere durable:

* it is not in the conversation store (no message persisted yet), so
  the cold-load snapshot (``GET /v1/sessions/{id}``) has nothing;
* :func:`omnigent.runtime.session_stream` is pure fan-out with no
  replay buffer, so a fresh subscriber only receives deltas published
  *after* it connected.

A reconnecting client therefore renders an empty bubble plus whatever
tail of deltas happened to arrive after it resubscribed — "a few
tokens", different every reload.

Native providers have the same gap for the message currently streaming:
the in-flight message streams as ``output_text.delta`` events carrying a
per-message ``message_id`` and is not yet in the store. Those are
tracked separately (see :data:`_native_inflight`), keyed by
``message_id`` and dropped per-message when the message's
``response.output_item.done`` commits, then replayed by
:func:`snapshot_for` as message-scoped deltas so the reconnecting client
rebuilds the same in-flight preview.

What it does
------------
The index is populated automatically by
:func:`omnigent.runtime.session_stream.publish` (the single SSE
chokepoint, same as :mod:`omnigent.runtime.pending_elicitations`):
it captures the turn's :class:`ResponseObject` from
``response.created`` / ``response.in_progress`` and accumulates
``response.output_text.delta`` text, then clears it when the turn
ends — on a terminal ``response.*`` event, on a terminal
``session.status`` (``idle``/``failed``, which catches Stop / delete /
setup-failure / policy-deny turn-ends that emit no ``response.*``), and
via :func:`discard` from the relay's teardown (runner death / rebind).
:func:`snapshot_for` is read by the ``/stream`` route via
``subscribe``'s ``pre_ready_snapshot`` hook and replays the
streamed-so-far text as a ``response.created`` +
``response.output_text.delta`` pair, so a reconnecting client's reducer
ends up in the same state as one that connected at turn start.

Reasoning deltas are intentionally NOT tracked — reasoning is
throwaway and may legitimately differ on reload (an earlier
reasoning-persistence fix was the wrong layer).

Deliberately ephemeral
----------------------
The text is never written to the conversation store; the final
assistant message still persists on ``response.completed`` exactly as
before, and the index is cleared at that point. Nothing here pollutes
the durable transcript. The index lives only in the Omnigent process, so it
does not survive an AP-server restart mid-turn — acceptable, because
the relay's in-memory accumulator does not survive a restart either,
and the only loss is the in-flight prefix of a single turn.

Lifecycle correctness (no gap, no duplicate)
--------------------------------------------
:func:`snapshot_for` must be read through ``subscribe``'s
``pre_ready_snapshot`` hook, which runs synchronously right after the
subscriber's queue slot is registered and *before the first
``yield``/``await``*. On the single Omnigent event loop where the relay
publishes, no delta can be published between slot registration and that
read, so deltas before that instant are in the snapshot prefix and
deltas at/after it land on the subscriber's queue (the live tail). The
two partition exactly. Reading it from the async ``on_subscribed`` hook
instead is a bug: ``on_subscribed`` runs *after*
``yield ready_event`` suspends, so deltas streamed in that gap land in
BOTH the snapshot and the queue and render twice.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Terminal turn-lifecycle event types. Any of these clears the
# conversation's in-flight entry: the turn is over, so its streamed
# text is either about to be persisted (``completed``) or discarded.
_TERMINAL_EVENT_TYPES = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
    }
)

# ``session.status`` values that mean no turn is active, so any tracked
# in-flight text belongs to a turn that ended WITHOUT a terminal
# ``response.*`` event and must be dropped. This covers turn-ends that
# the ``_TERMINAL_EVENT_TYPES`` set misses: a web Stop / session-delete
# (cancels the turn → ``session.status: idle``, no ``response.cancelled``),
# a SETUP-phase failure (``failed`` with no ``response.failed``), and the
# synthetic policy-deny notice (a bare ``output_text.delta`` bracketed by
# ``running``/``idle`` with no lifecycle envelope). Without this the entry
# would linger and replay stale text on the next reload.
_TERMINAL_STATUS_VALUES = frozenset({"idle", "failed"})


@dataclass
class _InFlightTurn:
    """
    The assistant text accumulated for one in-flight turn.

    :param response_id: The turn's response id, used both to detect a
        new turn (a different id resets accumulation) and to group the
        replayed events into the right bubble, e.g. ``"resp_abc123"``.
        ``None`` only in the anomalous case where a text delta arrived
        before any lifecycle event (no id captured yet).
    :param response: The turn's :class:`ResponseObject` serialized as a
        dict, captured verbatim from the ``response.created`` /
        ``response.in_progress`` event so :func:`snapshot_for` can
        replay a faithful ``response.created`` (carrying ``id`` and
        ``model``), or ``None`` if no lifecycle event was seen yet,
        e.g. ``{"id": "resp_abc", "model": "polly", "status":
        "in_progress", "created_at": 1730000000}``.
    :param parts: Accumulated ``response.output_text.delta`` strings in
        arrival order, e.g. ``["Let me ", "plan this."]``.
    """

    response_id: str | None = None
    response: dict[str, Any] | None = None
    parts: list[str] = field(default_factory=list)


# Per-conversation mapping of conversation_id → in-flight turn text.
# Present only while a turn is streaming; popped on any terminal event.
# Populated by ``record_publish`` on the SSE publish chokepoint; read
# by ``snapshot_for`` from ``subscribe``'s ``pre_ready_snapshot`` hook.
_inflight: dict[str, _InFlightTurn] = {}
_lock = threading.Lock()


@dataclass
class _NativeMessage:
    """
    Streamed text for ONE in-flight native assistant message.

    Unlike the response-scoped :class:`_InFlightTurn` (which blobs an
    in-process agent's whole turn under one ``response_id``), native
    providers stream text per assistant message keyed by a vendor
    ``message_id`` and emits no ``response.created``. Each message's
    chunks are tracked separately so :func:`snapshot_for` can replay
    them as message-scoped ``response.output_text.delta`` events (still
    carrying ``message_id``), letting a reconnecting client rebuild the
    same per-message in-flight previews it would have streamed live.

    :param parts: Accumulated delta strings in arrival (index) order,
        e.g. ``["Let me ", "check that."]``.
    :param last_index: Highest chunk ``index`` accumulated so far, e.g.
        ``4``. Replayed so the client's live tail (deltas published
        after reconnect, at higher indices) appends without duplicating
        the replayed prefix.
    :param final_seen: Whether the message's ``final: true`` chunk has
        arrived. For providers that emit final chunks, only once it has
        is ``"".join(parts)`` the COMPLETE message text and thus safe to
        byte-compare against a committed ``output_item.done`` text
        (before that it is a prefix). Codex-native does not emit final
        chunks, so it is retired only by a byte-equal committed item.
        See :func:`record_publish`.
    """

    parts: list[str] = field(default_factory=list)
    last_index: int = -1
    final_seen: bool = False


# Per-conversation, insertion-ordered mapping of message_id → its
# in-flight streamed text, for native message-scoped streaming.
# A message is added on its first ``output_text.delta`` and dropped when
# its authoritative ``response.output_item.done`` commits — matched to the
# committed item by BYTE-EQUAL text (``"".join(parts) == committed text``),
# NOT by position. The done event carries no message_id (its id is an AP id
# derived from the transcript uuid, a different namespace than the delta's
# message_id), and the prior FIFO "drop the oldest" guess was wrong for the
# common single-chunk message whose only (``final``) chunk is POSTed AFTER
# its committed item — at commit time that message isn't tracked yet, so
# FIFO popped the wrong entry (or none) and never retired the real id. The
# probe confirmed the streamed text equals the transcript text byte-for-
# byte, so content is a reliable join key. So the index only ever holds
# messages NOT yet in the conversation store, and replay can't double-
# render a committed message. NOT cleared on ``session.status: idle``
# (claude-native goes idle mid-turn while parked on a permission prompt,
# with text that hasn't committed — see the session.status branch); the
# per-message ``output_item.done`` and :func:`discard` (relay teardown)
# are the eviction paths.
_native_inflight: dict[str, dict[str, _NativeMessage]] = {}

# Per-conversation, insertion-ordered set of message_ids whose message has
# committed — dropped from ``_native_inflight`` and barred from re-entry.
# The forwarder tails the deltas file separately from the transcript, so a
# message's last chunk can be POSTed just AFTER its committed item. Without
# this guard that late ``output_text.delta`` would re-create the just-
# dropped entry; since native providers can end turns with ``session.status:
# idle`` (no terminal ``response.*`` that would clear the index), the
# resurrected entry then lingers and :func:`snapshot_for` replays it on the
# next reconnect as a stale duplicate. It also drives the LIVE drop:
# :func:`record_publish` returns a "suppress" verdict for any delta whose
# message_id is retired, and :func:`omnigent.runtime.session_stream.publish`
# withholds it from the live fan-out (mirrors the web client's
# ``retiredLiveMessages`` in web ``chatStore.ts``). Insertion-ordered +
# bounded so a long-lived session can't grow it without limit (vendor
# message_ids are unique, so an evicted-then-revived id is not a real
# concern — the race window is a single forwarder poll).
_native_retired: dict[str, dict[str, None]] = {}

# Per-conversation multiset of committed-message text fingerprints awaiting
# their deltas, keyed by HASH → list of monotonic buffered-at timestamps
# (one per occurrence). Populated when an ``output_item.done`` commits but
# NO in-flight message yet matches its text — i.e. the deltas raced behind
# the commit (the single-chunk case). When a message's ``final`` chunk later
# arrives and ``sha256("".join(parts))`` matches a NON-EXPIRED buffered
# fingerprint, that message is the committed one: it is retired and its
# (duplicate) chunk suppressed from the live stream. A multiset (a timestamp
# list per hash) lets two messages with identical content be reconciled by
# count — we only need to retire one message per committed text, and
# identical text renders identically regardless of which physical message it
# was. The per-occurrence timestamp drives the TTL (see
# :data:`_NATIVE_COMMITTED_TTL_S`): native providers may emit no terminal
# ``response.*`` to clear this buffer, so a fingerprint that never matches
# (a multi-text-block mismatch, or a delta dropped by the best-effort
# forwarder) would otherwise persist for the whole session and could
# mis-suppress a later identical-text message ("OK"/"Done." repeats). Drained
# on match; cleared wholesale on turn-end / teardown alongside the other
# native state.
_native_committed: dict[str, dict[str, list[float]]] = {}

# Cap on retired message_ids tracked per conversation. Far larger than the
# one-or-two-message race window; bounds memory on a long session.
_MAX_NATIVE_RETIRED_PER_CONV = 256

# Cap on distinct committed-text fingerprints buffered per conversation.
# These normally drain within one forwarder poll (the matching delta lands
# right after); the cap is a backstop against a message whose deltas never
# arrive (e.g. a multi-text-block message — see :func:`record_publish`) so
# the buffer can't grow unbounded mid-turn. Oldest fingerprint evicted
# first; an evicted entry just means a late chunk for it won't be
# suppressed (a possible — not guaranteed — duplicate), never a crash.
_MAX_NATIVE_COMMITTED_PER_CONV = 256

# How long a buffered committed-text fingerprint stays a valid suppression
# match. The real commit→delta race is ~1 forwarder poll (~0.25s); this sits
# far above that so the legitimate single-chunk race is always covered, yet
# bounds how long an unmatched fingerprint can mis-suppress a later
# identical-text message to a few seconds rather than the whole session
# (claude-native never emits a terminal ``response.*`` to clear it). Failure
# direction is safe: an expired fingerprint is simply not matched, so the
# delta is delivered (a possible transient duplicate) rather than a real
# message being hidden.
_NATIVE_COMMITTED_TTL_S = 10.0


def _monotonic() -> float:
    """
    Monotonic clock reading for the committed-fingerprint TTL.

    Thin indirection so tests can drive the TTL deterministically without
    patching the process-global ``time.monotonic`` (which would leak across
    tasks / pytest-xdist workers — see the project's no-global-singleton-
    patch test rule). Patch THIS helper instead.

    :returns: Seconds from an unspecified monotonic epoch.
    """
    return time.monotonic()


def _text_fingerprint(text: str) -> str:
    """
    Return a stable content fingerprint for committed/streamed text.

    Used as the byte-equal join key between a claude-native message's
    streamed deltas and its committed ``output_item.done`` text. SHA-256
    of the UTF-8 bytes: collision-free for this purpose and cheaper to
    hold/compare than the raw (possibly multi-KB) message text.

    :param text: The full message text, e.g. ``"Here is the answer."``.
    :returns: Hex digest, e.g. ``"a1b2c3..."``.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _committed_message_text(item: dict[str, Any]) -> str | None:
    """
    Extract the assistant text from a committed ``message`` item.

    Joins the ``output_text`` blocks of the item's ``content`` so the
    result can be byte-compared against a streamed message's joined
    deltas. Returns ``None`` when the item carries no output text (e.g. a
    shape with only non-text blocks) — the caller then neither matches nor
    buffers it, leaving the native index untouched.

    :param item: The ``event["item"]`` dict of a
        ``response.output_item.done`` whose ``type`` is ``"message"``,
        e.g. ``{"type": "message", "role": "assistant", "content":
        [{"type": "output_text", "text": "Hi"}]}``.
    :returns: The joined output text, or ``None`` if there is none.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "output_text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    if not parts:
        return None
    return "".join(parts)


def _retire_native_message(conversation_id: str, message_id: str) -> None:
    """
    Drop an in-flight native message and bar its id from re-entry.

    Caller MUST hold :data:`_lock`. Removes ``message_id`` from
    :data:`_native_inflight` (so :func:`snapshot_for` stops replaying its
    preview) and records it in :data:`_native_retired` (so a late trailing
    chunk for it is dropped from the index AND withheld from the live
    stream). Bounded; oldest retired id evicted first.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc"``.
    :param message_id: Vendor message id to retire, e.g. ``"m1"``.
    """
    messages = _native_inflight.get(conversation_id)
    if messages is not None:
        messages.pop(message_id, None)
        if not messages:
            _native_inflight.pop(conversation_id, None)
    retired = _native_retired.setdefault(conversation_id, {})
    retired[message_id] = None
    while len(retired) > _MAX_NATIVE_RETIRED_PER_CONV:
        del retired[next(iter(retired))]


def _is_codex_message_id(message_id: str) -> bool:
    """
    Return whether a native stream id came from Codex.

    Codex forwarder ids are constructed as
    ``"codex:<thread>:<turn>:<item_type>:<item_id>"``. Unlike
    Claude's message-display hook, Codex does not emit a ``final:
    true`` chunk; its ``item/completed`` event is the completion
    signal. The commit path can therefore retire a Codex preview when
    its accumulated text byte-matches the committed item even though
    ``final_seen`` is false.

    :param message_id: Vendor message id, e.g.
        ``"codex:thread_123:turn_123:agentMessage:item_1"``.
    :returns: ``True`` for Codex-native message ids.
    """
    return message_id.startswith("codex:")


def _match_committed_native_message(conversation_id: str, fingerprint: str) -> str | None:
    """
    Return the in-flight native message id whose COMPLETE text matches.

    Caller MUST hold :data:`_lock`. For Claude-native, only messages
    whose ``final`` chunk has arrived are candidates — before that
    ``"".join(parts)`` is just a prefix and could never equal the
    committed text. Codex-native is the exception: it does not emit
    ``final: true`` chunks, but its completed item arrives after the
    forwarder flushes all deltas for that item, so a byte-equal Codex
    preview is complete enough to retire. First match wins (insertion
    order); two messages with identical text are interchangeable for
    retirement, since identical content renders identically regardless
    of which physical message committed.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc"``.
    :param fingerprint: Committed text fingerprint to match against,
        from :func:`_text_fingerprint`.
    :returns: The matching message id, or ``None`` if none is complete
        and byte-equal yet.
    """
    messages = _native_inflight.get(conversation_id)
    if not messages:
        return None
    for message_id, message in messages.items():
        can_match = message.final_seen or _is_codex_message_id(message_id)
        if can_match and _text_fingerprint("".join(message.parts)) == fingerprint:
            return message_id
    return None


def _consume_committed_fingerprint(conversation_id: str, parts: list[str]) -> bool:
    """
    Pop one NON-EXPIRED buffered fingerprint matching ``parts``, if present.

    Caller MUST hold :data:`_lock`. Returns ``True`` when the joined text's
    fingerprint was buffered in :data:`_native_committed` within the last
    :data:`_NATIVE_COMMITTED_TTL_S` seconds — i.e. this message's
    ``output_item.done`` committed BEFORE its deltas arrived (the single-
    chunk race), so the message just completed is the committed one. Removes
    one fresh occurrence (popping the conversation when it empties).

    Expired occurrences are evicted as a side effect: a fingerprint older
    than the TTL belongs to an earlier, unrelated commit that never matched
    (a multi-text-block mismatch, or a delta the best-effort forwarder
    dropped). It must NOT suppress this independent message, so it is
    discarded and ``False`` is returned if nothing fresh remains — see
    :data:`_native_committed`.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc"``.
    :param parts: The message's accumulated, now-complete delta strings.
    :returns: ``True`` if a fresh buffered fingerprint matched and was
        consumed; ``False`` if none was buffered or all were expired.
    """
    committed = _native_committed.get(conversation_id)
    if not committed:
        return False
    fingerprint = _text_fingerprint("".join(parts))
    timestamps = committed.get(fingerprint)
    if not timestamps:
        return False
    # Drop occurrences that have outlived the commit→delta race window;
    # keep only those still within the TTL as valid suppression matches.
    cutoff = _monotonic() - _NATIVE_COMMITTED_TTL_S
    fresh = [ts for ts in timestamps if ts >= cutoff]
    matched = bool(fresh)
    if matched:
        # Consume one fresh occurrence (oldest of the fresh).
        fresh.pop(0)
    if fresh:
        committed[fingerprint] = fresh
    else:
        del committed[fingerprint]
    if not committed:
        _native_committed.pop(conversation_id, None)
    return matched


def _buffer_committed_fingerprint(conversation_id: str, fingerprint: str) -> None:
    """
    Buffer a committed-message fingerprint awaiting its (late) deltas.

    Caller MUST hold :data:`_lock`. Used when an ``output_item.done``
    commits but no in-flight message matches its text yet (the deltas
    raced behind the commit — the single-chunk case). The message's
    ``final`` chunk consumes it via :func:`_consume_committed_fingerprint`
    (if it arrives within :data:`_NATIVE_COMMITTED_TTL_S`). Stamped with the
    monotonic time so the TTL can later distinguish this commit's racing
    delta from an identical-text message a future turn. Bounded; oldest
    fingerprint evicted first (an evicted entry just means a late chunk for
    it won't be suppressed, never a crash).

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc"``.
    :param fingerprint: Committed text fingerprint, from
        :func:`_text_fingerprint`.
    """
    committed = _native_committed.setdefault(conversation_id, {})
    committed.setdefault(fingerprint, []).append(_monotonic())
    while len(committed) > _MAX_NATIVE_COMMITTED_PER_CONV:
        del committed[next(iter(committed))]


def record_publish(conversation_id: str, event: dict[str, Any]) -> bool:
    """
    Update the index from an SSE event on the publish path.

    Acts on these event groups and ignores every other type with a
    single dict-key lookup (this sits on the hot publish path):

    * ``response.created`` / ``response.in_progress`` — capture the
      turn's :class:`ResponseObject`. A response id different from the
      tracked one starts a fresh turn (resets the accumulated text);
      the same id refreshes the stored response object (e.g. the
      ``in_progress`` status update that follows ``created``).
    * ``response.output_text.delta`` WITHOUT a ``message_id`` — append
      the delta to the current (response-scoped) turn's accumulated text.
    * ``response.output_text.delta`` WITH a ``message_id`` (native
      message-scoped streaming) — append to that message's own buffer in
      :data:`_native_inflight`, ordered/de-duped by ``index``. A delta
      whose message_id is already retired is dropped (returns ``True`` so
      the publisher withholds it from the live stream). When the chunk is
      ``final`` and the message's now-complete text matches a buffered
      committed fingerprint (:data:`_native_committed`), the message
      committed BEFORE its deltas arrived: retire it and return ``True``
      so this duplicate chunk is suppressed live too. Codex-native does
      not emit ``final: true`` chunks, so its already-streamed preview is
      retired when the committed item itself byte-matches the preview.
    * ``response.output_item.done`` for a ``message`` item — it just
      committed to the conversation store, so the matching in-flight
      preview must stop replaying. Find that preview by BYTE-EQUAL text
      (its ``final``-complete deltas equal the committed text) and retire
      it; if no in-flight message matches yet (its deltas raced behind
      this commit), buffer the committed text fingerprint so the delta
      branch retires the message when its ``final`` chunk lands.
    * a terminal turn event (see :data:`_TERMINAL_EVENT_TYPES`) — drop
      both the response-scoped and native entries; the turn's text is now
      either persisted (``completed``) or discarded.
    * a ``session.status`` event whose status is terminal (see
      :data:`_TERMINAL_STATUS_VALUES`) — drop only the RESPONSE-SCOPED
      entry. This catches in-process turn-ends that emit no terminal
      ``response.*`` (web Stop / session-delete, SETUP-phase failure,
      policy-deny notice). The native message buffer is intentionally
      NOT dropped here — claude-native goes ``idle`` mid-turn while
      parked on a permission prompt, so dropping it would lose
      un-committed streamed text on reload.

    Idempotent and order-tolerant: a delta arriving before any
    lifecycle event creates a header-less entry (text still captured,
    replayed without a ``response.created`` envelope), and a duplicate
    terminal event is a no-op.

    :param conversation_id: Conversation/session id the event was
        published on, e.g. ``"conv_abc123"``.
    :param event: The event dict as passed to
        :func:`omnigent.runtime.session_stream.publish`. Reads
        ``event["type"]`` to dispatch, the nested ``event["response"]``
        object for lifecycle events, and ``event["delta"]`` for text
        deltas.
    :returns: ``True`` when the event must be WITHHELD from the live
        fan-out — a claude-native ``output_text.delta`` for an already-
        committed message (a duplicate trailing chunk). The caller
        (:func:`omnigent.runtime.session_stream.publish`) must skip
        broadcasting it. ``False`` for every other event (the normal
        case): record-keeping only, broadcast as usual.
    """
    event_type = event.get("type")

    if event_type == "response.created" or event_type == "response.in_progress":
        response = event.get("response")
        if not isinstance(response, dict):
            return False
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            return False
        with _lock:
            entry = _inflight.get(conversation_id)
            if entry is None or entry.response_id != response_id:
                # New turn (or first lifecycle event after a missed
                # one): start fresh so a prior turn's text can't leak.
                _inflight[conversation_id] = _InFlightTurn(
                    response_id=response_id,
                    response=response,
                )
            else:
                # Same turn — refresh the response object (e.g. the
                # status flip from "queued" to "in_progress") without
                # discarding text already accumulated this turn.
                entry.response = response
        return False

    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if not isinstance(delta, str):
            return False
        message_id = event.get("message_id")
        if isinstance(message_id, str) and message_id:
            # Terminal-observed (claude-native) message-scoped streaming:
            # track per message_id, NOT in the response-scoped blob (it
            # has no response.created and interleaves multiple messages
            # per turn). ``index`` orders chunks and de-dupes a replay.
            #
            # An EMPTY delta is NOT dropped on this path: a finalize marker
            # (``delta="", final=true`` — pi-native's end-of-message signal,
            # see the extension's ``finalizeStreamingMessage``) carries no
            # text but IS the completion signal that flips ``final_seen``,
            # which in turn gates the byte-equal retire on
            # ``output_item.done``. The old ``not delta`` guard dropped that
            # marker before ``final_seen`` could be set, so pi-native
            # messages were NEVER retired — ``snapshot_for`` then replayed
            # the committed message on every reconnect/cold-load and it
            # double-rendered beside the snapshot's persisted copy.
            index = event.get("index")
            final = bool(event.get("final"))
            with _lock:
                # A chunk for an already-committed message (its
                # ``output_item.done`` retired it) arrived late. Drop it:
                # accumulating would resurrect the dropped entry and replay
                # on reconnect, and returning True withholds it from the
                # live fan-out too — see :data:`_native_retired`.
                if message_id in _native_retired.get(conversation_id, {}):
                    return True
                messages = _native_inflight.setdefault(conversation_id, {})
                message = messages.get(message_id)
                if message is None:
                    if not delta and not final:
                        # An empty, non-final delta for an untracked message
                        # carries neither text nor a completion signal —
                        # don't create an empty entry (``snapshot_for`` would
                        # skip its blank text anyway; this just keeps the
                        # index from holding inert keys).
                        if not messages:
                            _native_inflight.pop(conversation_id, None)
                        return False
                    message = _NativeMessage()
                    messages[message_id] = message
                if isinstance(index, int) and not isinstance(index, bool):
                    if index <= message.last_index:
                        return False
                    message.last_index = index
                # Only real text advances the buffer; the finalize marker's
                # empty string is a signal, not content.
                if delta:
                    message.parts.append(delta)
                if final:
                    message.final_seen = True
                    # The message's text is now complete. If its committed
                    # ``output_item.done`` already arrived (deltas raced
                    # behind it — the single-chunk case), a fingerprint is
                    # buffered: this is that committed message. Retire it
                    # and suppress THIS (duplicate) chunk from the live
                    # stream. Otherwise leave it in-flight; the commit will
                    # match it by content when it lands.
                    if _consume_committed_fingerprint(conversation_id, message.parts):
                        _retire_native_message(conversation_id, message_id)
                        return True
            return False
        if not delta:
            # Response-scoped (in-process) path: an empty delta carries no
            # text and, lacking a ``message_id``, no message-scoped
            # completion signal — there is nothing to accumulate.
            return False
        with _lock:
            entry = _inflight.get(conversation_id)
            if entry is None:
                # Delta before any lifecycle event (anomalous for
                # scaffold harnesses, which emit response.created
                # first). Capture the text under a header-less entry
                # (no id yet) rather than dropping it; snapshot_for
                # replays it without a response.created envelope.
                entry = _InFlightTurn()
                _inflight[conversation_id] = entry
            entry.parts.append(delta)
        return False

    if event_type == "response.output_item.done":
        # An assistant message just committed to the conversation store, so
        # its in-flight preview must stop replaying (it would double-render
        # alongside the cold-load snapshot's persisted copy). Match it to
        # the right preview by BYTE-EQUAL text, NOT by position: the done
        # event carries no message_id, and the old FIFO "drop the oldest"
        # guess was wrong for the common single-chunk message whose only
        # (``final``) chunk is POSTed AFTER its commit — at this point that
        # message isn't tracked yet, so FIFO popped the wrong entry (or
        # none). The probe confirmed streamed text == transcript text
        # byte-for-byte, so content is the reliable join key. Non-message
        # items (tool calls) and in-process turns leave the index alone.
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "message":
            text = _committed_message_text(item)
            if text is not None:
                fingerprint = _text_fingerprint(text)
                with _lock:
                    matched_id = _match_committed_native_message(conversation_id, fingerprint)
                    if matched_id is not None:
                        # Deltas (incl. ``final``) already arrived: retire
                        # the message whose complete text equals this commit.
                        _retire_native_message(conversation_id, matched_id)
                    else:
                        # Deltas raced behind this commit (or never arrive):
                        # remember the fingerprint so the message's ``final``
                        # chunk retires + suppresses it when it lands.
                        _buffer_committed_fingerprint(conversation_id, fingerprint)
        return False

    if event_type in _TERMINAL_EVENT_TYPES:
        with _lock:
            _inflight.pop(conversation_id, None)
            _native_inflight.pop(conversation_id, None)
            _native_retired.pop(conversation_id, None)
            _native_committed.pop(conversation_id, None)
        return False

    if event_type == "session.status":
        status = event.get("status")
        if isinstance(status, str) and status in _TERMINAL_STATUS_VALUES:
            with _lock:
                # Only the response-scoped (in-process) blob clears on a
                # terminal status. The native message buffers deliberately
                # do NOT: claude-native goes ``idle`` (its PTY falls
                # quiet) MID-TURN while parked on a permission prompt,
                # with streamed text that has NOT yet committed to the
                # store — clearing it here would lose exactly that text on
                # reload (the bug). Native previews are cleared per-message
                # by their ``output_item.done`` (authoritative commit) and
                # wholesale by :func:`discard` on relay teardown. (In-
                # process agents instead emit ``waiting`` for a parked
                # elicitation, so their ``idle`` always means turn-over.)
                _inflight.pop(conversation_id, None)
        return False

    return False


def snapshot_for(conversation_id: str) -> list[dict[str, Any]]:
    """
    Return replay events for the conversation's in-flight assistant text.

    Read by the ``/stream`` route via ``subscribe``'s
    ``pre_ready_snapshot`` hook so a client that (re)connects mid-turn
    sees the text streamed so far. The events
    are shaped exactly like the live runner emission, so the frontend's
    block-stream reducer reconstructs the bubble with no special-casing
    and the live tail's continuing deltas append cleanly:

    * a ``response.created`` carrying the turn's :class:`ResponseObject`
      (so the reducer sets the response id + agent and opens the bubble);
      omitted when no lifecycle event was captured;
    * a single ``response.output_text.delta`` carrying the joined
      streamed-so-far text.

    For claude-native (message-scoped) streaming it instead replays one
    ``response.output_text.delta`` per in-flight message, each carrying
    its ``message_id`` and highest ``index``, so the client rebuilds the
    same per-message previews and the live tail appends without
    duplication. Committed messages are already dropped from the index
    (on their ``output_item.done``), so this never double-renders content
    the cold-load snapshot already supplies.

    Returns an empty list when there is no in-flight text at all.

    Returns deep copies of the stored response object so a caller
    mutating the replayed event cannot poison the index.

    :param conversation_id: Conversation/session id to query,
        e.g. ``"conv_abc123"``.
    :returns: An ordered list of SSE event dicts to yield ahead of the
        live tail, e.g.
        ``[{"type": "response.created", "response": {...}},
        {"type": "response.output_text.delta", "delta": "Let me plan."}]``.
        Empty when the turn has streamed no text yet.
    """
    with _lock:
        # Claude-native message-scoped replay: one delta per in-flight
        # message, carrying its message_id + highest index so the client
        # rebuilds the same per-message preview and its live tail (deltas
        # published after reconnect, at higher indices) appends cleanly.
        native_messages = _native_inflight.get(conversation_id)
        if native_messages:
            native_events: list[dict[str, Any]] = []
            for message_id, message in native_messages.items():
                text = "".join(message.parts)
                if not text:
                    continue
                native_events.append(
                    {
                        "type": "response.output_text.delta",
                        "delta": text,
                        "message_id": message_id,
                        "index": message.last_index,
                    }
                )
            if native_events:
                return native_events

        entry = _inflight.get(conversation_id)
        if entry is None:
            return []
        text = "".join(entry.parts)
        if not text:
            # Only replay once there is actual text to recover. This
            # scopes the fix to the bug (lost in-flight text) and keeps
            # the index inert for harnesses that emit lifecycle events
            # but no streamed text.
            return []
        events: list[dict[str, Any]] = []
        if entry.response is not None:
            events.append(
                {
                    "type": "response.created",
                    "response": copy.deepcopy(entry.response),
                }
            )
        events.append({"type": "response.output_text.delta", "delta": text})
        return events


def discard(conversation_id: str) -> None:
    """
    Drop a conversation's in-flight entry, if any.

    Called from the Omnigent relay's teardown (``_relay_runner_stream``'s
    ``finally``) so a relay that exits WITHOUT a terminal turn event —
    a runner death / tunnel drop mid-turn, a ``[DONE]`` with no
    preceding terminal, or a PATCH-rebind cancellation — does not strand
    the entry forever. Idempotent: a no-op when nothing is tracked.

    This is the eviction backstop that keeps the index from growing
    unbounded on a long-lived multi-user server; the in-turn clears in
    :func:`record_publish` (terminal ``response.*`` and terminal
    ``session.status``) cover the normal turn-end paths.

    :param conversation_id: Conversation/session id to drop,
        e.g. ``"conv_abc123"``.
    """
    with _lock:
        _inflight.pop(conversation_id, None)
        _native_inflight.pop(conversation_id, None)
        _native_retired.pop(conversation_id, None)
        _native_committed.pop(conversation_id, None)


def reset_text(conversation_id: str) -> None:
    """
    Clear the response-scoped accumulated text but keep the turn header.

    Called when the relay flushes a text segment to a committed message
    at a tool-call boundary: the just-flushed text is now persisted, so a
    mid-turn reconnect must NOT replay it (``snapshot_for`` joins
    ``parts``) and double-render it beside the committed copy. Keeping
    ``entry.response`` means the next segment's replay still carries the
    ``response.created`` header. The native (message-scoped) buffer is
    untouched — it has its own per-message commit eviction.

    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    """
    with _lock:
        entry = _inflight.get(conversation_id)
        if entry is not None:
            entry.parts.clear()


def reset_for_tests() -> None:
    """
    Clear the entire index. For test isolation only.

    The index is process-global; a test that leaks an entry would
    change the replay behavior of a later test. Not for production
    callers — there is no legitimate runtime use case for wiping it.
    """
    with _lock:
        _inflight.clear()
        _native_inflight.clear()
        _native_retired.clear()
        _native_committed.clear()
