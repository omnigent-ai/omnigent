"""
Unit tests for :mod:`omnigent.runtime.inflight_text`.

The in-flight-text index recovers the assistant text streamed in the
current turn so a client (re)connecting mid-turn can replay it.
These tests pin its state-machine invariants directly — the layer where
they actually live — rather than through a full workflow:

* :func:`record_publish` accumulates ``response.output_text.delta`` text
  and captures the turn's ``ResponseObject``; :func:`snapshot_for`
  replays a ``response.created`` + ``response.output_text.delta`` pair.
* A new ``response`` id resets accumulation so a later turn never
  prepends a prior turn's text.
* Any terminal turn event clears the entry.
* Replay only fires once real text has streamed (so a lifecycle-only
  turn — e.g. claude-native, which streams no ``output_text`` — stays
  inert and never double-renders the cold-load snapshot).
* Reasoning deltas are intentionally NOT tracked.

The wire-up between :func:`omnigent.runtime.session_stream.publish`
and this index, plus the snapshot/live-tail partition guarantee, is
tested in :file:`tests/runtime/test_session_stream.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.runtime import inflight_text


@pytest.fixture(autouse=True)
def _clean_inflight_text_index() -> None:
    """
    Reset the module-global in-flight-text dict between tests.

    The index is process-global; without this fixture a test that
    leaks an in-flight turn would change the replay behavior of a
    later test (e.g. a stale prefix prepended to its snapshot).
    """
    inflight_text.reset_for_tests()
    yield
    inflight_text.reset_for_tests()


def _created(response_id: str, model: str = "nessie") -> dict[str, Any]:
    """
    Build a ``response.created`` event dict for the given turn.

    :param response_id: The turn's response id, e.g. ``"resp_1"``.
    :param model: The agent name carried on the response object,
        e.g. ``"nessie"``.
    :returns: An event dict shaped like the scaffold's
        ``response.created`` emission.
    """
    return {
        "type": "response.created",
        "response": {
            "id": response_id,
            "model": model,
            "status": "queued",
            "created_at": 1,
        },
    }


def _delta(text: str) -> dict[str, Any]:
    """
    Build a ``response.output_text.delta`` event dict.

    :param text: The text fragment for this chunk, e.g. ``"Hello "``.
    :returns: An event dict shaped like the runtime's text-delta
        emission.
    """
    return {"type": "response.output_text.delta", "delta": text}


def test_records_text_and_replays_created_plus_delta() -> None:
    """
    Accumulated deltas replay as a ``response.created`` + joined delta.

    This is the core recovery the fix provides: the streamed-so-far
    text becomes a replayable pair so a reconnecting client repaints
    the bubble with the right agent and full text.
    """
    cid = "conv_a"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("Hello "))
    inflight_text.record_publish(cid, _delta("world"))

    snap = inflight_text.snapshot_for(cid)

    # Two events, created first so the reducer opens the bubble before
    # the text lands. Zero events here would mean the deltas were
    # dropped (the bug); a single event would mean the created envelope
    # was lost (wrong agent attribution + response-id grouping).
    assert len(snap) == 2, f"expected [created, delta], got {snap!r}"
    assert snap[0]["type"] == "response.created"
    # The captured response object carries the agent name + id used for
    # bubble attribution and grouping with the eventual persisted message.
    assert snap[0]["response"]["id"] == "resp_1"
    assert snap[0]["response"]["model"] == "nessie"
    # The deltas are joined in arrival order. A mismatch means the
    # accumulator dropped or reordered tokens.
    assert snap[1] == _delta("Hello world"), f"expected the joined streamed text, got {snap[1]!r}"


def test_reset_text_drops_committed_text_but_keeps_header() -> None:
    """
    ``reset_text`` clears accumulated text but keeps the turn header.

    Called when the relay flushes a text segment to a committed message at
    a tool-call boundary: the flushed text must NOT replay (it would
    double-render beside the committed copy), but the next segment's replay
    must still carry the ``response.created`` header.
    """
    cid = "conv_reset"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("flushed segment"))

    inflight_text.reset_text(cid)

    # The flushed text is gone — no replay (snapshot_for returns [] when a
    # header exists but no text remains).
    assert inflight_text.snapshot_for(cid) == [], (
        "reset_text must drop the already-committed text from the replay"
    )

    # The header survived: a later segment still replays WITH response.created.
    inflight_text.record_publish(cid, _delta("next segment"))
    snap = inflight_text.snapshot_for(cid)
    assert len(snap) == 2 and snap[0]["type"] == "response.created", snap
    assert snap[0]["response"]["id"] == "resp_1"
    assert snap[1] == _delta("next segment")


def test_in_progress_after_created_refreshes_without_dropping_text() -> None:
    """
    A same-id ``response.in_progress`` refreshes the object, keeps text.

    The scaffold emits ``response.created`` then ``response.in_progress``
    for the same turn. The second event must not reset the turn (which
    would discard text streamed between the two), only refresh the
    stored response object (e.g. the status flip to ``in_progress``).
    """
    cid = "conv_b"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("partial"))
    inflight_text.record_publish(
        cid,
        {
            "type": "response.in_progress",
            "response": {
                "id": "resp_1",
                "model": "nessie",
                "status": "in_progress",
                "created_at": 1,
            },
        },
    )

    snap = inflight_text.snapshot_for(cid)

    # Text survived the in_progress event; if the same-id branch reset
    # instead of refreshed, "partial" would be gone and snap empty.
    assert snap[1] == _delta("partial"), f"text dropped by in_progress: {snap!r}"
    # The refreshed status is reflected (proves refresh, not stale keep).
    assert snap[0]["response"]["status"] == "in_progress"


def test_new_response_id_resets_accumulation() -> None:
    """
    A different response id starts a fresh turn — no cross-turn leak.

    Without the id-keyed reset, turn 2's replay would prepend all of
    turn 1's text.
    """
    cid = "conv_c"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("turn one"))
    # New turn (response.completed would normally clear first, but a
    # missed terminal must not strand turn 1's text either — the id
    # change alone resets).
    inflight_text.record_publish(cid, _created("resp_2"))
    inflight_text.record_publish(cid, _delta("turn two"))

    snap = inflight_text.snapshot_for(cid)

    # Only turn 2's text and id. "turn oneturn two" here would mean the
    # accumulator never reset on the new response id.
    assert snap[0]["response"]["id"] == "resp_2"
    assert snap[1] == _delta("turn two"), f"turn-1 text leaked: {snap!r}"


@pytest.mark.parametrize(
    "terminal_type",
    [
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
    ],
)
def test_terminal_event_clears_entry(terminal_type: str) -> None:
    """
    Any terminal turn event drops the in-flight entry.

    After the turn ends its text is either persisted (``completed``) or
    discarded (``failed`` / ``cancelled`` / ``incomplete``); replaying
    it would double-render against the persisted message or resurrect a
    dead turn. All four terminal types must clear.
    """
    cid = "conv_d"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("some text"))
    inflight_text.record_publish(cid, {"type": terminal_type, "response": {"model": "nessie"}})

    # Empty list (not just falsy) — the entry was popped. A non-empty
    # result means the terminal branch failed to clear and the next
    # reconnect would replay stale text.
    assert inflight_text.snapshot_for(cid) == []


def test_no_replay_until_text_streamed() -> None:
    """
    A turn with lifecycle events but no text yields no replay.

    Scopes the fix to the actual bug (lost in-flight TEXT) and keeps
    the index inert for harnesses that emit ``response.created`` but no
    ``output_text`` deltas (claude-native), so reconnect never injects
    an empty bubble alongside that harness's durable cold-load snapshot.
    """
    cid = "conv_e"
    inflight_text.record_publish(cid, _created("resp_1"))

    # No text streamed → nothing to recover → no replay. A non-empty
    # result would inject a header-only bubble for every in-flight turn.
    assert inflight_text.snapshot_for(cid) == []


def test_reasoning_deltas_are_not_tracked() -> None:
    """
    Reasoning deltas never populate the index.

    Reasoning is throwaway and may legitimately differ on reload — only
    assistant ``output_text`` is recovered. If a reasoning delta were
    accumulated, a reasoning-only step would replay as assistant text.
    """
    cid = "conv_f"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, {"type": "response.reasoning_text.delta", "delta": "think"})
    inflight_text.record_publish(
        cid, {"type": "response.reasoning_summary_text.delta", "delta": "summary"}
    )

    # Lifecycle seen but no output_text → no replay. Non-empty here
    # would mean reasoning leaked into the assistant-text accumulator.
    assert inflight_text.snapshot_for(cid) == []


def test_delta_before_lifecycle_replays_headless() -> None:
    """
    Text arriving before any lifecycle event is still recovered.

    Anomalous for the scaffold (which emits ``response.created`` first),
    but the text must not be silently dropped — better a header-less
    delta replay than lost content.
    """
    cid = "conv_g"
    inflight_text.record_publish(cid, _delta("orphan text"))

    snap = inflight_text.snapshot_for(cid)

    # Just the delta, no response.created envelope (none was captured).
    # An empty result would mean orphan text is dropped.
    assert snap == [_delta("orphan text")], f"orphan text not recovered: {snap!r}"


def test_unknown_conversation_returns_empty() -> None:
    """A conversation with no tracked turn returns an empty replay."""
    # == [] (not falsy/type check) so an accidental sentinel return value
    # would be caught.
    assert inflight_text.snapshot_for("conv_never_seen") == []


def _status(value: str) -> dict[str, Any]:
    """
    Build a ``session.status`` event dict.

    :param value: The status value, e.g. ``"idle"`` or ``"running"``.
    :returns: An event dict shaped like the SSE ``session.status`` payload.
    """
    return {"type": "session.status", "status": value}


@pytest.mark.parametrize("terminal_status", ["idle", "failed"])
def test_terminal_session_status_clears_entry(terminal_status: str) -> None:
    """
    A terminal ``session.status`` clears text from a turn with no terminal event.

    A web Stop / session-delete cancels the turn and emits only
    ``session.status: idle`` (no ``response.cancelled``); a SETUP-phase
    failure emits ``failed`` with no ``response.failed``. The entry must
    still be dropped, or it lingers and replays stale text on the next
    reload (and the index grows unbounded).
    """
    cid = "conv_status"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("partial answer"))
    # Sanity: the turn's text is tracked mid-stream.
    assert inflight_text.snapshot_for(cid), "expected in-flight text before the status event"

    inflight_text.record_publish(cid, _status(terminal_status))

    # Cleared — the turn ended without a terminal response.* event.
    # Non-empty here means the Stop/delete/setup-failure leak is back.
    assert inflight_text.snapshot_for(cid) == []


@pytest.mark.parametrize("nonterminal_status", ["running", "waiting"])
def test_nonterminal_session_status_keeps_inflight_text(nonterminal_status: str) -> None:
    """
    A non-terminal ``session.status`` must NOT clear in-flight text.

    ``running`` is the active-turn status and ``waiting`` is a mid-turn
    pause (e.g. a parked elicitation); a turn is still streaming, so its
    accumulated text must survive — clearing here would re-break the
    reload recovery the fix provides.
    """
    cid = "conv_status_keep"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("still going"))
    inflight_text.record_publish(cid, _status(nonterminal_status))

    snap = inflight_text.snapshot_for(cid)
    # Text preserved across a non-terminal status. Empty here would mean
    # an active turn's streamed text was wrongly dropped.
    assert snap[-1] == _delta("still going"), (
        f"in-flight text dropped by {nonterminal_status!r}: {snap!r}"
    )


def test_policy_deny_sentinel_does_not_linger() -> None:
    """
    The policy-deny notice is cleared by its trailing ``idle`` (no stray replay).

    The deny path publishes a synthetic ``output_text.delta``
    (``"[Denied by policy: …]"``) with NO ``response.created`` and NO
    terminal ``response.*`` — only ``session.status running`` … ``idle``
    around it. The terminal ``idle`` must clear the header-less entry, or
    the deny notice replays as a stray agent-less bubble on every reload.
    """
    cid = "conv_deny"
    inflight_text.record_publish(cid, _status("running"))
    inflight_text.record_publish(cid, _delta("[Denied by policy: nope]"))
    # The sentinel was captured (header-less) while the turn looked active.
    assert inflight_text.snapshot_for(cid), "deny sentinel should be tracked before idle"

    inflight_text.record_publish(cid, _status("idle"))

    # Cleared by idle → no stray replay on a later reload.
    assert inflight_text.snapshot_for(cid) == []


def test_discard_drops_entry_and_is_idempotent() -> None:
    """
    :func:`discard` drops a conversation's entry; unknown ids are a no-op.

    The relay's teardown calls this when it exits without a terminal
    event (runner death / rebind), the eviction backstop against an
    unbounded index.
    """
    cid = "conv_discard"
    inflight_text.record_publish(cid, _created("resp_1"))
    inflight_text.record_publish(cid, _delta("text"))
    assert inflight_text.snapshot_for(cid), "expected tracked text before discard"

    inflight_text.discard(cid)
    # Entry gone. Non-empty would mean the relay-exit leak backstop failed.
    assert inflight_text.snapshot_for(cid) == []
    # Idempotent: discarding an untracked id must not raise.
    inflight_text.discard("conv_never_tracked")


# ── Claude-native message-scoped streaming ───────────────────────────


def _native_delta(
    message_id: str, index: int, text: str, *, final: bool = False
) -> dict[str, Any]:
    """
    Build a claude-native ``response.output_text.delta`` event dict.

    :param message_id: Vendor per-message id, e.g. ``"m1"``.
    :param index: 0-based chunk order within the message, e.g. ``0``.
    :param text: The chunk text, e.g. ``"Hello "``.
    :param final: Whether this is the message's last chunk. Sets the
        ``final`` flag; only once a message is ``final`` is its joined
        text complete and thus eligible for the content match that
        retires it against a committed ``output_item.done``.
    :returns: An event dict shaped like the forwarder's per-chunk emit.
    """
    return {
        "type": "response.output_text.delta",
        "delta": text,
        "message_id": message_id,
        "index": index,
        "final": final,
    }


def _message_done(item_id: str = "ci_x", text: str = "...") -> dict[str, Any]:
    """
    Build an assistant ``message`` ``response.output_item.done`` event.

    :param item_id: The committed item id, e.g. ``"ci_1"``.
    :param text: The committed assistant text. This is the byte-equal
        join key the retire path matches streamed deltas against, so a
        test that wants the commit to retire a specific in-flight message
        must pass that message's full streamed text here, e.g. ``"Hello"``.
    :returns: An event dict shaped like the forwarder's committed
        assistant message emission.
    """
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": item_id,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def test_native_message_replays_one_delta_with_id_and_index() -> None:
    """
    A native message's chunks replay as one delta carrying its id+index.

    The reconnect path re-emits the streamed-so-far text as a single
    message-scoped ``output_text.delta`` (message_id + highest index)
    so the web client rebuilds the in-flight preview and its live tail
    (higher indices) appends without duplicating the replayed prefix.
    """
    cid = "conv_native_one"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello "))
    inflight_text.record_publish(cid, _native_delta("m1", 1, "world"))

    snap = inflight_text.snapshot_for(cid)

    # Exactly one replay event for the one in-flight message; joined text,
    # message_id preserved (so the client scopes the preview), and index ==
    # the highest accumulated so the live tail appends from index 2.
    assert snap == [
        {
            "type": "response.output_text.delta",
            "delta": "Hello world",
            "message_id": "m1",
            "index": 1,
        }
    ], f"expected one message-scoped replay delta, got {snap!r}"


def test_native_messages_replay_in_order_not_blobbed() -> None:
    """
    Multiple in-flight messages replay as separate deltas, in stream order.

    A claude-native turn streams several messages (message → tool →
    message); each must replay under its OWN message_id, not be
    concatenated into one blob (which would lose message boundaries and
    the per-message ids the client keys previews on).
    """
    cid = "conv_native_multi"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "first"))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "second"))

    snap = inflight_text.snapshot_for(cid)

    # Two distinct deltas, m1 before m2 (insertion = stream order). One
    # blob here would mean message boundaries / ids were lost.
    assert [(e["message_id"], e["delta"]) for e in snap] == [("m1", "first"), ("m2", "second")]


def test_native_output_item_done_retires_by_content_not_position() -> None:
    """
    A committed ``message`` retires the preview whose TEXT it matches.

    Two complete messages are in flight; the commit carries the SECOND
    one's text, so the second is retired and the first — still uncommitted
    — keeps replaying. The old FIFO rule would have dropped the oldest
    (m1) regardless of content, leaving the committed m2 to double-render
    against the cold-load snapshot. The done event carries no message_id,
    so content is the only reliable join key (proven byte-equal by probe).
    """
    cid = "conv_native_content_match"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "first answer", final=True))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "second answer", final=True))

    # Commit the SECOND message's text (not the oldest).
    suppress = inflight_text.record_publish(cid, _message_done("ci_2", text="second answer"))
    assert suppress is False, "a committed item is broadcast, never suppressed from live"

    snap = inflight_text.snapshot_for(cid)
    # m2 retired by content; m1 (older, uncommitted) survives. FIFO would
    # have produced ["m1"... dropped, "m2" kept] — i.e. the wrong result.
    assert [(e["message_id"], e["delta"]) for e in snap] == [("m1", "first answer")], (
        f"content match must retire m2 (the committed text), not the oldest m1: {snap!r}"
    )


def test_pi_native_empty_final_marker_retires_message_on_commit() -> None:
    """
    A pi-native turn is retired even though its ``final`` chunk is empty.

    pi-native (and any harness using the web UI's ``finalizeStreamingMessage``
    contract) signals end-of-message with an EMPTY marker — ``delta=""`` with
    ``final=true`` — after the text chunks. The completion flag, not the
    (absent) text, is what flips ``final_seen`` and makes the message eligible
    for the byte-equal retire on ``output_item.done``.

    Regression: the old ``not delta`` guard dropped that empty marker before
    ``final_seen`` could be set, so the message was never retired. ``snapshot_for``
    then replayed the fully-committed message on every reconnect / cold-load,
    double-rendering it beside the snapshot's persisted copy (the reported bug).
    """
    cid = "conv_pi_empty_final"
    text = "I'm Claude, made by Anthropic."
    # Text chunk(s), then the separate empty finalize marker.
    inflight_text.record_publish(cid, _native_delta("pi:msg:0", 0, text, final=False))
    marker = _native_delta("pi:msg:0", 1, "", final=True)
    suppress_marker = inflight_text.record_publish(cid, marker)
    # The empty marker is broadcast (not suppressed) while the message is still
    # uncommitted — its live preview must finalize on the web client.
    assert suppress_marker is False

    # The empty marker recorded text only via its earlier chunk; the join key
    # is the full message text, so the commit retires it by content.
    suppress_done = inflight_text.record_publish(cid, _message_done("ci_pi", text=text))
    assert suppress_done is False, "a committed item is broadcast, never suppressed from live"

    # No replay: a reconnect / cold-load shows ONLY the snapshot's copy.
    assert inflight_text.snapshot_for(cid) == [], (
        "a committed pi-native message must not replay (it would double-render)"
    )


def test_pi_native_empty_final_marker_suppresses_when_commit_races_ahead() -> None:
    """
    The empty finalize marker also retires when the commit raced ahead.

    The committed ``output_item.done`` can arrive BEFORE the deltas (the
    single-chunk race): no in-flight message matches yet, so its fingerprint
    is buffered. When the text chunk then the empty ``final`` marker land, the
    marker — now honored rather than dropped — completes the message, matches
    the buffered fingerprint, retires it, and is suppressed from the live tail
    so the duplicate trailing chunk never reaches the client.
    """
    cid = "conv_pi_empty_final_race"
    text = "PONG"
    # Commit lands first (deltas raced behind): fingerprint buffered.
    inflight_text.record_publish(cid, _message_done("ci_pong", text=text))
    # Text chunk, then the empty finalize marker completes + matches it.
    inflight_text.record_publish(cid, _native_delta("pi:msg:0", 0, text, final=False))
    marker = _native_delta("pi:msg:0", 1, "", final=True)
    suppress_marker = inflight_text.record_publish(cid, marker)
    assert suppress_marker is True, "the duplicate trailing chunk must be suppressed from live"

    assert inflight_text.snapshot_for(cid) == [], "the committed message must not replay"


def test_codex_output_item_done_retires_matching_preview_without_final_delta() -> None:
    """
    Codex-native retires previews on commit even without ``final: true``.

    Codex's app-server stream has ``item/agentMessage/delta`` events and
    a later ``item/completed`` event, but the coalesced deltas are marked
    ``final: false``. The completed item is therefore the only reliable
    completion signal. If a matching ``codex:`` preview is not retired at
    commit time, a page refresh replays it from ``inflight_text`` next to
    the committed DB message, producing a duplicate assistant bubble.
    """
    cid = "conv_codex_no_final"
    message_id = "codex:thread_123:turn_123:agentMessage:item_agent"
    inflight_text.record_publish(
        cid,
        _native_delta(message_id, 0, "Hi! What would you like to work on today?", final=False),
    )

    suppress = inflight_text.record_publish(
        cid,
        _message_done("ci_1", text="Hi! What would you like to work on today?"),
    )

    assert suppress is False, "committed items are still broadcast to live clients"
    assert inflight_text.snapshot_for(cid) == [], (
        "codex completed item must retire the no-final preview so refresh cannot replay it"
    )


def test_non_codex_output_item_done_keeps_non_final_matching_preview() -> None:
    """
    Non-Codex native streams still require ``final: true`` before match.

    A non-final Claude-style chunk can be only a prefix of a longer
    in-flight message. Matching it against a same-text committed item
    would retire the wrong preview and suppress the rest of the message.
    """
    cid = "conv_native_non_final_guard"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "OK", final=False))

    inflight_text.record_publish(cid, _message_done("ci_1", text="OK"))

    snap = inflight_text.snapshot_for(cid)
    assert [(e["message_id"], e["delta"]) for e in snap] == [("m1", "OK")]


def test_native_single_chunk_commit_before_delta_is_retired_and_suppressed() -> None:
    """
    The core bug: a single-chunk message whose only chunk lands AFTER commit.

    FIFO could never handle this — at commit time the message is not
    tracked at all, so nothing is popped and its id is never retired; its
    late ``final`` chunk then resurrects a preview that both replays on
    reconnect and renders live as a duplicate of the committed message.

    Content matching closes both holes. The commit buffers its text
    fingerprint; when the lone ``final`` chunk arrives, its complete text
    matches, so the message is retired (no replay) AND
    :func:`record_publish` returns ``True`` so the publisher withholds the
    chunk from the live fan-out.
    """
    cid = "conv_native_single_chunk_race"
    # Commit arrives FIRST — no delta for this message has been seen yet.
    suppress_commit = inflight_text.record_publish(cid, _message_done("ci_1", text="Hello world"))
    assert suppress_commit is False
    assert inflight_text.snapshot_for(cid) == [], "nothing is in flight at commit time"

    # The message's lone chunk (first AND final) lands a poll later.
    suppress_delta = inflight_text.record_publish(
        cid, _native_delta("m1", 0, "Hello world", final=True)
    )

    # Withheld from the live stream (the duplicate the user kept seeing)...
    assert suppress_delta is True, (
        "the trailing chunk of an already-committed message must be withheld from the live fan-out"
    )
    # ...and never resurrected into the replay index.
    assert inflight_text.snapshot_for(cid) == [], (
        "a chunk matching an already-committed message must not create an "
        "in-flight entry (it would replay as a stale duplicate on reload)"
    )


def test_native_late_chunk_for_retired_message_is_dropped_and_suppressed() -> None:
    """
    Once retired, any later chunk for that message is dropped + suppressed.

    After a message is retired (here via the normal deltas-then-commit
    order), a stray trailing chunk for it must neither re-enter the replay
    index nor reach live subscribers.
    """
    cid = "conv_native_retired_stray"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "the answer", final=True))
    # Commit matches m1 by content → m1 retired.
    inflight_text.record_publish(cid, _message_done("ci_1", text="the answer"))
    assert inflight_text.snapshot_for(cid) == [], "m1 retired on commit"

    # A duplicate/stray chunk for the retired m1 arrives.
    suppress = inflight_text.record_publish(cid, _native_delta("m1", 1, " (extra)"))

    assert suppress is True, "a chunk for a retired message is withheld from live"
    assert inflight_text.snapshot_for(cid) == [], "and does not resurrect the preview"


def test_native_retire_by_content_leaves_a_still_streaming_sibling() -> None:
    """
    Retiring one committed message leaves a younger, still-streaming one.

    m1 completes and commits; m2 is still mid-stream (no ``final`` yet).
    Committing m1 by content retires only m1 — m2 keeps accumulating and
    replaying (the retire is scoped to the matched message, not the turn).
    """
    cid = "conv_native_sibling"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "done one", final=True))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "still "))
    # Commit m1 by content.
    inflight_text.record_publish(cid, _message_done("ci_1", text="done one"))

    # m2 keeps streaming after m1's commit.
    inflight_text.record_publish(cid, _native_delta("m2", 1, "going"))

    snap = inflight_text.snapshot_for(cid)
    assert [(e["message_id"], e["delta"]) for e in snap] == [("m2", "still going")], (
        f"only the still-streaming m2 should replay, got {snap!r}"
    )


def test_native_commit_without_matching_text_retires_nothing() -> None:
    """
    A commit whose text matches no in-flight preview must drop none.

    Content matching never mis-retires: if the committed text doesn't
    byte-match a completed in-flight message (deltas not yet arrived, or a
    multi-text-block message whose deltas concatenate differently), every
    preview survives — the fingerprint is buffered for a later match, and
    unmatched previews are cleared at turn-end, not dropped here on a
    wrong positional guess.
    """
    cid = "conv_native_no_match"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "alpha", final=True))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "beta", final=True))

    # Commit text matches NEITHER in-flight message.
    inflight_text.record_publish(cid, _message_done("ci_x", text="totally different"))

    snap = inflight_text.snapshot_for(cid)
    assert [(e["message_id"], e["delta"]) for e in snap] == [("m1", "alpha"), ("m2", "beta")], (
        f"a non-matching commit must not drop any preview (no FIFO guess): {snap!r}"
    )


def test_native_identical_text_messages_reconciled_by_count() -> None:
    """
    Two messages with identical text, commit-before-deltas: multiset count.

    When both commits arrive before either's deltas, the committed-text
    multiset holds the one fingerprint with count 2. Each message's
    ``final`` chunk consumes one, so BOTH are retired and BOTH chunks
    suppressed — and the count drains to exactly zero, so a later identical
    chunk is treated as a genuine new live message (not a phantom match a
    plain set would produce forever).
    """
    cid = "conv_native_identical"
    # Both commits first → same fingerprint, count 2.
    inflight_text.record_publish(cid, _message_done("ci_1", text="OK"))
    inflight_text.record_publish(cid, _message_done("ci_2", text="OK"))

    s1 = inflight_text.record_publish(cid, _native_delta("m1", 0, "OK", final=True))
    s2 = inflight_text.record_publish(cid, _native_delta("m2", 0, "OK", final=True))
    assert (s1, s2) == (True, True), "both duplicate chunks suppressed from live"
    assert inflight_text.snapshot_for(cid) == [], "both messages retired, neither replays"

    # Count is fully drained: a THIRD identical chunk has no buffered
    # commit to match, so it is a live message (proves a count, not a set).
    s3 = inflight_text.record_publish(cid, _native_delta("m3", 0, "OK", final=True))
    assert s3 is False, "no buffered commit remains, so this is a fresh live message"
    assert [e["message_id"] for e in inflight_text.snapshot_for(cid)] == ["m3"]


def test_native_stale_committed_fingerprint_expires_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A buffered commit fingerprint past the TTL stops suppressing later text.

    Claude-native emits no terminal ``response.*`` to clear
    ``_native_committed``, so a fingerprint whose matching delta never
    arrives (a multi-text-block mismatch, or a delta the best-effort
    forwarder dropped) would otherwise persist for the whole session. This
    pins that it goes stale: an identical-text message a later turn must NOT
    be suppressed by it.

    Drives the monotonic clock via the ``_monotonic`` indirection (patched,
    not the global ``time.monotonic`` — that would leak across workers).
    """
    cid = "conv_native_ttl_expire"
    clock = {"now": 1000.0}
    monkeypatch.setattr(inflight_text, "_monotonic", lambda: clock["now"])

    # A commit lands first (single-chunk race) and buffers "OK" at t=1000,
    # but its own delta never arrives (e.g. dropped) — the fingerprint is
    # now stale and would mis-suppress a future "OK".
    inflight_text.record_publish(cid, _message_done("ci_1", text="OK"))

    # A genuinely independent "OK" message streams a later turn, past the TTL.
    clock["now"] = 1000.0 + inflight_text._NATIVE_COMMITTED_TTL_S + 1.0
    suppress = inflight_text.record_publish(cid, _native_delta("m_later", 0, "OK", final=True))

    # Expired → NOT a match: True here would mean the stale fingerprint hid a
    # legitimate later message (the cross-turn-leak regression this guards).
    assert suppress is False, "a fingerprint older than the TTL must not suppress a later message"
    # And the later message is a normal live message (still in the index).
    assert [e["message_id"] for e in inflight_text.snapshot_for(cid)] == ["m_later"], (
        "the un-suppressed later message must remain a live preview"
    )


def test_native_committed_fingerprint_within_ttl_still_suppresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The legitimate single-chunk race (delta within the TTL) still suppresses.

    The companion to the expiry test: a delta arriving inside the TTL window
    is still the commit's own racing chunk, so it must be retired and
    suppressed exactly as before. Guards against a TTL set so tight it
    re-opens the duplicate the fix exists to kill.
    """
    cid = "conv_native_ttl_fresh"
    clock = {"now": 5000.0}
    monkeypatch.setattr(inflight_text, "_monotonic", lambda: clock["now"])

    # Commit buffers "Hello" at t=5000.
    inflight_text.record_publish(cid, _message_done("ci_1", text="Hello"))

    # Its racing final delta lands well within the TTL window.
    clock["now"] = 5000.0 + (inflight_text._NATIVE_COMMITTED_TTL_S / 2.0)
    suppress = inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello", final=True))

    assert suppress is True, (
        "a delta racing within the TTL is the commit's own chunk — suppress it"
    )
    assert inflight_text.snapshot_for(cid) == [], "and the message is retired (no replay)"


def test_native_output_item_done_non_message_keeps_previews() -> None:
    """
    A ``function_call`` ``output_item.done`` must NOT drop a preview.

    Only assistant message items commit streamed text; a tool-call item
    is unrelated, so dropping a preview on it would lose an in-flight
    message that is still streaming.
    """
    cid = "conv_native_tool"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "thinking"))
    inflight_text.record_publish(
        cid,
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "id": "fc_1", "name": "Bash", "arguments": "{}"},
        },
    )

    snap = inflight_text.snapshot_for(cid)
    # The in-flight message survives a tool-call commit.
    assert [e["message_id"] for e in snap] == ["m1"]


def test_native_delta_dedupes_by_index() -> None:
    """
    A replayed chunk at an already-seen index is ignored (no double text).

    The forwarder de-dupes by ``(message_id, index)`` and forwards in
    order, but a reconnect/replay can re-deliver a chunk; a chunk at or
    below the high-water index must not be appended again.
    """
    cid = "conv_native_dup"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello "))
    inflight_text.record_publish(cid, _native_delta("m1", 1, "world"))
    # Duplicate of index 1 (and a stale index 0) — both ignored.
    inflight_text.record_publish(cid, _native_delta("m1", 1, "world"))
    inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello "))

    snap = inflight_text.snapshot_for(cid)
    assert snap[0]["delta"] == "Hello world", f"duplicate index doubled the text: {snap!r}"


@pytest.mark.parametrize("terminal_status", ["idle", "failed"])
def test_native_previews_survive_terminal_session_status(terminal_status: str) -> None:
    """
    A terminal ``session.status`` must NOT clear in-flight native previews.

    Claude-native goes ``idle`` (its PTY falls quiet) MID-TURN while
    parked on a permission prompt, with streamed text that has not yet
    committed to the store. Dropping the preview on ``idle`` would lose
    exactly that text on reload — the user-reported bug (streamed text +
    pending elicitation, gone on refresh). Native previews are evicted
    per-message by ``output_item.done`` and wholesale by ``discard``, not
    by session status. (Contrast ``test_terminal_session_status_clears_entry``,
    which pins that the RESPONSE-scoped blob still clears on ``idle`` —
    in-process agents emit ``waiting`` for a parked elicitation, so their
    ``idle`` always means turn-over.)
    """
    cid = "conv_native_status"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "partial answer"))

    inflight_text.record_publish(cid, _status(terminal_status))

    # The preview survives the status flip so a refresh during the
    # approval-wait still replays the streamed-so-far text.
    snap = inflight_text.snapshot_for(cid)
    assert [e["message_id"] for e in snap] == ["m1"], (
        f"native preview wrongly dropped by session.status {terminal_status!r}: {snap!r}"
    )
    assert snap[0]["delta"] == "partial answer"


def test_native_discard_clears_previews() -> None:
    """:func:`discard` drops in-flight native previews (relay-exit backstop)."""
    cid = "conv_native_discard"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "partial"))
    assert inflight_text.snapshot_for(cid), "expected a preview before discard"

    inflight_text.discard(cid)

    assert inflight_text.snapshot_for(cid) == []
