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
``message_id`` and reconciled against recent
``response.output_item.done`` messages, then replayed by
:func:`snapshot_for` as message-scoped deltas so the reconnecting client
rebuilds the same in-flight preview.

What it does
------------
The index is populated automatically by
:func:`omnigent.runtime.session_stream.publish` (the single SSE
chokepoint, same as :mod:`omnigent.runtime.pending_elicitations`):
it captures the turn's :class:`ResponseObject` from
``response.created`` / ``response.in_progress`` and accumulates
``response.output_text.delta`` text. Response-scoped text clears when
the turn ends; native text reconciles per message and clears on teardown.
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
import threading
from collections import deque
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
    ``message_id`` and emit no ``response.created``. Each message's
    chunks are tracked separately so :func:`snapshot_for` can replay
    them as message-scoped ``response.output_text.delta`` events (still
    carrying ``message_id``), letting a reconnecting client rebuild the
    same per-message in-flight previews it would have streamed live.

    :param text: Aggregate delta text, e.g. ``"Let me check that."``.
    :param last_index: Highest chunk ``index`` accumulated so far, e.g.
        ``4``. Used to reject repeated chunks and included in reconnect
        replay events for wire-shape fidelity.
    :param forwarded: Whether any text for this message has reached live
        subscribers. A prefix suppressed behind a committed message is
        forwarded as one aggregate if it later diverges.
    :param final_seen: Whether the provider marked the stream complete.
    :param claimed: Whether a committed item currently covers this
        aggregate. Claimed messages are excluded from reconnect snapshots.
    """

    text: str = ""
    last_index: int = -1
    forwarded: bool = False
    final_seen: bool = False
    claimed: bool = False


# Per-conversation, insertion-ordered mapping of message_id to streamed text.
# Native done items carry no message_id, so they reconcile by text content.
_native_inflight: dict[str, dict[str, _NativeMessage]] = {}

# Only the latest few committed messages can race their deltas. A count
# window avoids clocks, expiry, hashing, and unbounded stale state.
_RECENT_NATIVE_MESSAGES = 3
_native_recent_committed: dict[str, deque[str]] = {}


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


def _drop_native_message(conversation_id: str, message_id: str) -> None:
    """Drop one native aggregate. Caller must hold :data:`_lock`."""
    messages = _native_inflight.get(conversation_id)
    if messages is not None:
        messages.pop(message_id, None)
        if not messages:
            _native_inflight.pop(conversation_id, None)


def _consume_recent_native_text(conversation_id: str, text: str) -> None:
    """Consume one recent committed-text occurrence. Caller holds the lock."""
    recent = _native_recent_committed.get(conversation_id)
    if recent is None:
        return
    recent.remove(text)
    if not recent:
        _native_recent_committed.pop(conversation_id, None)


def _supersede_older_native(conversation_id: str, committed_id: str | None) -> None:
    """
    Evict native aggregates superseded by a just-committed message.

    Native providers stream and commit assistant text one message at a
    time, in order, so once a message commits every earlier un-``claimed``
    aggregate is stale: its own commit either already dropped it, or failed
    to reconcile (a lost/reordered/mismatched delta left ``text`` neither
    equal to nor a prefix of the committed item) and it would otherwise
    linger in :data:`_native_inflight` and replay on every reconnect via
    :func:`snapshot_for`. Only ``_native_inflight`` is pruned; the committed
    transcript is unaffected, so over-eviction can at worst drop a live
    preview the committed item then supplies. Caller holds :data:`_lock`.

    Only entries BEFORE the committed one are evicted, so the entry that
    may still be streaming (always the most recent) is never touched.

    :param conversation_id: Conversation whose native buffer to prune.
    :param committed_id: The ``message_id`` the commit reconciled against,
        or ``None`` when it matched none. With ``None`` the commit's own
        aggregate is untracked, so every entry except the tail (which may
        still be streaming) is treated as superseded.
    """
    messages = _native_inflight.get(conversation_id)
    if not messages:
        return
    ids = list(messages)
    cut = ids.index(committed_id) if committed_id in messages else len(ids) - 1
    for message_id in ids[:cut]:
        if not messages[message_id].claimed:
            _drop_native_message(conversation_id, message_id)


def record_publish(conversation_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
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
      :data:`_native_inflight`, ordered/de-duped by ``index``. Suppress the
      running aggregate while it is a prefix of a recent committed message.
      On divergence, forward the whole aggregate once, then resume sending
      incremental deltas.
    * ``response.output_item.done`` for a ``message`` item — it just
      committed to the conversation store. Drop an equal aggregate or claim
      a matching prefix so it no longer appears in reconnect snapshots, then
      remember the committed text for late deltas.
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
    :returns: The event to broadcast, which may carry a rewritten aggregate
        delta, or ``None`` when the event should be suppressed.
    """
    event_type = event.get("type")

    if event_type == "response.created" or event_type == "response.in_progress":
        response = event.get("response")
        if not isinstance(response, dict):
            return event
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            return event
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
        return event

    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if not isinstance(delta, str):
            return event
        message_id = event.get("message_id")
        if isinstance(message_id, str) and message_id:
            index = event.get("index")
            final = bool(event.get("final"))
            with _lock:
                messages = _native_inflight.setdefault(conversation_id, {})
                message = messages.get(message_id)
                if message is None:
                    if not delta:
                        if not messages:
                            _native_inflight.pop(conversation_id, None)
                        return None
                    message = _NativeMessage()
                    messages[message_id] = message
                if isinstance(index, int) and not isinstance(index, bool):
                    if index <= message.last_index:
                        return None
                    message.last_index = index
                if delta:
                    message.text += delta
                if final:
                    message.final_seen = True
                if not delta:
                    if message.claimed and final:
                        aggregate = message.text
                        recent = _native_recent_committed.get(conversation_id)
                        if aggregate in (recent or ()):
                            _drop_native_message(conversation_id, message_id)
                            _consume_recent_native_text(conversation_id, aggregate)
                    return None

                aggregate = message.text
                recent = _native_recent_committed.get(conversation_id)
                matched = next(
                    (text for text in reversed(recent or ()) if text.startswith(aggregate)),
                    None,
                )
                if matched is not None:
                    message.claimed = True
                    if matched == aggregate and message.final_seen:
                        _drop_native_message(conversation_id, message_id)
                        _consume_recent_native_text(conversation_id, matched)
                    return None

                recovered = message.claimed or not message.forwarded
                message.claimed = False
                message.forwarded = True
                if recovered:
                    return {**event, "delta": aggregate}
            return event
        if not delta:
            # Response-scoped (in-process) path: an empty delta carries no
            # text and, lacking a ``message_id``, no message-scoped
            # completion signal — there is nothing to accumulate.
            return event
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
        return event

    if event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "message":
            text = _committed_message_text(item)
            if text is not None:
                with _lock:
                    messages = _native_inflight.get(conversation_id, {})
                    exact_id = next(
                        (
                            message_id
                            for message_id, message in messages.items()
                            if message.text == text
                        ),
                        None,
                    )
                    if exact_id is not None:
                        # Native text commits in stream order, so every
                        # earlier un-committed aggregate is superseded by
                        # this one — evict them before dropping the match so
                        # a mis-reconciled entry can't linger and replay.
                        _supersede_older_native(conversation_id, exact_id)
                        _drop_native_message(conversation_id, exact_id)
                    else:
                        claimed_id = next(
                            (
                                message_id
                                for message_id, message in messages.items()
                                if message.text and text.startswith(message.text)
                            ),
                            None,
                        )
                        if claimed_id is not None:
                            _supersede_older_native(conversation_id, claimed_id)
                            claimed = messages[claimed_id]
                            claimed.claimed = True
                            claimed.forwarded = False
                        else:
                            # The commit matched no tracked aggregate (its
                            # own deltas were lost or mismatched). Everything
                            # but the tail — which may still be streaming —
                            # is superseded and safe to evict.
                            _supersede_older_native(conversation_id, None)
                    recent = _native_recent_committed.setdefault(
                        conversation_id, deque(maxlen=_RECENT_NATIVE_MESSAGES)
                    )
                    recent.append(text)
        return event

    if event_type in _TERMINAL_EVENT_TYPES:
        with _lock:
            _inflight.pop(conversation_id, None)
            _native_inflight.pop(conversation_id, None)
            _native_recent_committed.pop(conversation_id, None)
        return event

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
        return event

    return event


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

    For native message-scoped streaming it instead replays one
    ``response.output_text.delta`` per in-flight message, each carrying
    its ``message_id`` and highest ``index``, matching the live event
    shape. Aggregates covered by a committed item are omitted, so this
    never double-renders content the cold-load snapshot supplies.

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
        # Native message-scoped replay: one delta per in-flight
        # message, carrying its message_id + highest observed index to
        # preserve the live event's wire shape.
        native_messages = _native_inflight.get(conversation_id)
        if native_messages:
            native_events: list[dict[str, Any]] = []
            for message_id, message in native_messages.items():
                if message.claimed:
                    continue
                text = message.text
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
        _native_recent_committed.pop(conversation_id, None)


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
        _native_recent_committed.clear()
