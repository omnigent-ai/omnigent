"""
Unit tests for :mod:`omnigent.runtime.pending_elicitations`.

The pending-elicitations index is a per-conversation set of
outstanding elicitation ids that powers the sidebar's "needs
attention" badge. Tests here pin its core invariants directly:

* :func:`record_publish` only acts on
  ``response.elicitation_request`` events and silently ignores
  every other type — it sits on the hot SSE publish path.
* :func:`resolve` removes ids, is idempotent, and cleans up
  empty conversation sets so :func:`count_for` returns ``0``
  cleanly.
* :func:`counts_for` is a one-pass batch lookup that includes
  every requested id (0 for untracked).

The wire-up between :func:`omnigent.runtime.session_stream.publish`
and the index lives in
:file:`tests/runtime/test_session_stream.py`; this file tests the
module in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.runtime import pending_elicitations


@pytest.fixture(autouse=True)
def _clean_pending_elicitations_index() -> None:
    """
    Reset the module-global pending-elicitations dict between tests.

    The index is process-global; without this fixture, a test that
    leaks an entry would silently change the behavior of every
    later test by inflating counts.
    """
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


def _elicit_event(elicitation_id: str, tool_name: str | None = None) -> dict[str, Any]:
    """
    Build a minimal ``response.elicitation_request`` event dict.

    The real event carries a ``params`` block; most index bookkeeping
    only reads ``type`` and ``elicitation_id`` so the ``params`` are
    omitted unless ``tool_name`` is supplied.

    :param elicitation_id: Correlation id to embed,
        e.g. ``"elicit_abc"``.
    :param tool_name: When set, stamped onto a ``params`` block as the
        gated tool name that the UI can render. Left off by callers
        exercising id-only bookkeeping (and to model server-emitted
        policy elicitations, which carry no tool name).
    :returns: An event dict shaped like the SSE payload.
    """
    event: dict[str, Any] = {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
    }
    if tool_name is not None:
        event["params"] = {"tool_name": tool_name}
    return event


def test_record_publish_increments_count_for_elicitation_event() -> None:
    """
    An elicitation_request event with a valid id increments
    the per-conversation count by one — this is the primary
    "session needs attention" signal.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    # The session now has one outstanding prompt; the sidebar
    # should render a badge of "1" — anything other than 1 here
    # means the publish-time increment isn't taking.
    assert pending_elicitations.count_for("conv_a") == 1


def test_record_publish_is_idempotent_on_repeat_publish() -> None:
    """
    Re-publishing the same elicitation_id does not double-count.

    The underlying container is a set, so the second add is a
    no-op. This matters because callers can re-publish on
    reconnect or retry, and a duplicate id should not inflate
    the badge.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    # Still 1 — if 2, the index is using a list/Counter and
    # the sidebar would over-badge sessions on republish.
    assert pending_elicitations.count_for("conv_a") == 1


def test_record_publish_tracks_multiple_distinct_ids() -> None:
    """
    Multiple distinct ids on the same conversation accumulate.

    A session can have several approvals queued (e.g. a tool
    chain that asks for permission on each step).
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_2"))
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_3"))
    # 3 = three distinct elicitation ids on one session. If 1,
    # ids are clobbering each other (e.g. dict-keyed by conv
    # only); if 2, one id was filtered out unexpectedly.
    assert pending_elicitations.count_for("conv_a") == 3


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "session.status", "status": "running"},
        {"type": "response.completed"},
        # Defensive — an event payload missing the type field at
        # all should be silently ignored, not crash.
        {"elicitation_id": "elicit_x"},
    ],
)
def test_record_publish_ignores_non_elicitation_events(event: dict[str, Any]) -> None:
    """
    Non-elicitation events do not touch the index.

    record_publish sits on the hot publish path — every text
    delta, status, and tool event flows through it. Only
    ``response.elicitation_request`` events should mutate state.
    """
    pending_elicitations.record_publish("conv_a", event)
    # The conversation should never appear in the index for
    # non-elicitation events. count_for returning > 0 here
    # would mean the type filter is broken — every text delta
    # would inflate the sidebar badge.
    assert pending_elicitations.count_for("conv_a") == 0


@pytest.mark.parametrize(
    "bad_id",
    [None, "", 42, {"nested": "value"}, []],
)
def test_record_publish_ignores_invalid_elicitation_id(bad_id: Any) -> None:
    """
    A malformed elicitation_id is silently dropped, not tracked.

    The index keys on the id string; a non-string or empty
    value can't be matched by ``resolve`` later, so tracking it
    would create a permanent phantom entry. Drop loudly via
    return rather than raising — the SSE publish path must
    not throw.
    """
    event: dict[str, Any] = {
        "type": "response.elicitation_request",
        "elicitation_id": bad_id,
    }
    pending_elicitations.record_publish("conv_a", event)
    # Index unchanged — a phantom entry here would render a
    # badge the user can never clear.
    assert pending_elicitations.count_for("conv_a") == 0


def test_resolve_removes_outstanding_id() -> None:
    """
    Resolving a tracked id drops the per-session count back to zero.

    This is what fires when the user accepts/rejects in the UI —
    the Omnigent server's approval dispatch calls
    :func:`resolve` and the sidebar badge should clear on the
    next poll.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    pending_elicitations.resolve("conv_a", "elicit_1")
    # 0 = the verdict landed and the badge clears. If 1, the
    # decrement isn't taking and stale badges accumulate.
    assert pending_elicitations.count_for("conv_a") == 0


def test_resolve_is_idempotent_on_unknown_id() -> None:
    """
    Resolving an unknown id is a no-op, never an error.

    The approval dispatch path doesn't gate its resolve call on
    whether the id is in the index (e.g. when running in
    multi-replica mode, the id may live on a different
    replica). The function must accept unknown ids silently.
    """
    # No tracked state — resolve must not raise.
    pending_elicitations.resolve("conv_a", "elicit_never_tracked")
    assert pending_elicitations.count_for("conv_a") == 0


def test_resolve_drops_empty_conversation_set() -> None:
    """
    After every id for a conversation is resolved, the
    conversation key is removed so :func:`count_for` can
    return 0 without leaving stale empty sets behind. This
    keeps memory bounded in long-running processes that see
    elicitations across many sessions.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    pending_elicitations.resolve("conv_a", "elicit_1")
    # Probe internals via the public API — count is 0 either way,
    # but check the dict directly to confirm the key was popped.
    # If the key is still present (empty set), memory leaks
    # accumulate one entry per resolved conversation forever.
    assert "conv_a" not in pending_elicitations._pending


def test_resolve_keeps_other_ids_on_same_conversation() -> None:
    """
    Resolving one id of N leaves the other N-1 in place.

    A multi-step tool chain may have multiple approvals
    outstanding; one verdict shouldn't clear the rest.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_2"))
    pending_elicitations.resolve("conv_a", "elicit_1")
    # 1 = only the resolved id was removed. If 0, resolve is
    # clobbering the whole conversation; if 2, it isn't
    # decrementing at all.
    assert pending_elicitations.count_for("conv_a") == 1


def test_counts_for_returns_zero_for_untracked_sessions() -> None:
    """
    Batch lookup includes every requested id in the result,
    even ones with no tracked elicitations.

    The list_sessions handler relies on this — it iterates the
    full page of sessions and looks up each id by key. A
    missing entry in the result would either KeyError or
    silently default — both surprising.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    counts = pending_elicitations.counts_for(["conv_a", "conv_b", "conv_c"])
    # Tracked session reports its real count; untracked
    # sessions report 0 explicitly. If conv_b or conv_c are
    # missing from the dict, the handler's `.get(id, 0)` would
    # paper it over, but the contract is to return all ids.
    assert counts == {"conv_a": 1, "conv_b": 0, "conv_c": 0}


def test_counts_for_handles_empty_input() -> None:
    """
    An empty session list returns an empty mapping, not a
    KeyError or a snapshot of the whole index. Matches what
    list_sessions does when its page is empty.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    counts = pending_elicitations.counts_for([])
    # Empty input → empty output. If this returns the full
    # index, the route layer would over-report counts for
    # sessions the caller didn't ask about.
    assert counts == {}


def test_conversations_are_independent() -> None:
    """
    An elicitation on one conversation does not affect another.

    Cross-session leakage would mean the sidebar lights up
    every row whenever any session gets a prompt.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_a"))
    pending_elicitations.record_publish("conv_b", _elicit_event("elicit_b"))
    pending_elicitations.resolve("conv_a", "elicit_a")
    # conv_a cleared, conv_b untouched. If conv_b is 0, the
    # resolve scope is too wide; if conv_a is still 1, the
    # resolve missed.
    assert pending_elicitations.count_for("conv_a") == 0
    assert pending_elicitations.count_for("conv_b") == 1


def test_record_publish_clears_index_on_elicitation_resolved_event() -> None:
    """
    A ``response.elicitation_resolved`` event flowing through the
    publish chokepoint clears the matching index entry.

    The runner emits this event from the ``finally`` block of its
    own approval wait — that's the only signal the Omnigent server gets
    when the runner's Future was cancelled / timed out without a
    UI verdict. If the type filter doesn't match this event, the
    badge stays stuck after the runner gives up.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    assert pending_elicitations.count_for("conv_a") == 1
    pending_elicitations.record_publish(
        "conv_a",
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": "elicit_1",
        },
    )
    # 0 = the resolve branch fired. If 1, the publish chokepoint
    # ignored the resolved event and the badge would stay stuck
    # after every runner-side timeout / cancellation.
    assert pending_elicitations.count_for("conv_a") == 0


def test_record_publish_handles_resolved_event_for_unknown_id() -> None:
    """
    A ``response.elicitation_resolved`` event for an id that was
    never tracked is a silent no-op — the runner can fire-and-
    forget at every Future cleanup without coordinating with the
    Omnigent server's view of what's currently tracked.
    """
    pending_elicitations.record_publish(
        "conv_a",
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": "elicit_never_seen",
        },
    )
    # 0 and no exception — if this raised, the runner's
    # fire-and-forget contract would be broken.
    assert pending_elicitations.count_for("conv_a") == 0


def test_snapshot_for_returns_full_event_payloads() -> None:
    """
    :func:`snapshot_for` returns the event dicts originally passed
    to :func:`record_publish`, in insertion order, so cold-load
    callers can replay them into the UI's block stream.

    Catches a regression where the index drops the params payload
    and only retains the id — that would render an empty
    ApprovalCard with no prompt text.
    """
    event_one = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_first",
        "params": {"message": "Approve tool A?", "mode": "form"},
    }
    event_two = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_second",
        "params": {"message": "Approve tool B?", "mode": "form"},
    }
    pending_elicitations.record_publish("conv_a", event_one)
    pending_elicitations.record_publish("conv_a", event_two)
    snapshot = pending_elicitations.snapshot_for("conv_a")
    # Both payloads survive, in publish order. If the order is
    # reversed or one is missing, the UI's cold-load replay would
    # show prompts in the wrong order or drop one entirely.
    assert len(snapshot) == 2
    assert snapshot[0]["elicitation_id"] == "elicit_first"
    assert snapshot[0]["params"]["message"] == "Approve tool A?"
    assert snapshot[1]["elicitation_id"] == "elicit_second"
    assert snapshot[1]["params"]["message"] == "Approve tool B?"


def test_snapshot_for_returns_empty_for_untracked_session() -> None:
    """
    Sessions with no outstanding prompts return an empty list, not
    a KeyError. The route handler reads the snapshot unconditionally
    on every ``GET /v1/sessions/{id}`` — raising would break the
    snapshot for every session.
    """
    assert pending_elicitations.snapshot_for("conv_nonexistent") == []


def test_pending_session_ids_tracks_publish_and_resolve() -> None:
    """
    The id list mirrors the index lifecycle exactly.

    ``GET /v1/sessions/{id}`` uses this list to decide whether the
    (DB-querying) descendant walk for child approval mirroring can be
    skipped. A stale id left after resolve would re-trigger the walk
    forever; a missing id would hide a child's pending prompt from
    ancestor snapshots.
    """
    # Empty index → empty list; this is the common steady state the
    # snapshot route uses to skip the descendant walk entirely.
    assert pending_elicitations.pending_session_ids() == []
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    pending_elicitations.record_publish("conv_b", _elicit_event("elicit_2"))
    assert sorted(pending_elicitations.pending_session_ids()) == ["conv_a", "conv_b"]
    pending_elicitations.resolve("conv_a", "elicit_1")
    # conv_a's only prompt resolved → its id must drop out, otherwise
    # every snapshot of conv_a's tree keeps paying the DB walk.
    assert pending_elicitations.pending_session_ids() == ["conv_b"]


def test_record_publish_ignores_tool_observations_for_pending_prompt() -> None:
    """
    Forwarded tool transcript events do not resolve pending prompts.

    Claude-native can publish a same-tool ``function_call`` before the
    PermissionRequest hook has returned. Treating that observation as a
    permission decision caused the hook to return ``deny``. The index
    should only clear on explicit id resolution or
    ``response.elicitation_resolved``.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_edit", "Edit"))
    pending_elicitations.record_publish(
        "conv_a",
        {
            "type": "function_call",
            "name": "Edit",
            "call_id": "toolu_edit_pre_permission",
        },
    )
    pending_elicitations.record_publish(
        "conv_a",
        {
            "type": "function_call_output",
            "call_id": "toolu_edit_pre_permission",
            "output": "ok",
        },
    )
    # Still 1: neither the same-tool call nor its result is an approval
    # verdict. If this becomes 0, transcript observation is again being
    # confused with permission resolution.
    assert pending_elicitations.count_for("conv_a") == 1


def test_project_for_peek_form_mode_surfaces_prompt_and_fields() -> None:
    """
    A form-mode elicitation projects to a compact item carrying the
    prompt text and the requested field names.

    This is what ``sys_session_get_history`` appends so a parent agent
    sees that a sub-agent is parked awaiting input, *and* what it's being
    asked. If ``fields`` is missing or ``prompt`` is ``None`` the
    parent learns the sub-agent is blocked but not on what — a weaker
    signal than the index actually holds.
    """
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_bio",
        "params": {
            "mode": "form",
            "message": "Answer 3 questions on human biology",
            "requestedSchema": {
                "type": "object",
                "properties": {"q1": {"type": "string"}, "q2": {"type": "string"}},
            },
        },
    }
    item = pending_elicitations.project_for_peek(event)
    # type discriminator distinguishes the synthetic item from the
    # message / function_call items in the same peek list.
    assert item["type"] == "pending_elicitation"
    assert item["elicitation_id"] == "elicit_bio"
    # prompt is the human-facing message — proves the params.message
    # made it through, not a None/empty placeholder.
    assert item["prompt"] == "Answer 3 questions on human biology"
    # fields lists the schema's property keys in order; a missing key
    # or wrong order means the schema walk regressed.
    assert item["fields"] == ["q1", "q2"]


@pytest.mark.parametrize(
    "params",
    [
        # url mode: no requestedSchema at all.
        {"mode": "url", "message": "Authorize at the link", "url": "https://x"},
        # form mode but the schema declares no properties.
        {"mode": "form", "message": "Confirm?", "requestedSchema": {"type": "object"}},
        # form mode with an empty properties dict.
        {"mode": "form", "message": "Confirm?", "requestedSchema": {"properties": {}}},
    ],
)
def test_project_for_peek_omits_fields_when_no_properties(params: dict[str, Any]) -> None:
    """
    When the elicitation declares no requested fields, ``fields`` is
    omitted entirely rather than emitted as an empty list.

    Keeps the peek item minimal: a ``"fields": []`` would suggest the
    sub-agent is asking for structured input when it isn't (url-mode
    OAuth, or a bare confirm). The prompt still surfaces so the parent
    knows the sub-agent is waiting.
    """
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_x",
        "params": params,
    }
    item = pending_elicitations.project_for_peek(event)
    assert item["type"] == "pending_elicitation"
    assert item["prompt"] == params["message"]
    # No fields key — not an empty list. ``"fields" in item`` being
    # True here means the empty-properties guard regressed.
    assert "fields" not in item


def test_project_for_peek_tolerates_missing_params() -> None:
    """
    An event with no ``params`` block projects to ``prompt=None`` and
    no ``fields``, without raising.

    The projector runs on the peek hot path over whatever the snapshot
    returned; a malformed/legacy payload must degrade to "blocked, no
    detail" rather than throwing and failing the whole peek.
    """
    item = pending_elicitations.project_for_peek(
        {"type": "response.elicitation_request", "elicitation_id": "elicit_bare"}
    )
    assert item == {
        "type": "pending_elicitation",
        "elicitation_id": "elicit_bare",
        "prompt": None,
    }


def test_set_elicitation_observer_runs_for_request_and_resolved() -> None:
    """
    A registered observer fires once per ``record_publish`` of a
    request or resolved event, with the same ``(conv_id, event)``
    arguments. Other event types are filtered out before the
    observer is consulted, so the wake notifier never sees noise
    from text deltas / status updates / unrelated server events.
    """
    seen: list[tuple[str, str | None]] = []

    def _observer(conv_id: str, event: dict[str, Any]) -> None:
        """Record the (conv, type) pair for each observed event."""
        seen.append((conv_id, event.get("type")))

    pending_elicitations.set_elicitation_observer(_observer)
    try:
        # Request fires once, resolved fires once, junk events do not.
        pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
        pending_elicitations.record_publish(
            "conv_a",
            {"type": "response.elicitation_resolved", "elicitation_id": "elicit_1"},
        )
        pending_elicitations.record_publish(
            "conv_a", {"type": "response.output_text.delta", "delta": "hi"}
        )
    finally:
        pending_elicitations.set_elicitation_observer(None)

    # 2 == one request + one resolved. If 3, the type filter let a
    # text delta through and the wake notifier would fire on every
    # streamed token. If 1, the resolved-event branch is skipping the
    # observer (observer is request-only) and the notifier would never
    # re-arm.
    assert seen == [
        ("conv_a", "response.elicitation_request"),
        ("conv_a", "response.elicitation_resolved"),
    ]


def test_set_elicitation_observer_none_clears_registration() -> None:
    """
    Passing ``None`` clears the prior observer; a later publish must
    not trigger the cleared callback.
    """
    received: list[str] = []

    def _observer(conv_id: str, event: dict[str, Any]) -> None:
        """Append every observed (conv_id) so we can assert call count."""
        received.append(conv_id)

    pending_elicitations.set_elicitation_observer(_observer)
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    # Confirms observer is wired before clearing — otherwise the
    # later assertion is vacuous.
    assert received == ["conv_a"]

    pending_elicitations.set_elicitation_observer(None)
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_2"))
    # Still one entry: the second publish bypassed the cleared
    # observer. A second entry here would mean teardown leaks across
    # Omnigent server lifespans (the lifespan callsite calls clear at
    # shutdown).
    assert received == ["conv_a"]


def test_reset_for_tests_clears_observer_registration() -> None:
    """
    ``reset_for_tests`` clears any registered observer.

    Otherwise a test that registered an observer and forgot to clear
    it would leak the callback into the next test, where it could
    fire against unrelated state.
    """
    received: list[str] = []

    def _observer(conv_id: str, event: dict[str, Any]) -> None:
        """Append every observed conv_id so we can assert call count."""
        received.append(conv_id)

    pending_elicitations.set_elicitation_observer(_observer)
    pending_elicitations.reset_for_tests()
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_1"))
    # Empty list: the observer was cleared by reset. If the
    # received list has an entry, a leaked observer from a prior
    # test would trigger spurious cross-test side effects.
    assert received == []


def test_lookup_returns_matching_elicitation() -> None:
    """
    :func:`lookup` returns ``(conversation_id, event)`` when the
    elicitation id is outstanding.

    The standalone approval page route uses this to render the prompt
    from the in-memory index without a database round-trip.
    """
    event = _elicit_event("elicit_xyz")
    pending_elicitations.record_publish("conv_a", event)
    result = pending_elicitations.lookup("elicit_xyz")
    assert result is not None
    conv_id, payload = result
    assert conv_id == "conv_a"
    assert payload["elicitation_id"] == "elicit_xyz"


def test_lookup_returns_none_for_unknown_id() -> None:
    """
    :func:`lookup` returns ``None`` for an id that was never tracked
    or has already been resolved, matching the 404-on-stale contract
    of the standalone approval page.
    """
    assert pending_elicitations.lookup("elicit_never") is None


def test_lookup_returns_none_after_resolve() -> None:
    """
    Once an elicitation is resolved, :func:`lookup` no longer finds it.

    The approval page should render a "resolved" message for stale ids.
    """
    pending_elicitations.record_publish("conv_a", _elicit_event("elicit_gone"))
    pending_elicitations.resolve("conv_a", "elicit_gone")
    assert pending_elicitations.lookup("elicit_gone") is None


def test_lookup_returns_deep_copy() -> None:
    """
    :func:`lookup` returns a deep copy of the stored event so callers
    cannot mutate the index.
    """
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_copy",
        "params": {"message": "original"},
    }
    pending_elicitations.record_publish("conv_a", event)
    result = pending_elicitations.lookup("elicit_copy")
    assert result is not None
    _, payload = result
    payload["params"]["message"] = "tampered"
    # Re-lookup must still see the original.
    result2 = pending_elicitations.lookup("elicit_copy")
    assert result2 is not None
    assert result2[1]["params"]["message"] == "original"


def test_snapshot_for_returns_independent_copies() -> None:
    """
    Mutating a snapshot entry must not poison the internal index,
    even at nested depths.

    The index stores arbitrary event dicts whose ``params`` block
    is itself a dict. A shallow copy would leak nested
    mutations — e.g. ``snap[0]["params"]["message"] = "x"`` would
    mutate the index's stored event. ``deepcopy`` is required.
    """
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_mutate",
        "params": {"message": "original"},
    }
    pending_elicitations.record_publish("conv_a", event)
    snap1 = pending_elicitations.snapshot_for("conv_a")
    # Top-level reassignment — caught by a shallow copy.
    snap1[0]["params"] = {"message": "tampered-top-level"}
    # Nested in-place mutation — only caught by deep copy. This
    # is the exact pattern the review comment flagged: shallow
    # ``dict(event)`` would let this corrupt the index.
    snap1_again = pending_elicitations.snapshot_for("conv_a")
    snap1_again[0]["params"]["message"] = "tampered-nested"
    snap2 = pending_elicitations.snapshot_for("conv_a")
    # The third read must still reflect the original event, not
    # either tampered version. If "tampered-nested", the snapshot
    # returned a shared reference and external mutation corrupted
    # the index; if "tampered-top-level", the top-level copy is
    # there but nested values are still shared.
    assert snap2[0]["params"]["message"] == "original"
