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
    :param final: Whether this is the message's last chunk.
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
        join key the reconciler matches against streamed deltas, so a
        test that wants the commit to cover a specific in-flight message
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


def test_native_message_replays_one_aggregate_with_id_and_index() -> None:
    """Reconnect replay carries the full aggregate and its high-water index."""
    cid = "conv_native_one"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello "))
    inflight_text.record_publish(cid, _native_delta("m1", 1, "world"))

    assert inflight_text.snapshot_for(cid) == [
        {
            "type": "response.output_text.delta",
            "delta": "Hello world",
            "message_id": "m1",
            "index": 1,
        }
    ]


def test_native_messages_replay_in_order_not_blobbed() -> None:
    """Each native message keeps its own reconnect aggregate."""
    cid = "conv_native_multi"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "first"))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "second"))

    snap = inflight_text.snapshot_for(cid)

    assert [(event["message_id"], event["delta"]) for event in snap] == [
        ("m1", "first"),
        ("m2", "second"),
    ]


def test_native_done_drops_matched_aggregate_and_supersedes_older() -> None:
    """A commit drops its own aggregate by content and evicts older ones.

    Committing the MIDDLE message proves the match is by content, not
    position (a position drop would take the oldest ``m1``), while ``m1``
    is evicted as superseded and the still-streamable tail ``m3`` survives.
    """
    cid = "conv_native_content_match"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "alpha", final=True))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "beta", final=True))
    inflight_text.record_publish(cid, _native_delta("m3", 0, "gamma"))
    done = _message_done("ci_2", text="beta")

    assert inflight_text.record_publish(cid, done) is done
    snap = inflight_text.snapshot_for(cid)
    assert [(event["message_id"], event["delta"]) for event in snap] == [("m3", "gamma")]


def test_native_done_supersedes_stranded_older_message() -> None:
    """A later commit evicts an earlier aggregate that never reconciled.

    Reproduces the reconnect double-message bug: ``m1`` streamed but its
    own commit never reconciled (a lost/mismatched delta), so it lingered
    in the map and replayed on every reconnect. ``m2`` committing
    supersedes it, so neither replays.
    """
    cid = "conv_native_stranded"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "I'll do the rebase.", final=True))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "Back to a clean state.", final=True))

    done = _message_done("ci_2", text="Back to a clean state.")
    assert inflight_text.record_publish(cid, done) is done

    assert inflight_text.snapshot_for(cid) == []


def test_stranded_commit_supersedes_older_but_keeps_tail() -> None:
    """A commit matching no aggregate evicts older ones, sparing the tail.

    When the committed text matches no tracked aggregate (its own deltas
    were lost/mismatched), earlier messages are still superseded, but the
    most-recent entry is spared in case it is still streaming.
    """
    cid = "conv_native_stranded_commit"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "alpha", final=True))
    inflight_text.record_publish(cid, _native_delta("m2", 0, "beta"))

    done = _message_done("ci_x", text="mismatched bytes")
    assert inflight_text.record_publish(cid, done) is done

    assert [(e["message_id"], e["delta"]) for e in inflight_text.snapshot_for(cid)] == [
        ("m2", "beta")
    ]


def test_pi_empty_final_marker_is_inert() -> None:
    """Pi's empty final marker carries state but never reaches subscribers."""
    cid = "conv_pi_marker"
    delta = _native_delta("pi:msg:0", 0, "PONG")
    marker = _native_delta("pi:msg:0", 1, "", final=True)

    assert inflight_text.record_publish(cid, delta) == delta
    assert inflight_text.record_publish(cid, marker) is None
    inflight_text.record_publish(cid, _message_done("ci_pi", text="PONG"))

    assert inflight_text.snapshot_for(cid) == []


def test_codex_done_drops_equal_aggregate_without_final_delta() -> None:
    """Commit-time equality works for providers that never set final."""
    cid = "conv_codex_no_final"
    inflight_text.record_publish(
        cid,
        _native_delta(
            "codex:thread_123:turn_123:agentMessage:item_agent",
            0,
            "Hi! What would you like to work on today?",
        ),
    )

    inflight_text.record_publish(
        cid,
        _message_done("ci_1", text="Hi! What would you like to work on today?"),
    )

    assert inflight_text.snapshot_for(cid) == []


def test_done_first_suppresses_every_matching_late_delta() -> None:
    """Every late chunk stays hidden while its aggregate matches the committed text."""
    cid = "conv_done_first"
    done = _message_done("ci_1", text="Hello big world")
    assert inflight_text.record_publish(cid, done) is done

    assert inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello ")) is None
    assert inflight_text.snapshot_for(cid) == []
    assert inflight_text.record_publish(cid, _native_delta("m1", 1, "big ")) is None
    assert inflight_text.snapshot_for(cid) == []
    assert inflight_text.record_publish(cid, _native_delta("m1", 2, "world", final=True)) is None
    assert inflight_text.snapshot_for(cid) == []


def test_done_claims_a_partial_aggregate_until_late_tail_arrives() -> None:
    """A normal delta→done→late-delta race never replays the covered prefix."""
    cid = "conv_partial_then_done"
    first = _native_delta("m1", 0, "Hello ")
    assert inflight_text.record_publish(cid, first) == first

    inflight_text.record_publish(cid, _message_done("ci_1", text="Hello world"))
    assert inflight_text.snapshot_for(cid) == []

    assert inflight_text.record_publish(cid, _native_delta("m1", 1, "world", final=True)) is None
    assert inflight_text.snapshot_for(cid) == []


def test_shared_prefix_divergence_recovers_the_whole_aggregate_once() -> None:
    """A genuinely new message sharing a committed prefix streams in full on divergence."""
    cid = "conv_prefix_collision"
    inflight_text.record_publish(cid, _message_done("ci_old", text="OK"))

    assert inflight_text.record_publish(cid, _native_delta("m2", 0, "OK")) is None

    divergent = _native_delta("m2", 1, " so")
    recovered = inflight_text.record_publish(cid, divergent)
    assert recovered == {**divergent, "delta": "OK so"}

    tail = _native_delta("m2", 2, " now", final=True)
    assert inflight_text.record_publish(cid, tail) is tail
    assert inflight_text.snapshot_for(cid)[0]["delta"] == "OK so now"


def test_recent_committed_window_is_count_bounded() -> None:
    """A committed prefix ages out after three newer messages without a clock."""
    cid = "conv_recent_window"
    for index, text in enumerate(("old", "one", "two", "three")):
        inflight_text.record_publish(cid, _message_done(f"ci_{index}", text=text))

    delta = _native_delta("m_new", 0, "old")
    assert inflight_text.record_publish(cid, delta) == delta


def test_identical_done_first_messages_are_consumed_by_count() -> None:
    """Each equal aggregate consumes one matching recent committed occurrence."""
    cid = "conv_native_identical"
    inflight_text.record_publish(cid, _message_done("ci_1", text="OK"))
    inflight_text.record_publish(cid, _message_done("ci_2", text="OK"))

    assert inflight_text.record_publish(cid, _native_delta("m1", 0, "OK", final=True)) is None
    assert inflight_text.record_publish(cid, _native_delta("m2", 0, "OK", final=True)) is None
    assert inflight_text.snapshot_for(cid) == []

    fresh = _native_delta("m3", 0, "OK")
    assert inflight_text.record_publish(cid, fresh) == fresh


def test_native_output_item_done_non_message_keeps_previews() -> None:
    """Tool-call done items do not reconcile assistant text."""
    cid = "conv_native_tool"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "thinking"))
    inflight_text.record_publish(
        cid,
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "id": "fc_1", "name": "Bash", "arguments": "{}"},
        },
    )

    assert [event["message_id"] for event in inflight_text.snapshot_for(cid)] == ["m1"]


def test_native_delta_dedupes_by_index_before_fanout() -> None:
    """A repeated or stale chunk index is centrally suppressed."""
    cid = "conv_native_dup"
    inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello "))
    inflight_text.record_publish(cid, _native_delta("m1", 1, "world"))

    assert inflight_text.record_publish(cid, _native_delta("m1", 1, "world")) is None
    assert inflight_text.record_publish(cid, _native_delta("m1", 0, "Hello ")) is None
    assert inflight_text.snapshot_for(cid)[0]["delta"] == "Hello world"


def test_native_reconnect_aggregate_and_live_tail_do_not_overlap() -> None:
    """Reconnect gets the aggregate while the next live event stays incremental."""
    cid = "conv_native_reconnect"
    first = _native_delta("m1", 0, "Hello ")
    assert inflight_text.record_publish(cid, first) == first
    assert inflight_text.snapshot_for(cid)[0]["delta"] == "Hello "

    tail = _native_delta("m1", 1, "world")
    assert inflight_text.record_publish(cid, tail) is tail
    assert inflight_text.snapshot_for(cid)[0]["delta"] == "Hello world"


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
