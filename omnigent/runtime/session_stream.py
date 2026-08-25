"""Pure pub-sub in-process live stream for real-time SSE delivery.

This module is a fan-out broadcaster keyed by ``conversation_id``.
Every active call to :func:`subscribe` owns its own bounded ephemeral
``asyncio.Queue``; :func:`publish` fans the event out to all queues
currently subscribed to that conversation_id. A subscriber that falls
behind past the bound is disconnected so it can recover through the
snapshot + live-tail reconnect contract. Events emitted before any
subscriber is connected are LOST — there is no buffer and no replay.
Clients that need to recover state across a disconnect fetch
``GET /v1/sessions/{id}`` for the persisted history and dedupe by item id.

This module owns no per-conversation lifecycle. There is no
``register`` / ``unregister`` step: the first ``subscribe`` call
lazily creates a subscriber slot, and the last ``subscribe`` to
exit removes the slot in its ``finally`` block.

Producer (workflow thread, sync):
    publish(conversation_id, event)  — thread-safe broadcast
    close(conversation_id)           — broadcasts end-of-stream
                                       to all active subscribers

Consumer (SSE endpoint, async):
    subscribe(conversation_id) -> AsyncIterator  — yields events
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from omnigent.debug_logging import debug_event, debug_sink_enabled, sse_event_logger
from omnigent.runtime import inflight_text, pending_elicitations

_logger = logging.getLogger(__name__)

# A generous burst allowance that still bounds one stalled subscriber's memory.
_SUBSCRIBER_QUEUE_MAX_EVENTS = 1024

# Sentinel objects that signal terminal subscriber states.
_DONE = object()
_OVERFLOW = object()


class SubscriberOverflowError(RuntimeError):
    """Raised when a subscriber falls behind the bounded live-event queue."""


# Subscriber registry: conversation_id -> set of
# (queue, event_loop) pairs. The event_loop reference is needed
# so the sync producer thread can safely deliver items via
# ``call_soon_threadsafe`` into the queue's owning loop.
_subscribers: dict[
    str,
    set[tuple[asyncio.Queue[dict[str, Any] | object], asyncio.AbstractEventLoop]],
] = {}
_lock = threading.Lock()


def _enqueue_or_overflow(
    queue: asyncio.Queue[dict[str, Any] | object],
    item: dict[str, Any] | object,
) -> None:
    """Enqueue *item*, replacing a full backlog with an overflow signal."""
    try:
        queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass

    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    queue.put_nowait(_OVERFLOW)


# ── SSE-event debug logging (table-only; see omnigent.debug_logging) ──────────
# Frequent, low-signal events not worth a debug-log row.
_SSE_SKIP_TYPES = frozenset(
    {"session.terminal.activity", "session.heartbeat", "response.heartbeat"}
)
# Failure events logged at WARNING so the table's level column flags them.
_SSE_WARN_TYPES = frozenset(
    {"response.failed", "response.error", "turn.failed", "response.policy_denied"}
)
# Top-level event fields safe to log — stable identifiers, closed enums, and
# pure numerics only. Deliberately excludes human/LLM-authored free text
# (``reason`` — PolicyDeniedEvent's deny reason is LLM-generated and can quote
# the content it evaluated — and ``blocked_on``, a free-form "human phrase") and
# ``tool_name`` (author-defined for custom/MCP tools; ``call_id`` still
# correlates a call to its result without naming the tool), alongside the
# content fields dropped in _sse_safe_attributes.
_SSE_SAFE_KEYS = (
    "status",
    "call_id",
    "message_id",
    "phase",
    "attempt",
    "max_attempts",
    "sequence_number",
    "index",
    "final",
    "model",
    "context_tokens",
    "context_window",
    "total_cost_usd",
)


def _sse_safe_attributes(event: dict[str, Any]) -> dict[str, object]:
    """Extract only safe identifiers/dimensions from an SSE event.

    A strict whitelist: content and free-text fields (``delta`` text, tool
    ``arguments`` / outputs, message/reasoning ``data``, ``error.message``,
    human/LLM-authored ``reason`` / ``blocked_on``, and ``tool_name``) are never
    read, so no model response or user content reaches the debug table.
    """
    attrs: dict[str, object] = {}
    for key in _SSE_SAFE_KEYS:
        value = event.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            attrs[key] = value
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        attrs["response_id"] = response["id"]
    elif isinstance(event.get("response_id"), str):
        attrs["response_id"] = event["response_id"]
    item = event.get("item")
    if isinstance(item, dict):
        if isinstance(item.get("id"), str):
            attrs["item_id"] = item["id"]
        if isinstance(item.get("type"), str):
            attrs["item_type"] = item["type"]
    error = event.get("error")
    if isinstance(error, dict):
        if error.get("code") is not None:
            attrs["error_code"] = error["code"]
        if error.get("source") is not None:
            attrs["error_source"] = error["source"]
    return attrs


def _log_sse_event(conversation_id: str, event: dict[str, Any]) -> None:
    """Mirror one emitted SSE event to the debug-log table (best-effort).

    No-op unless the debug-log sink is enabled. Logs the event name and safe
    ids only (never content); heartbeats / terminal-activity are skipped, and
    failure events go at WARNING. Never raises into :func:`publish`.
    """
    with contextlib.suppress(Exception):
        if not debug_sink_enabled():
            return
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type in _SSE_SKIP_TYPES:
            return
        level = logging.WARNING if event_type in _SSE_WARN_TYPES else logging.INFO
        extra = debug_event(event_type, session_id=conversation_id)
        extra["attributes"] = _sse_safe_attributes(event)
        sse_event_logger().log(level, "sse %s", event_type, extra=extra)


def publish(conversation_id: str, event: dict[str, Any]) -> None:
    """
    Broadcast an event to every active subscriber of the given
    conversation (called from sync workflow thread). The event
    payload is delivered verbatim — no sequence number or other
    ordering field is added on the wire. Events emitted while no
    subscriber is connected are dropped silently; reconnecting
    clients use the snapshot endpoint, not replay.

    No-op when ``_subscribers`` has no entry for this
    ``conversation_id`` (typical between turns when nothing is
    listening).

    :param conversation_id: The conversation to publish to,
        e.g. ``"conv_abc123"``.
    :param event: The event dict to publish, e.g.
        ``{"type": "response.output_text.delta",
        "delta": "Hello"}``. The ``"type"`` key SHOULD match the
        ``type`` ``Literal`` of one of the variants in
        :data:`omnigent.server.schemas.ServerStreamEvent`;
        the Omnigent route layer validates each emitted dict against
        the union before serializing, so an unmodelled event
        fails loud at the SSE boundary.
    """
    # Mirror the emitted event to the debug-log table (best-effort, table-only,
    # no-op unless the sink is enabled). Done first so it captures every event
    # the server produces — including ones with no live subscriber.
    _log_sse_event(conversation_id, event)
    # Track reconnect state and centrally suppress or rewrite native deltas
    # before they reach subscribers.
    live_event = inflight_text.record_publish(conversation_id, event)
    # Side-channel: keep the cross-session pending-elicitations
    # index in step with the SSE stream. Only acts on
    # ``response.elicitation_request`` events; every other event
    # type is a single dict lookup and a return. A suppressed event is
    # always a text delta, never an elicitation, so this still runs.
    pending_elicitations.record_publish(conversation_id, event)
    if live_event is None:
        return
    with _lock:
        subs = list(_subscribers.get(conversation_id, ()))
    for queue, loop in subs:
        loop.call_soon_threadsafe(_enqueue_or_overflow, queue, live_event)


def close(conversation_id: str) -> None:
    """
    Broadcast an end-of-stream sentinel to every active subscriber
    of the given conversation. Subscribers awaiting their queue
    will see the sentinel, exit their async-iteration loop, and
    cleanly tear down their entry. Idempotent and a no-op when no
    subscribers are connected.

    :param conversation_id: The conversation whose subscribers
        should be signalled, e.g. ``"conv_abc123"``.
    """
    with _lock:
        subs = list(_subscribers.get(conversation_id, ()))
    for queue, loop in subs:
        loop.call_soon_threadsafe(_enqueue_or_overflow, queue, _DONE)


def shutdown_all() -> None:
    """Signal all active subscribers across every conversation to exit.

    Broadcasts the end-of-stream sentinel to every queued subscriber so
    SSE generators return at their next iteration without waiting for a
    heartbeat timeout or forced task cancellation. Called from the asyncio
    event loop (``_ShutdownSignalingServer.shutdown`` in ``cli.py``) before
    uvicorn's graceful-shutdown wait starts, so streams drain within the
    window rather than being force-cancelled. Sync callers should use
    :func:`close` per-conversation instead.
    """
    with _lock:
        all_subs = [entry for subs in _subscribers.values() for entry in subs]
    for queue, _ in all_subs:
        _enqueue_or_overflow(queue, _DONE)


async def subscribe(
    conversation_id: str,
    *,
    heartbeat_interval_s: float | None = None,
    ready_event: dict[str, Any] | None = None,
    pre_ready_snapshot: Callable[[], Iterable[dict[str, Any]]] | None = None,
    on_subscribed: Callable[[], Awaitable[Iterable[dict[str, Any]]]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Subscribe to live events for a conversation.

    Creates a fresh bounded ephemeral queue for this subscriber, registers
    it under ``conversation_id``, and yields events as they arrive
    from :func:`publish`. Ends when :func:`close` broadcasts the
    end-of-stream sentinel or when the caller stops iterating
    (e.g. client disconnect cancels the generator). The
    ``finally`` block always unregisters this subscriber slot so
    a stale queue cannot keep accumulating events.

    If the subscriber falls more than
    :data:`_SUBSCRIBER_QUEUE_MAX_EVENTS` events behind, its queued backlog
    is replaced with an overflow signal and this iterator raises
    :class:`SubscriberOverflowError`. HTTP/SSE callers treat that as a
    dropped transport and reconnect through the persisted snapshot rather
    than retaining an unbounded in-memory backlog.

    Live-tail only: events emitted before this call are NOT
    replayed. Multiple concurrent subscribers to the same
    conversation each see every event independently — there is
    no contention between them.

    Must be called from the asyncio event loop that the caller
    intends to iterate on; the sync producer side uses
    ``loop.call_soon_threadsafe`` to enqueue across threads.

    :param conversation_id: The conversation to subscribe to,
        e.g. ``"conv_abc123"``.
    :param heartbeat_interval_s: When set, yield a synthetic
        ``{"type": "session.heartbeat"}`` dict whenever the queue
        has been idle for this many seconds. Heartbeats are
        generated locally inside this subscriber and never enter
        the publish path, so multiple subscribers each get their
        own independent cadence. ``None`` (default) preserves the
        pure event-driven shape used by harness-internal
        consumers that don't need keepalive.
    :param ready_event: Optional event yielded immediately after
        this subscriber's slot is registered, e.g.
        ``{"type": "session.heartbeat"}``. This gives HTTP/SSE
        clients a subscription acknowledgment before an expensive
        snapshot hook runs, while still registering the live-tail
        queue before any producer can publish a turn event.
    :param pre_ready_snapshot: Optional SYNC hook run once, immediately
        after slot registration and before any ``yield``/``await``. Its
        events are yielded ahead of the live tail. Unlike ``on_subscribed``
        this must be synchronous: it is the only place a dedup-sensitive
        snapshot can be read while still partitioning exactly against the
        live tail, because no ``publish`` can interleave before the first
        suspension. Use it for the in-flight assistant-text replay;
        reading that from ``on_subscribed`` (after ``yield ready_event``)
        double-renders deltas streamed in the gap. Best-effort:
        exceptions are swallowed so a failing snapshot never blocks the
        live tail. ``None`` skips it.
    :param on_subscribed: Optional async hook run once, right after this
        subscriber's slot is registered and before the first live event
        is awaited. Its returned event dicts are yielded as a
        snapshot-on-connect ahead of the live tail. Registering the slot
        first guarantees no delta is dropped between snapshot and tail.
        Best-effort: exceptions are swallowed so a slow/failing snapshot
        never blocks live delivery. ``None`` skips the snapshot.
    :returns: An async iterator of event dicts. Each event is
        yielded verbatim as it was passed to :func:`publish`,
        plus synthetic heartbeat dicts when *heartbeat_interval_s*
        is set.
    :raises SubscriberOverflowError: If this subscriber falls behind the
        bounded event queue.
    """
    queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(
        maxsize=_SUBSCRIBER_QUEUE_MAX_EVENTS
    )
    loop = asyncio.get_running_loop()
    entry = (queue, loop)
    with _lock:
        _subscribers.setdefault(conversation_id, set()).add(entry)
    # Read the pre-ready snapshot synchronously here — after slot
    # registration, before the ``yield`` below suspends. On the Omnigent event
    # loop (where the relay calls ``publish``) nothing runs in between, so
    # the snapshot and the live tail partition exactly: deltas before this
    # point are in the snapshot, deltas after are on ``queue``. Reading it
    # after ``yield ready_event`` instead lets the relay publish deltas
    # into BOTH, which render twice.
    try:
        pre_ready_events: list[dict[str, Any]] = (
            list(pre_ready_snapshot()) if pre_ready_snapshot is not None else []
        )
    except Exception:
        _logger.debug(
            "session_stream pre_ready_snapshot failed for %s",
            conversation_id,
            exc_info=True,
        )
        pre_ready_events = []
    try:
        if ready_event is not None:
            yield ready_event
        for pre_ready_event in pre_ready_events:
            yield pre_ready_event
        if on_subscribed is not None:
            # Gather the snapshot AFTER the slot is registered (above) so a
            # delta published during the gather lands on ``queue`` and is
            # yielded by the loop below — no missed events between snapshot
            # and live tail (the broker has no buffer). Best-effort: a
            # failing/slow hook must not block the live tail.
            try:
                snapshot_events = await on_subscribed()
            except Exception:
                _logger.debug(
                    "session_stream on_subscribed snapshot failed for %s",
                    conversation_id,
                    exc_info=True,
                )
                snapshot_events = ()
            for snapshot_event in snapshot_events:
                yield snapshot_event
        while True:
            if heartbeat_interval_s is None:
                item = await queue.get()
            else:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval_s)
                except asyncio.TimeoutError:
                    # Queue was idle past the heartbeat deadline. Emit
                    # a synthetic keepalive. Its wire bytes give the
                    # route's ``request.is_disconnected()`` check and
                    # the client's SSE read-timeout something to fire
                    # against if the socket has gone half-open (e.g.
                    # after a laptop sleep).
                    yield {"type": "session.heartbeat"}
                    continue
            if item is _DONE:
                return
            if item is _OVERFLOW:
                raise SubscriberOverflowError(
                    f"session stream subscriber for {conversation_id!r} "
                    f"exceeded {_SUBSCRIBER_QUEUE_MAX_EVENTS} queued events"
                )
            assert isinstance(item, dict)
            yield item
    finally:
        with _lock:
            subs = _subscribers.get(conversation_id)
            if subs is not None:
                subs.discard(entry)
                if not subs:
                    _subscribers.pop(conversation_id, None)
