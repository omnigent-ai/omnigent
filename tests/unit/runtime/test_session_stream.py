"""
Unit tests for :mod:`omnigent.runtime.session_stream`.

The session stream is a pure pub-sub fan-out:

* :func:`publish` is a no-op when no subscriber is connected for the
  conversation_id.
* Every active :func:`subscribe` call gets its own queue and sees
  every event published after it subscribed.
* :func:`close` broadcasts an end-of-stream sentinel to every
  subscriber.
* Subscriber slots are torn down in the generator's ``finally``
  block so leaks cannot accumulate.

These tests pin those invariants directly. They are sibling to the
workflow-integration drift tests in
:mod:`tests.unit.server.test_stream_events`, which exercise the
end-to-end publish pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runtime import session_stream


# Each test resets the global subscriber registry so cross-test leak
# of a hung subscriber from another test can't mask a real failure.
@pytest.fixture(autouse=True)
def _clean_session_stream_registry() -> None:
    """
    Reset the module-global subscriber map before and after each test.

    The pub-sub registry is process-global; without this fixture, a
    test that leaks a subscriber would silently change the behavior
    of every later test by retaining the leak's slot.
    """
    session_stream._subscribers.clear()
    yield
    session_stream._subscribers.clear()


async def _collect(conv_id: str, expected: int) -> list[dict[str, Any]]:
    """
    Subscribe and collect exactly ``expected`` events, then stop.

    :param conv_id: Conversation id to subscribe to,
        e.g. ``"conv_abc"``.
    :param expected: Exact number of events to collect before the
        async iterator is broken out of. The caller pre-knows the
        count so the test cannot hang.
    :returns: The collected event dicts in arrival order.
    """
    out: list[dict[str, Any]] = []
    # Bind the generator explicitly and ``aclose`` it on the way
    # out so the slot-cleanup ``finally`` in ``subscribe`` runs
    # deterministically — Python 3.13 no longer auto-closes async
    # generators when ``async for`` breaks/returns (cleanup is
    # garbage-collection-timed). Real consumers (FastAPI
    # ``StreamingResponse``) ``aclose`` the wrapping generator on
    # disconnect, so prod doesn't leak; this is purely a test
    # determinism fix.
    gen = session_stream.subscribe(conv_id)
    try:
        async for event in gen:
            out.append(event)
            if len(out) >= expected:
                return out
        return out
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_publish_without_subscriber_is_silent_noop() -> None:
    """
    ``publish`` with no subscriber connected drops events silently.

    Production breakage that causes this test to fail: somebody adds
    a side effect (e.g. a print, a log line, an exception) to the
    no-subscriber path. The pub-sub design contract is that events
    fired before any client connects are LOST and the producer pays
    no cost — turn-emit sites publish unconditionally.
    """
    # Should not raise, should not log, should leave the registry empty.
    session_stream.publish("conv_unknown", {"type": "x", "i": 1})
    # If publish were silently creating a slot, the registry would
    # have grown. The contract: only ``subscribe`` adds slots.
    assert session_stream._subscribers == {}, (
        f"publish must NOT create subscriber slots. State: {session_stream._subscribers!r}"
    )


@pytest.mark.asyncio
async def test_single_subscriber_receives_events_in_order() -> None:
    """
    A single subscriber gets every event published after it subscribed.

    Production breakage that causes this test to fail: ``publish``
    fails to deliver to a registered subscriber, OR delivery
    reorders the events (impossible with ``call_soon_threadsafe`` +
    a single queue, but pinning the invariant guards future refactors).
    """
    task = asyncio.create_task(_collect("conv_a", expected=3))
    # Yield once so the subscriber registers its slot before publish.
    # Without this yield the event would land before the subscriber
    # connected and be dropped — that's the design, but this test
    # is about the post-subscribe path.
    await asyncio.sleep(0)
    session_stream.publish("conv_a", {"type": "e", "i": 1})
    session_stream.publish("conv_a", {"type": "e", "i": 2})
    session_stream.publish("conv_a", {"type": "e", "i": 3})
    received = await asyncio.wait_for(task, timeout=2.0)
    # Exact ordering of i=1,2,3 — anything else means publish
    # reordered or the queue isn't FIFO.
    assert received == [
        {"type": "e", "i": 1},
        {"type": "e", "i": 2},
        {"type": "e", "i": 3},
    ], (
        f"Subscriber saw {received!r}; expected i=1,2,3 in order. "
        f"A mismatch indicates either reordering inside publish or "
        f"a missed event during fan-out."
    )


@pytest.mark.asyncio
async def test_pre_subscribe_events_are_lost() -> None:
    """
    Events published before any subscriber connected are dropped.

    Production breakage that causes this test to fail: someone
    adds a buffer / replay queue to the pub-sub module, which would
    silently change the contract clients reconcile against
    (``GET /v1/sessions/{id}`` for history, live stream from
    connect forward). Buffering would mean the snapshot+live combo
    double-delivers items, breaking client dedup.
    """
    # Publish first, with no subscriber.
    session_stream.publish("conv_lost", {"type": "early", "i": 0})

    # Now subscribe and publish another event. The subscriber must
    # see ONLY the second event — the first one is gone.
    task = asyncio.create_task(_collect("conv_lost", expected=1))
    await asyncio.sleep(0)
    session_stream.publish("conv_lost", {"type": "live", "i": 1})
    received = await asyncio.wait_for(task, timeout=2.0)
    assert received == [{"type": "live", "i": 1}], (
        f"Subscriber received {received!r}; expected only the "
        f"post-subscribe event. A buffered/replayed early event "
        f"would have appeared first."
    )


@pytest.mark.asyncio
async def test_multi_subscriber_fan_out_independently_delivers() -> None:
    """
    Two subscribers to the same conv_id each see every event.

    Production breakage that causes this test to fail: the registry
    regresses to a single-queue-per-conversation design where
    subscribers race on ``queue.get()`` and each event goes to
    exactly one (whichever asyncio scheduled first). Multi-subscriber
    works naturally under fan-out; the test catches a regression to
    the old broken model.
    """
    t1 = asyncio.create_task(_collect("conv_fan", expected=2))
    t2 = asyncio.create_task(_collect("conv_fan", expected=2))
    # Yield until both subscribers are registered. Two cycles
    # because each task needs a turn to enter its async generator.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session_stream.publish("conv_fan", {"type": "x", "i": 1})
    session_stream.publish("conv_fan", {"type": "x", "i": 2})
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)
    # Both subscribers see ALL events — that's the fan-out contract.
    expected = [{"type": "x", "i": 1}, {"type": "x", "i": 2}]
    assert r1 == expected, (
        f"Subscriber 1 received {r1!r}, expected {expected!r}. "
        f"Missing events indicate a single-consumer queue regression "
        f"(events being stolen by subscriber 2)."
    )
    assert r2 == expected, (
        f"Subscriber 2 received {r2!r}, expected {expected!r}. "
        f"Missing events indicate a single-consumer queue regression "
        f"(events being stolen by subscriber 1)."
    )


@pytest.mark.asyncio
async def test_close_broadcasts_done_to_all_subscribers() -> None:
    """
    ``close`` signals end-of-stream to every connected subscriber.

    Production breakage that causes this test to fail: ``close``
    delivers to only one subscriber (off-by-one bug in the iter-and-
    fan-out logic), OR ``close`` fails to terminate a subscriber's
    async generator (regression to a polling loop). The
    session-lifecycle path uses ``close`` to clean-disconnect all
    SSE consumers at session-end; missing terminations leak
    sockets.
    """
    out1: list[dict[str, Any]] = []
    out2: list[dict[str, Any]] = []

    async def drain(buf: list[dict[str, Any]]) -> None:
        async for event in session_stream.subscribe("conv_close"):
            buf.append(event)

    t1 = asyncio.create_task(drain(out1))
    t2 = asyncio.create_task(drain(out2))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session_stream.publish("conv_close", {"type": "a", "i": 1})
    session_stream.close("conv_close")
    # Both subscribers terminate cleanly under the close sentinel.
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)
    assert out1 == [{"type": "a", "i": 1}], (
        f"Subscriber 1 saw {out1!r}; close should have delivered the event before terminating."
    )
    assert out2 == [{"type": "a", "i": 1}], (
        f"Subscriber 2 saw {out2!r}; close should have delivered the event before terminating."
    )


@pytest.mark.asyncio
async def test_subscriber_slot_cleaned_up_on_exit() -> None:
    """
    Exiting the subscribe iterator removes the slot from the registry.

    Production breakage that causes this test to fail: the
    ``finally`` block in ``subscribe`` doesn't tear down the slot,
    leaking a queue per disconnect. Over many client reconnects,
    leaked queues accumulate ``call_soon_threadsafe`` callbacks that
    never get drained, eventually causing memory pressure.
    """
    task = asyncio.create_task(_collect("conv_clean", expected=1))
    await asyncio.sleep(0)
    # Verify the slot exists during the subscribe.
    assert "conv_clean" in session_stream._subscribers, (
        f"Expected a slot for conv_clean during active subscribe. "
        f"State: {session_stream._subscribers!r}"
    )
    session_stream.publish("conv_clean", {"type": "a"})
    await asyncio.wait_for(task, timeout=2.0)
    # After the subscriber exits, the slot must be gone (the last
    # subscriber-out pops the conv_id key entirely).
    assert "conv_clean" not in session_stream._subscribers, (
        f"Slot leak after subscriber exit. State: {session_stream._subscribers!r}"
    )


# ── Side-channel: pending-elicitations index ─────────────────────────


def test_publishing_elicitation_event_updates_pending_index() -> None:
    """
    Publishing a ``response.elicitation_request`` event registers
    the elicitation in the per-conversation pending index.

    The index is the cross-session signal the sidebar reads to badge
    sessions with outstanding approval prompts. Wiring it to
    ``session_stream.publish`` is what makes the count visible
    regardless of which process emitted the event (server-side
    policy, claude-native hook, or runner-relayed). A regression
    that decouples them would silently break the sidebar for every
    session whose chat isn't currently open.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    session_stream.publish(
        "conv_p",
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_publish_test",
        },
    )
    # 1 = the publish side-channel ran. If 0, the import or call
    # in ``session_stream.publish`` was removed; the sidebar's
    # cross-session badge becomes a dead feature.
    assert pending_elicitations.count_for("conv_p") == 1
    pending_elicitations.reset_for_tests()


def test_publishing_non_elicitation_event_leaves_pending_index_untouched() -> None:
    """
    A regular SSE event (text delta, status, completion) does NOT
    register anything in the pending index.

    ``record_publish`` filters on event type, but this test pins it
    end-to-end through the publish call — if a future refactor
    inverts the filter or removes it, this catches the over-counting
    immediately. Without this guard, every text delta would inflate
    the sidebar badge.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    session_stream.publish(
        "conv_q",
        {"type": "response.output_text.delta", "delta": "hello"},
    )
    session_stream.publish("conv_q", {"type": "response.completed"})
    # 0 = type-filter is intact. > 0 here would mean every delta /
    # completion event creates a phantom pending entry.
    assert pending_elicitations.count_for("conv_q") == 0
    pending_elicitations.reset_for_tests()


# ── Idle keepalive: session.heartbeat ────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_fires_on_idle_when_interval_set() -> None:
    """
    Idle subscribers emit ``session.heartbeat`` on the configured cadence.

    Production breakage that causes this test to fail: the idle
    keepalive in :func:`subscribe` regresses (wrong timeout handling,
    swallowed cancellation, off-by-one). The session-stream SSE route
    relies on the heartbeat so that a half-open client socket (e.g.
    after a laptop sleep) surfaces via the route's
    ``request.is_disconnected()`` check and the client's SSE
    read-timeout. Without the keepalive, both can lag for minutes.
    """
    # Short interval keeps the test fast. The production cadence
    # (15s) is set at the route layer, not in this module.
    gen = session_stream.subscribe("conv_hb_idle", heartbeat_interval_s=0.05)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    finally:
        await gen.aclose()
    # Both yields are synthetic heartbeats. The queue was never
    # published to, so anything else means subscribe leaked state
    # from a prior test or mis-typed the keepalive payload.
    assert first == {"type": "session.heartbeat"}, (
        f"First idle yield was {first!r}; expected the synthetic session.heartbeat keepalive."
    )
    assert second == {"type": "session.heartbeat"}, (
        f"Second idle yield was {second!r}; expected another "
        f"session.heartbeat (the keepalive must repeat, not fire once)."
    )


@pytest.mark.asyncio
async def test_ready_event_emits_after_registration_before_snapshot() -> None:
    """
    ``ready_event`` acknowledges subscription before snapshot work runs.

    Production breakage that causes this test to fail: the ready event
    is yielded before registering the subscriber slot, after the
    snapshot hook, or not at all. The SessionsChat one-shot path relies
    on this ordering to know a no-replay SSE stream is subscribed
    before it posts the user message.
    """
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def _snapshot() -> list[dict[str, Any]]:
        """
        Block the snapshot hook until the test releases it.

        :returns: A single snapshot event yielded after the ready event.
        """
        snapshot_started.set()
        await release_snapshot.wait()
        return [{"type": "snapshot"}]

    gen = session_stream.subscribe(
        "conv_ready",
        ready_event={"type": "session.heartbeat"},
        on_subscribed=_snapshot,
    )
    try:
        ready = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert ready == {"type": "session.heartbeat"}
        assert "conv_ready" in session_stream._subscribers, (
            "ready_event must be emitted only after the subscriber slot "
            "is registered; otherwise a fast producer can still publish "
            "before the live-tail queue exists."
        )
        assert snapshot_started.is_set() is False, (
            "ready_event must not wait behind snapshot work. The HTTP "
            "client uses the first event as a low-latency stream-ready ack."
        )

        release_snapshot.set()
        snapshot = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert snapshot == {"type": "snapshot"}
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_heartbeat_interleaves_with_published_events() -> None:
    """
    Real events override heartbeats; heartbeats resume when the queue empties.

    Production breakage that causes this test to fail: heartbeat
    bookkeeping eats a real event (e.g. the timeout handler swallows
    a queued item) OR a real arrival fails to reset the keepalive
    deadline. The first kills user-visible deltas; the second
    floods the wire with heartbeats during active turns.
    """
    gen = session_stream.subscribe("conv_hb_mix", heartbeat_interval_s=0.05)
    try:
        # Idle long enough for the first heartbeat to fire.
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert first == {"type": "session.heartbeat"}

        # Publish a real event; the next yield must be that event,
        # NOT a heartbeat (the queue.get fires before the timeout).
        session_stream.publish("conv_hb_mix", {"type": "real", "i": 1})
        # ``call_soon_threadsafe`` enqueues the put; yield once so
        # the producer side actually runs and the put_nowait lands
        # on the queue before our wait_for starts.
        await asyncio.sleep(0)
        second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert second == {"type": "real", "i": 1}, (
            f"After publishing a real event, the next yield was "
            f"{second!r}; expected the real event. A heartbeat here "
            f"means the timeout path swallowed the queued item."
        )

        # Going idle again restarts the heartbeat cadence.
        third = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert third == {"type": "session.heartbeat"}, (
            f"After draining the real event, the next yield was "
            f"{third!r}; expected a fresh heartbeat. If a real-event "
            f"arrival fails to re-arm the idle timer, the keepalive "
            f"would stall instead of resuming."
        )
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_no_heartbeat_when_interval_unset() -> None:
    """
    Default ``heartbeat_interval_s=None`` preserves the pure
    event-driven shape.

    Production breakage that causes this test to fail: someone
    flips the default to a non-None value, which would add synthetic
    events to every harness-internal consumer that doesn't expect
    keepalives. The opt-in design keeps the new behavior scoped to
    the route layer.
    """
    gen = session_stream.subscribe("conv_hb_off")
    try:
        # No interval means the queue.get is a plain await. Nothing
        # arrives, the wait_for boundary is what raises.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gen.__anext__(), timeout=0.2)
    finally:
        await gen.aclose()


# ── In-flight assistant-text replay ──────────────────────────


def test_publishing_text_delta_records_inflight_text() -> None:
    """
    A text delta published through ``publish`` lands in the index.

    Wires the in-flight-text side-channel to ``session_stream.publish``
    (the single SSE chokepoint) so the ``/stream`` snapshot-on-connect
    hook can replay the streamed-so-far text. A regression that drops
    the ``inflight_text.record_publish`` call in ``publish`` would make
    every reconnect lose the in-flight bubble again.
    """
    from omnigent.runtime import inflight_text

    inflight_text.reset_for_tests()
    session_stream.publish(
        "conv_ift",
        {
            "type": "response.created",
            "response": {"id": "resp_1", "model": "nessie", "status": "queued", "created_at": 1},
        },
    )
    session_stream.publish(
        "conv_ift",
        {"type": "response.output_text.delta", "delta": "streamed text"},
    )

    snap = inflight_text.snapshot_for("conv_ift")
    # Non-empty replay = the publish side-channel ran. Empty here means
    # the wiring was removed and the cross-reconnect recovery is dead.
    assert snap[-1] == {"type": "response.output_text.delta", "delta": "streamed text"}, (
        f"publish did not record in-flight text; got {snap!r}"
    )
    inflight_text.reset_for_tests()


def test_publishing_unrelated_event_leaves_inflight_index_untouched() -> None:
    """
    A non-text, non-lifecycle event does not create an in-flight entry.

    ``record_publish`` filters on event type; this pins it end-to-end
    through ``publish`` so a future refactor that inverts/removes the
    filter (and starts replaying e.g. tool events as assistant text) is
    caught immediately.
    """
    from omnigent.runtime import inflight_text

    inflight_text.reset_for_tests()
    session_stream.publish(
        "conv_ift2",
        {"type": "response.elicitation_request", "elicitation_id": "elicit_1"},
    )
    # No lifecycle/text event → nothing tracked. A non-empty result
    # would mean unrelated events inflate the replay.
    assert inflight_text.snapshot_for("conv_ift2") == []
    inflight_text.reset_for_tests()


@pytest.mark.asyncio
async def test_publish_withholds_committed_native_duplicate_from_live_stream() -> None:
    """
    A trailing chunk for an already-committed native message isn't fanned out.

    This is the LIVE half of the claude-native double-render fix:
    ``record_publish`` returns a suppress verdict for a
    ``response.output_text.delta`` whose message already committed, and
    ``publish`` must WITHHOLD it from connected subscribers. The old order
    (fan out first, record after) could only scrub the reconnect snapshot
    — it could never un-send a delta already on a live subscriber's queue,
    which is exactly the duplicate users saw.

    Reproduces the single-chunk race: the message's ``output_item.done``
    (broadcast as ``response.output_item.done``) arrives before its lone
    ``final`` delta. The subscriber must see the committed item and a later
    sentinel, but NOT the duplicate delta wedged between them.
    """
    from omnigent.runtime import inflight_text

    inflight_text.reset_for_tests()
    cid = "conv_live_gate"
    committed = {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": "ci_1",
            "content": [{"type": "output_text", "text": "Hi there"}],
        },
    }
    # Same text as the commit → matches it → suppressed from the live tail.
    duplicate_delta = {
        "type": "response.output_text.delta",
        "delta": "Hi there",
        "message_id": "m1",
        "index": 0,
        "final": True,
    }
    sentinel = {"type": "marker", "i": 1}

    task = asyncio.create_task(_collect(cid, expected=2))
    # Yield so the subscriber registers its slot before we publish.
    await asyncio.sleep(0)
    session_stream.publish(cid, committed)  # broadcast; buffers the fingerprint
    session_stream.publish(cid, duplicate_delta)  # matches commit → withheld
    session_stream.publish(cid, sentinel)  # broadcast

    received = await asyncio.wait_for(task, timeout=2.0)
    # The committed item and the sentinel arrive; the duplicate delta does
    # NOT. If the live gate regressed, received would be 3 events with the
    # delta wedged in the middle (and `_collect` would have returned the
    # first two: committed + delta).
    assert received == [committed, sentinel], (
        "the duplicate trailing chunk of an already-committed native "
        f"message must be withheld from the live stream; got {received!r}"
    )
    inflight_text.reset_for_tests()


@pytest.mark.asyncio
async def test_inflight_replay_via_pre_ready_snapshot_does_not_duplicate_window_deltas() -> None:
    """
    A delta streamed in the ready_event gap renders once, not twice.

    Reproduces the real ``/stream`` subscribe shape and the double-render
    regression. The route passes BOTH ``ready_event`` (the subscription
    ack heartbeat, yielded first — which SUSPENDS the generator) and
    ``pre_ready_snapshot`` (the in-flight text replay). The relay
    keeps publishing deltas during the post-``ready_event`` gap. The fix
    captures the replay snapshot synchronously at slot registration —
    before the suspension — so a gap delta lands ONLY on the live tail,
    not in both the replay and the tail.

    1. A turn streams ``response.created`` + two deltas with NO
       subscriber connected — lost to the live stream (no replay
       buffer), recoverable only from the in-flight index.
    2. A client subscribes with ``ready_event`` + ``pre_ready_snapshot``,
       exactly as the route does.
    3. After the ack arrives, the relay publishes one more delta ("!")
       in the gap. The slot is registered, so it joins the live tail;
       the snapshot was already frozen at registration without it.
    4. The bubble renders the recovered prefix once + the gap delta once.

    Breakage that turns this red: moving the snapshot read back behind
    ``yield ready_event`` (into the async ``on_subscribed`` hook). Then
    "!" is in BOTH the replayed snapshot text AND the live tail, and the
    bubble renders "Hello world!!" instead of "Hello world!".
    """
    from omnigent.runtime import inflight_text

    inflight_text.reset_for_tests()
    cid = "conv_window"
    created = {
        "type": "response.created",
        "response": {"id": "resp_1", "model": "nessie", "status": "in_progress", "created_at": 1},
    }
    # (1) Prefix streamed before any subscriber connected — only in the
    # in-flight index, never on a queue.
    session_stream.publish(cid, created)
    session_stream.publish(cid, {"type": "response.output_text.delta", "delta": "Hello "})
    session_stream.publish(cid, {"type": "response.output_text.delta", "delta": "world"})

    # (2) Exactly what the /stream route passes: a sync pre_ready_snapshot
    # reading the real index, plus the ready_event ack. The snapshot is
    # captured synchronously inside this first __anext__, before the
    # ready_event below is yielded.
    gen = session_stream.subscribe(
        cid,
        ready_event={"type": "session.heartbeat"},
        pre_ready_snapshot=lambda: inflight_text.snapshot_for(cid),
    )
    deltas: list[str] = []
    created_replays = 0
    try:
        ev0 = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        # The ack is first; the replay snapshot is already frozen.
        assert ev0 == {"type": "session.heartbeat"}, f"expected ready_event ack first, got {ev0!r}"

        # (3) Gap publish: slot is registered, so "!" lands on the live
        # tail; the frozen snapshot does not contain it.
        session_stream.publish(cid, {"type": "response.output_text.delta", "delta": "!"})

        # Drain the replay (created + joined prefix) then the live tail.
        for _ in range(8):
            try:
                ev = await asyncio.wait_for(gen.__anext__(), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if ev.get("type") == "response.created":
                created_replays += 1
            elif ev.get("type") == "response.output_text.delta":
                deltas.append(ev["delta"])
    finally:
        await gen.aclose()
        inflight_text.reset_for_tests()

    # (4) Recovered prefix once + gap delta once. "Hello world!!" (or
    # "Hello worldHello world!") would mean the gap delta was counted in
    # both the replay and the live tail — the double-render.
    assert "".join(deltas) == "Hello world!", (
        f"rendered bubble was {''.join(deltas)!r}; expected 'Hello world!' "
        f"(prefix once + gap delta once). A duplicate means the snapshot "
        f"read drifted back behind the ready_event yield."
    )
    # Exactly one replayed response.created opens the bubble once; a
    # second would re-open it and (in append-only clients) duplicate.
    assert created_replays == 1, f"expected 1 replayed response.created, got {created_replays}"
    # The joined prefix is replayed exactly once across all deltas.
    assert deltas.count("Hello world") == 1, (
        f"joined prefix should be replayed exactly once, got deltas={deltas!r}"
    )
