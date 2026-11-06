"""
Unit tests for :mod:`omnigent.runtime.pending_client_tool_calls`.

The pending-client-tool-calls index is a per-conversation map of
outstanding ``action_required`` function_call ids that lets a
reconnecting client discover and answer a call parked while it was
disconnected — the live SSE stream has no replay. Tests here pin its
core invariants directly, mirroring
:mod:`tests.runtime.test_pending_elicitations`:

* :func:`record_publish` only acts on the event shapes the module
  docstring describes and silently ignores every other type — it sits
  on the hot SSE publish path.
* :func:`resolve` removes ids, is idempotent, and cleans up empty
  conversation sets so :func:`snapshot_for` returns ``[]`` cleanly.
* :func:`clear_for` drops every outstanding call for a conversation at
  once, for turn-end events that never emit their own per-call
  completion (an interrupt cancels ``dispatch_tool`` without emitting
  a terminal item for it).

The wire-up between :func:`omnigent.runtime.session_stream.publish` and
the index lives in :file:`tests/runtime/test_session_stream.py`; this
file tests the module in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.runtime import pending_client_tool_calls


@pytest.fixture(autouse=True)
def _clean_pending_client_tool_calls_index() -> None:
    """Reset the module-global index between tests, like its elicitations sibling."""
    pending_client_tool_calls.reset_for_tests()
    yield
    pending_client_tool_calls.reset_for_tests()


def _action_required_event(
    call_id: str, name: str = "lookup_secret_token", *, client_owned: bool = True
) -> dict[str, Any]:
    """Build a minimal ``response.output_item.done`` action_required event.

    :param call_id: Tool call id to embed, e.g. ``"call_abc"``.
    :param name: Tool name the LLM called.
    :param client_owned: Matches ``TurnContext.dispatch_tool``'s marker —
        ``False`` models a server-executed dispatch (host-tool bridge, MCP,
        spec-local) that must never appear in the reconnect snapshot.
    :returns: An event dict shaped like the real SSE payload.
    """
    return {
        "type": "response.output_item.done",
        "item": {
            "id": "fc_1",
            "type": "function_call",
            "status": "action_required",
            "name": name,
            "arguments": "{}",
            "call_id": call_id,
            "agent": "test-agent",
            "client_owned": client_owned,
        },
    }


def _function_call_output_event(call_id: str, output: str = "ok") -> dict[str, Any]:
    """Build a minimal ``response.output_item.done`` function_call_output event.

    :param call_id: Tool call id the output answers.
    :param output: Tool result string.
    """
    return {
        "type": "response.output_item.done",
        "item": {
            "id": "fco_1",
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
            "status": "completed",
        },
    }


def test_record_publish_adds_outstanding_call_on_action_required() -> None:
    """An action_required function_call event adds a snapshot entry keyed by call_id."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    snapshot = pending_client_tool_calls.snapshot_for("conv_a")
    # Exactly one entry for the one call published. If empty, the add
    # branch's status/type filter rejected a valid action_required event.
    assert len(snapshot) == 1
    assert snapshot[0]["item"]["call_id"] == "call_1"


def test_record_publish_excludes_server_executed_calls_from_the_snapshot() -> None:
    """A server-executed dispatch (``client_owned: False``) never joins the index.

    Reproduces the second watchdog defect: the snapshot must only ever offer
    a reconnecting client calls it can actually answer. A host-tool-bridge,
    MCP, or spec-local dispatch is executed by Omnigent's own machinery —
    showing it as "pending" would invite a client to try to answer a call
    that was never theirs to answer. A genuinely client-owned call on the
    same conversation is unaffected.
    """
    pending_client_tool_calls.record_publish(
        "conv_a", _action_required_event("call_server", "search_docs", client_owned=False)
    )
    pending_client_tool_calls.record_publish(
        "conv_a", _action_required_event("call_client", "lookup_secret_token", client_owned=True)
    )
    snapshot = pending_client_tool_calls.snapshot_for("conv_a")
    # Exactly the client-owned call — the server-executed one must be absent,
    # not merely unresolved.
    assert [event["item"]["call_id"] for event in snapshot] == ["call_client"]


def test_record_publish_tracks_multiple_distinct_calls() -> None:
    """Multiple distinct call ids on the same conversation accumulate."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_2"))
    snapshot = pending_client_tool_calls.snapshot_for("conv_a")
    # 2 = parallel tool calls both tracked. If 1, one clobbered the other
    # (e.g. a dict keyed by conversation only, not call_id).
    assert {event["item"]["call_id"] for event in snapshot} == {"call_1", "call_2"}


def test_record_publish_is_idempotent_on_repeat_publish() -> None:
    """Re-publishing the same action_required call_id overwrites, not duplicates."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    # Still one entry — a dict-keyed store naturally dedupes by call_id.
    assert len(pending_client_tool_calls.snapshot_for("conv_a")) == 1


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "session.status", "status": "running"},
        {"type": "response.completed"},
        # A function_call item that hasn't parked yet (in_progress / completed
        # status) must never be tracked — only the action_required status is
        # a parked call.
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "status": "in_progress", "call_id": "call_x"},
        },
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "status": "completed", "call_id": "call_x"},
        },
        # Missing/malformed item payloads must not raise.
        {"type": "response.output_item.done", "item": "not-a-dict"},
        {"type": "response.output_item.done"},
    ],
)
def test_record_publish_ignores_non_tracking_events(event: dict[str, Any]) -> None:
    """Events that are not an outstanding action_required call leave the index untouched."""
    pending_client_tool_calls.record_publish("conv_a", event)
    # The hot publish path must filter every one of these to a no-op;
    # anything > 0 here means an unrelated event type inflated the index.
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


@pytest.mark.parametrize("bad_call_id", [None, "", 42, {"nested": "value"}, []])
def test_record_publish_ignores_invalid_call_id(bad_call_id: Any) -> None:
    """A malformed call_id on an action_required event is dropped, not tracked."""
    event = {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "status": "action_required", "call_id": bad_call_id},
    }
    pending_client_tool_calls.record_publish("conv_a", event)
    # A phantom entry here could never be resolved by a real call_id later.
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


def test_record_publish_drains_on_matching_function_call_output() -> None:
    """The scaffold's own completed function_call_output round-trip drains the entry."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.record_publish("conv_a", _function_call_output_event("call_1"))
    # 0 = the output event drained the matching call. If 1, the drain
    # branch isn't wired to function_call_output items and a reconnecting
    # client would be shown an already-answered call as still pending.
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


def test_record_publish_drains_on_session_input_consumed_for_function_call_output() -> None:
    """The native-mirrored accept path (session.input.consumed) also drains the entry."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.record_publish(
        "conv_a",
        {
            "type": "session.input.consumed",
            "data": {
                "type": "function_call_output",
                "data": {"call_id": "call_1", "output": "ok"},
            },
        },
    )
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


@pytest.mark.parametrize(
    "event_type",
    [
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
        "session.interrupted",
    ],
)
def test_record_publish_clears_all_calls_on_turn_end(event_type: str) -> None:
    """Every turn-end event type drains every outstanding call for the conversation.

    A cancelled call (interrupt) never emits its own function_call_output or
    completion item, so this is the only signal that it is no longer
    answerable — without it a reconnecting client could be shown a call
    whose harness awaiter has already been cancelled.
    """
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_2"))
    pending_client_tool_calls.record_publish("conv_a", {"type": event_type})
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


def test_resolve_removes_outstanding_call() -> None:
    """Resolving a tracked call_id drops it from the snapshot."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.resolve("conv_a", "call_1")
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


def test_resolve_is_idempotent_on_unknown_call_id() -> None:
    """Resolving an unknown or already-drained call_id is a silent no-op.

    This is what makes a duplicate answer — or an answer the index never
    saw because it landed on a different replica — harmless.
    """
    pending_client_tool_calls.resolve("conv_a", "call_never_tracked")
    assert pending_client_tool_calls.snapshot_for("conv_a") == []
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.resolve("conv_a", "call_1")
    # A second resolve of the same call_id must not raise.
    pending_client_tool_calls.resolve("conv_a", "call_1")
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


def test_an_interim_output_drains_the_entry_and_a_later_real_output_is_still_harmless() -> None:
    """An interim/placeholder answer drains the index exactly like any other answer.

    The index only looks at ``call_id`` — it has no notion of an "interim" vs
    a "real" answer. Whichever ``function_call_output`` the index observes
    first for a ``call_id`` is the one that drains the entry; nothing about
    the output's content changes that. A second, later output for the same
    ``call_id`` (e.g. a genuine answer posted after a client's own interim
    placeholder reply) is resolved the same as any duplicate: a harmless
    no-op against this index. The server's accept path
    (``POST /v1/sessions/{id}/events``) does not special-case or reject a
    second ``function_call_output`` for an already-answered call either
    (see ``test_reconnect_recovers_and_answers_a_parked_client_tool_call``
    in ``tests/integration/test_client_tool_reconnect.py``, which posts a
    duplicate answer and asserts it is accepted).
    """
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    # The interim/placeholder reply — content is irrelevant to the index.
    pending_client_tool_calls.record_publish(
        "conv_a", _function_call_output_event("call_1", output="pending")
    )
    assert pending_client_tool_calls.snapshot_for("conv_a") == []
    # The later real answer for the same call_id is still a harmless no-op
    # against the index — it does not resurrect the entry or raise.
    pending_client_tool_calls.resolve("conv_a", "call_1")
    assert pending_client_tool_calls.snapshot_for("conv_a") == []


def test_resolve_keeps_other_calls_on_same_conversation() -> None:
    """Resolving one of several outstanding calls leaves the rest in place."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_2"))
    pending_client_tool_calls.resolve("conv_a", "call_1")
    snapshot = pending_client_tool_calls.snapshot_for("conv_a")
    assert [event["item"]["call_id"] for event in snapshot] == ["call_2"]


def test_resolve_drops_empty_conversation_entry() -> None:
    """After every call for a conversation resolves, the conversation key is popped."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.resolve("conv_a", "call_1")
    # Probe internals via the public API's backing cache to confirm no
    # empty entry lingers (mirrors the equivalent pending_elicitations test).
    assert "conv_a" not in pending_client_tool_calls._pending


def test_conversations_are_independent() -> None:
    """A call on one conversation does not affect another conversation's snapshot."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_a"))
    pending_client_tool_calls.record_publish("conv_b", _action_required_event("call_b"))
    pending_client_tool_calls.resolve("conv_a", "call_a")
    assert pending_client_tool_calls.snapshot_for("conv_a") == []
    assert len(pending_client_tool_calls.snapshot_for("conv_b")) == 1


def test_clear_for_drains_all_calls_directly() -> None:
    """:func:`clear_for` drops every outstanding call for one conversation."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_2"))
    pending_client_tool_calls.record_publish("conv_b", _action_required_event("call_3"))
    pending_client_tool_calls.clear_for("conv_a")
    assert pending_client_tool_calls.snapshot_for("conv_a") == []
    # conv_b is untouched — clear_for must be scoped to the one conversation.
    assert len(pending_client_tool_calls.snapshot_for("conv_b")) == 1


def test_snapshot_for_returns_full_event_payloads_in_order() -> None:
    """:func:`snapshot_for` returns the original event dicts, in insertion order."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1", "tool_a"))
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_2", "tool_b"))
    snapshot = pending_client_tool_calls.snapshot_for("conv_a")
    assert len(snapshot) == 2
    assert snapshot[0]["item"]["name"] == "tool_a"
    assert snapshot[1]["item"]["name"] == "tool_b"


def test_snapshot_for_returns_empty_for_untracked_session() -> None:
    """Sessions with nothing outstanding return an empty list, not a KeyError.

    ``GET /v1/sessions/{id}`` reads the snapshot unconditionally on every
    request, so raising here would break every session's snapshot.
    """
    assert pending_client_tool_calls.snapshot_for("conv_nonexistent") == []


def test_snapshot_for_returns_independent_deep_copies() -> None:
    """Mutating a returned snapshot entry must not poison the internal index."""
    pending_client_tool_calls.record_publish("conv_a", _action_required_event("call_1"))
    snapshot = pending_client_tool_calls.snapshot_for("conv_a")
    snapshot[0]["item"]["arguments"] = "tampered"
    snapshot_again = pending_client_tool_calls.snapshot_for("conv_a")
    # If this reads "tampered", the snapshot leaked a shared reference
    # into the index instead of a deep copy.
    assert snapshot_again[0]["item"]["arguments"] == "{}"
