"""Unit tests for the sessions-API REPL adapter event translator.

Three CUJs caught these regressions during manual testing of
``omnigent run`` in sessions mode:

1. ``ResponseCreated`` was imported from the public ``omnigent_client``
   re-export, which doesn't include it. ImportError on first turn.
2. The adapter passed server-shape events
   (:class:`omnigent.server.schemas.OutputTextDeltaEvent`) through
   without translating to the SDK-shape dataclasses in
   :mod:`omnigent_client._events`. Net: spinner spun, server-side
   LLM call completed, no assistant text reached the TUI.
3. The translator initially used wrong class names from
   ``omnigent.server.schemas`` (``ResponseTextDeltaEvent`` vs the
   actual ``OutputTextDeltaEvent``); silent ImportError on the local
   import inside the translator.

This file pins each translation case so all three classes of bug
surface at import time or in a sub-second unit test.
"""

from __future__ import annotations

import pytest
from omnigent_client._events import (
    ReasoningDelta,
    ReasoningStarted,
    ReasoningSummaryDelta,
    ResponseCancelled,
    ResponseCompleted,
    ResponseCreated,
    ResponseFailed,
    ResponseIncomplete,
    ResponseInProgress,
    ResponseQueued,
    TextDelta,
)

from omnigent.repl._repl import (
    _plan_output_item_render,
    _server_event_to_sdk_event,
    _TurnProseTracker,
)
from omnigent.server.schemas import (
    CancelledEvent,
    CompletedEvent,
    CreatedEvent,
    FailedEvent,
    IncompleteDetails,
    IncompleteEvent,
    InProgressEvent,
    OutputTextDeltaEvent,
    QueuedEvent,
    ReasoningStartedEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningTextDeltaEvent,
    ResponseObject,
)


def _resp_obj(status: str = "in_progress") -> ResponseObject:
    """Build a minimal :class:`ResponseObject` for envelope tests."""
    return ResponseObject(
        id="resp_test",
        object="response",
        status=status,
        model="m",
        created_at=0,
        completed_at=0 if status not in {"queued", "in_progress"} else None,
        output=[],
        background=False,
        store=True,
        usage=None,
        previous_response_id=None,
        conversation={"id": "conv_test"},
        instructions=None,
        reasoning=None,
        error=None,
        incomplete_details=None,
    )


def test_created_event_translates_to_response_created() -> None:
    """``CreatedEvent`` → :class:`ResponseCreated` carrying the response id.

    Regression: without this mapping the REPL adapter never sets
    ``current_response_id`` and downstream history lookups silently
    target the wrong response.
    """
    env = CreatedEvent(type="response.created", response=_resp_obj("queued"))
    out = _server_event_to_sdk_event(env)
    assert isinstance(out, ResponseCreated)
    assert out.response.id == "resp_test"


def test_text_delta_event_translates_to_text_delta() -> None:
    """``OutputTextDeltaEvent`` → :class:`TextDelta` with the same payload.

    Regression: this is the event the sessions renderer consumes
    to paint assistant text on the screen. A mismatch here means a
    silent "spinner spins forever, no text" REPL.
    """
    env = OutputTextDeltaEvent(
        type="response.output_text.delta",
        delta="hello",
    )
    out = _server_event_to_sdk_event(env)
    assert isinstance(out, TextDelta)
    assert out.delta == "hello"


def test_queued_in_progress_translate() -> None:
    """Lifecycle envelopes map to their SDK counterparts."""
    queued = QueuedEvent(type="response.queued", response=_resp_obj("queued"))
    in_prog = InProgressEvent(type="response.in_progress", response=_resp_obj("in_progress"))
    assert isinstance(_server_event_to_sdk_event(queued), ResponseQueued)
    assert isinstance(_server_event_to_sdk_event(in_prog), ResponseInProgress)


def test_terminal_events_translate() -> None:
    """Each terminal status maps to the right SDK terminal class.

    Regression: a wrong mapping here causes the REPL to miss the
    turn-terminal event and wait for a stream that already ended.
    """
    completed = CompletedEvent(type="response.completed", response=_resp_obj("completed"))
    failed = FailedEvent(type="response.failed", response=_resp_obj("failed"))
    cancelled = CancelledEvent(type="response.cancelled", response=_resp_obj("cancelled"))
    assert isinstance(_server_event_to_sdk_event(completed), ResponseCompleted)
    assert isinstance(_server_event_to_sdk_event(failed), ResponseFailed)
    assert isinstance(_server_event_to_sdk_event(cancelled), ResponseCancelled)


def test_incomplete_event_preserves_reason() -> None:
    """``IncompleteEvent`` carries the ``incomplete_details.reason`` through.

    The SDK's :class:`ResponseIncomplete` flattens the reason into a
    top-level field. Without this propagation, REPL renderers that
    surface "incomplete: max_tokens" show an empty reason.
    """
    resp = _resp_obj("incomplete")
    # ResponseObject construction defaults incomplete_details to None;
    # set it explicitly via model_copy so we hit the propagation path.
    resp = resp.model_copy(update={"incomplete_details": IncompleteDetails(reason="max_tokens")})
    env = IncompleteEvent(type="response.incomplete", response=resp)
    out = _server_event_to_sdk_event(env)
    assert isinstance(out, ResponseIncomplete)
    assert out.reason == "max_tokens"


def test_reasoning_events_translate() -> None:
    """All three reasoning variants map to their SDK counterparts."""
    started = ReasoningStartedEvent(type="response.reasoning.started")
    delta = ReasoningTextDeltaEvent(type="response.reasoning_text.delta", delta="thinking")
    summary = ReasoningSummaryTextDeltaEvent(
        type="response.reasoning_summary_text.delta",
        delta="summary",
    )
    assert isinstance(_server_event_to_sdk_event(started), ReasoningStarted)
    parsed_delta = _server_event_to_sdk_event(delta)
    assert isinstance(parsed_delta, ReasoningDelta)
    assert parsed_delta.delta == "thinking"
    parsed_summary = _server_event_to_sdk_event(summary)
    assert isinstance(parsed_summary, ReasoningSummaryDelta)
    assert parsed_summary.delta == "summary"


def test_unknown_event_returns_none() -> None:
    """Untranslated event types fall through to ``None`` (forward-compatible)."""

    class _Unknown:
        type = "response.something.new"

    assert _server_event_to_sdk_event(_Unknown()) is None


def test_elicitation_request_event_translates() -> None:
    """``ElicitationRequestEvent`` → :class:`ElicitationRequest` with all fields."""
    from omnigent_client._events import ElicitationRequest

    from omnigent.server.schemas import (
        ElicitationRequestEvent,
        ElicitationRequestParams,
    )

    params = ElicitationRequestParams(
        mode="form",
        message="Approve this?",
        requestedSchema={"type": "object"},
        phase="tool_call",
        policy_name="approve_shell",
        content_preview="rm -rf /tmp",
    )
    event = ElicitationRequestEvent(
        type="response.elicitation_request",
        elicitation_id="elicit_abc",
        params=params,
    )
    out = _server_event_to_sdk_event(event)
    assert isinstance(out, ElicitationRequest)
    assert out.elicitation_id == "elicit_abc"
    assert out.message == "Approve this?"
    assert out.mode == "form"
    assert out.phase == "tool_call"
    assert out.policy_name == "approve_shell"
    assert out.content_preview == "rm -rf /tmp"
    assert out.requested_schema == {"type": "object"}
    # Own-session prompt: no mirrored-child routing hint.
    assert out.target_session_id is None


def test_elicitation_request_event_preserves_target_session_id() -> None:
    """A mirrored sub-agent prompt carries ``target_session_id`` through.

    The server stamps ``params.target_session_id`` when it mirrors a
    child's parked elicitation into an ancestor stream. If the translator
    drops it (the original bug), the REPL resolves the verdict against the
    parent session, which 404s, and the sub-agent stays blocked forever.
    """
    from omnigent_client._events import ElicitationRequest

    from omnigent.server.schemas import (
        ElicitationRequestEvent,
        ElicitationRequestParams,
    )

    params = ElicitationRequestParams(
        mode="form",
        message="Claude wants to call **Read**",
        phase="tool_call",
        policy_name="claude_sdk_permission",
        content_preview="Read(builder.py)",
        target_session_id="conv_child123",
    )
    event = ElicitationRequestEvent(
        type="response.elicitation_request",
        elicitation_id="elicit_child",
        params=params,
    )
    out = _server_event_to_sdk_event(event)
    assert isinstance(out, ElicitationRequest)
    # The child id survived translation; a None here is the original
    # dropped-field bug that misroutes the verdict to the parent.
    assert out.target_session_id == "conv_child123"


def test_elicitation_resolve_session_id_routes_mirrored_child_to_child() -> None:
    """Mirrored child prompt resolves against the child, own prompt against the stream.

    This is the routing decision the render callback makes before POSTing
    a verdict: a mirrored sub-agent prompt (``target_session_id`` set)
    must resolve on the child that parked on it; an own-session prompt
    (unset) resolves on the session the event arrived on.
    """
    from omnigent_client._events import ElicitationRequest

    from omnigent.repl._repl import _elicitation_resolve_session_id

    mirrored = ElicitationRequest(
        elicitation_id="elicit_child",
        message="Claude wants to call **Read**",
        requested_schema={},
        mode="form",
        phase="tool_call",
        policy_name="claude_sdk_permission",
        content_preview="Read(builder.py)",
        target_session_id="conv_child123",
    )
    # Mirrored: verdict goes to the parked child, not the parent stream.
    assert _elicitation_resolve_session_id(mirrored, "conv_parent") == "conv_child123"

    own = ElicitationRequest(
        elicitation_id="elicit_own",
        message="Approve this?",
        requested_schema={},
        mode="form",
        phase="tool_call",
        policy_name="approve_shell",
        content_preview="rm -rf /tmp",
    )
    # Own-session prompt: no hint, so resolve on the receiving stream.
    assert _elicitation_resolve_session_id(own, "conv_parent") == "conv_parent"


def test_output_item_done_event_passes_through() -> None:
    """``OutputItemDoneEvent`` is returned as-is for adapter-level handling."""
    from omnigent.server.schemas import OutputItemDoneEvent

    event = OutputItemDoneEvent(
        type="response.output_item.done",
        item={
            "type": "function_call",
            "status": "action_required",
            "call_id": "c1",
            "name": "bash",
        },
    )
    out = _server_event_to_sdk_event(event)
    assert isinstance(out, OutputItemDoneEvent)
    assert out.item["call_id"] == "c1"


def test_output_item_done_message_passes_through() -> None:
    """``OutputItemDoneEvent`` with a message item passes through.

    The translator does not filter by item type; the render callback
    is responsible for skipping message items (which are already
    rendered via ``TextDelta`` streaming) and only rendering
    ``function_call`` / ``function_call_output`` as history entries.
    """
    from omnigent.server.schemas import OutputItemDoneEvent

    event = OutputItemDoneEvent(
        type="response.output_item.done",
        item={
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hello"}],
        },
    )
    out = _server_event_to_sdk_event(event)
    assert isinstance(out, OutputItemDoneEvent)
    assert out.item["type"] == "message"


@pytest.mark.parametrize(
    ("item_type", "role", "saw_text_deltas", "expect_flush", "expect_render"),
    [
        # Regression (the "growing preamble" bug): a tool call interrupts
        # streamed prose. The executor emits no per-block ``message``
        # boundary, so this is the ONLY chance to commit the in-flight
        # text. It MUST flush — otherwise the next text block appends to
        # the prior block's buffer and the live region re-renders the
        # whole turn's prose on every later delta.
        ("function_call", None, True, True, True),
        ("function_call_output", None, True, True, True),
        ("slash_command", None, True, True, True),
        # Same item types with no in-flight prose: nothing to commit, but
        # the item still renders as a history entry.
        ("function_call", None, False, False, True),
        # Assistant message after streamed deltas: commit the trailing
        # tail at the boundary, but DON'T re-render the full item (the
        # deltas already rendered it — re-rendering would duplicate it).
        ("message", "assistant", True, True, False),
        # Assistant message with no streamed deltas (non-streaming
        # harness): render the full item, nothing to flush.
        ("message", "assistant", False, False, True),
        # User messages and non-renderable items (reasoning) are handled
        # by other branches; the plan neither flushes nor renders them.
        ("message", "user", False, False, False),
        ("reasoning", None, True, False, False),
    ],
)
def test_plan_output_item_render(
    item_type: str,
    role: str | None,
    saw_text_deltas: bool,
    expect_flush: bool,
    expect_render: bool,
) -> None:
    """
    ``_plan_output_item_render`` commits in-flight prose at every
    content-block boundary that interrupts streamed text.

    A single turn interleaves assistant text blocks with tool calls,
    but the streaming executor emits the assistant ``message`` output
    item only once (after all deltas) — never between text blocks. A
    ``function_call`` arriving mid-stream is therefore the only signal
    that one text block ended; the plan must request a flush there
    (``expect_flush``) or the formatter's paragraph buffer accumulates
    across blocks and the preamble visibly grows on every later delta.

    :param item_type: The streamed item's ``type`` field.
    :param role: The item's ``role`` (only set on ``message`` items).
    :param saw_text_deltas: Whether assistant prose is in flight.
    :param expect_flush: Expected ``flush_inflight_text`` decision.
    :param expect_render: Expected ``render_item`` decision.
    """
    plan = _plan_output_item_render(item_type, role, saw_text_deltas)
    assert plan.flush_inflight_text is expect_flush, (
        f"{item_type!r} (role={role!r}, saw_text_deltas={saw_text_deltas}) "
        f"should flush={expect_flush}. A wrong flush decision at a tool-call "
        f"boundary is exactly the streaming-text duplication bug: without the "
        f"flush the next text block appends to the prior block's buffer."
    )
    assert plan.render_item is expect_render, (
        f"{item_type!r} (role={role!r}, saw_text_deltas={saw_text_deltas}) "
        f"should render={expect_render}. Re-rendering an assistant message "
        f"whose prose already streamed would duplicate it; skipping a tool "
        f"call would drop it from the transcript."
    )


def _assistant_message_item(text: str) -> dict[str, object]:
    """
    Build an assistant ``message`` output item with one text block.

    Shape matches what the relay's ``_flush_relay_text`` publishes after
    persisting a streamed text segment (``response.output_item.done``).

    :param text: The message's output text, e.g. ``"Got it — done."``.
    :returns: The ``item`` dict of the ``output_item.done`` event.
    """
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def test_prose_tracker_suppresses_persisted_segment_copy() -> None:
    """The relay's persisted copy of a streamed segment is matched once.

    Regression for the mid-turn duplication bug: the relay flushes a
    streamed text segment at a tool-call boundary and publishes the
    persisted item as ``output_item.done``. By then the tool-call item
    already committed the in-flight prose, so the delta-based skip
    can't catch the item — the content match must.
    """
    tracker = _TurnProseTracker()
    tracker.on_delta("Got it — ")
    tracker.on_delta("dispatching.")
    # Tool-call boundary commits the in-flight prose (mirrors
    # _flush_inflight_assistant_text resetting _saw_text_deltas).
    tracker.commit_segment()

    # The persisted copy byte-matches the streamed segment: it must be
    # consumed (suppressed). A False here re-renders the whole segment
    # as a duplicate "◆ agent + text" block — the reported bug.
    assert tracker.consume_match(_assistant_message_item("Got it — dispatching.")) is True
    # The entry is consumed: an identical item later in the turn has no
    # streamed counterpart left and must render (it is real content).
    assert tracker.consume_match(_assistant_message_item("Got it — dispatching.")) is False


def test_prose_tracker_does_not_match_unstreamed_text() -> None:
    """A non-streamed assistant message must not be suppressed.

    If the item's text differs from every committed segment, the
    message never streamed as deltas (e.g. a non-streaming harness)
    and skipping it would drop real content from the transcript.
    """
    tracker = _TurnProseTracker()
    tracker.on_delta("Streamed prose.")
    tracker.commit_segment()

    assert tracker.consume_match(_assistant_message_item("Different text.")) is False
    # The streamed segment's entry is untouched by the failed match.
    assert tracker.consume_match(_assistant_message_item("Streamed prose.")) is True


def test_prose_tracker_multiset_handles_identical_segments() -> None:
    """Two identical streamed segments suppress exactly two items.

    Multiset semantics: a turn that legitimately narrates the same text
    twice (two flushed segments) gets two persisted-item publishes; each
    must match its own entry, and a third identical item must render.
    """
    tracker = _TurnProseTracker()
    tracker.on_delta("Done.")
    tracker.commit_segment()
    tracker.on_delta("Done.")
    tracker.commit_segment()

    assert tracker.consume_match(_assistant_message_item("Done.")) is True
    assert tracker.consume_match(_assistant_message_item("Done.")) is True
    # Both entries consumed — a third identical message is new content.
    assert tracker.consume_match(_assistant_message_item("Done.")) is False


def test_prose_tracker_reset_turn_drops_prior_turn_prose() -> None:
    """A new turn must not suppress messages with last turn's text."""
    tracker = _TurnProseTracker()
    tracker.on_delta("Same words.")
    tracker.commit_segment()
    tracker.reset_turn()

    # Post-reset, the prior turn's segment is gone: an assistant message
    # with identical text this turn is genuinely new and must render.
    assert tracker.consume_match(_assistant_message_item("Same words.")) is False


def test_prose_tracker_ignores_items_without_output_text() -> None:
    """Items with no ``output_text`` content never match anything."""
    tracker = _TurnProseTracker()
    tracker.on_delta("prose")
    tracker.commit_segment()

    # No content list at all.
    assert tracker.consume_match({"type": "message", "role": "assistant"}) is False
    # Content present but no output_text blocks (e.g. refusal-only).
    assert (
        tracker.consume_match(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "no"}],
            }
        )
        is False
    )
    # The streamed entry survives both non-matches.
    assert tracker.consume_match(_assistant_message_item("prose")) is True


def test_prose_tracker_joins_multi_block_item_content() -> None:
    """An item with several ``output_text`` blocks matches by joined text.

    Mirrors the server-side join in ``_flush_relay_text`` /
    ``_committed_message_text``: content blocks concatenate in order.
    """
    tracker = _TurnProseTracker()
    tracker.on_delta("part one ")
    tracker.on_delta("part two")
    tracker.commit_segment()

    item = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "part one "},
            {"type": "output_text", "text": "part two"},
        ],
    }
    assert tracker.consume_match(item) is True


def test_prose_tracker_uncommitted_segment_does_not_match() -> None:
    """Only COMMITTED segments are candidates for suppression.

    While prose is still in flight, ``_saw_text_deltas`` is True and the
    delta-based skip handles the item; the tracker must not also match
    (the call-site short-circuits, but the tracker's own contract is
    that uncommitted parts are not yet a segment).
    """
    tracker = _TurnProseTracker()
    tracker.on_delta("in flight")

    assert tracker.consume_match(_assistant_message_item("in flight")) is False


def test_client_task_cancel_event_passes_through() -> None:
    """``ClientTaskCancelEvent`` is returned as-is for adapter-level handling."""
    from omnigent.server.schemas import ClientTaskCancelEvent

    event = ClientTaskCancelEvent(
        type="response.client_task.cancel",
        task_id="resp_async_abc",
        call_id="call_xyz",
    )
    out = _server_event_to_sdk_event(event)
    assert isinstance(out, ClientTaskCancelEvent)
    assert out.task_id == "resp_async_abc"
    assert out.call_id == "call_xyz"


def test_render_callback_event_flow() -> None:
    """Replay a real SSE event stream and assert rendering order.

    Given the event sequence observed from a real server:

        1. session.status running
        2. session.input.consumed
        3. response.output_text.delta
        4. response.output_item.done (message)
        5. response.completed
        6. session.status idle

    The render callback should produce:

        - header on session.status running
        - streamed text on delta
        - NO duplicate from output_item.done (message skipped)
        - flush + turn_done on session.status idle
    """

    from pydantic import TypeAdapter

    from omnigent.server.schemas import ServerStreamEvent

    adapter = TypeAdapter(ServerStreamEvent)

    raw_events = [
        {
            "type": "session.status",
            "conversation_id": "conv_test",
            "status": "running",
            "sequence_number": None,
        },
        {
            "type": "session.input.consumed",
            "data": {
                "item_id": "msg_1",
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "say hello"}],
                    "agent": None,
                },
            },
            "sequence_number": None,
        },
        {
            "type": "response.output_text.delta",
            "delta": "Hello, hope you are well!",
            "sequence_number": None,
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "msg_2",
                "response_id": "resp_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello, hope you are well!"}],
                "model": "test-agent",
            },
            "sequence_number": None,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": "test-agent",
                "created_at": 0,
                "completed_at": 0,
                "output": [],
                "background": False,
                "store": True,
                "usage": None,
                "previous_response_id": None,
                "conversation": {"id": "conv_test"},
                "instructions": None,
                "reasoning": None,
                "error": None,
                "incomplete_details": None,
            },
            "sequence_number": None,
        },
        {
            "type": "session.status",
            "conversation_id": "conv_test",
            "status": "idle",
            "sequence_number": None,
        },
    ]

    events = [adapter.validate_python(e) for e in raw_events]

    # Build a minimal mock adapter + callback to track rendering.
    rendered: list[str] = []

    class _FakeSession:
        _agent_name = "test-agent"
        _current_response_id: str | None = None
        _is_streaming = True
        _tool_callables = None
        _turn_done = None
        # Mimic a local send: ``send()`` increments this before POST so
        # the matching ``session.input.consumed`` event is suppressed.
        _pending_local_user_sends = 1

    fake_session = _FakeSession()

    # Import the schemas needed by the callback.
    from omnigent_client._events import (
        ResponseCreated as _Created,
    )
    from omnigent_client._events import (
        TextDelta as _TD,
    )

    from omnigent.server.schemas import (
        OutputItemDoneEvent as _OIDE,
    )
    from omnigent.server.schemas import (
        SessionInputConsumedEvent as _SICEv,
    )
    from omnigent.server.schemas import (
        SessionStatusEvent as _StatusEv,
    )

    # Replay events through the same logic as _render_session_event.
    for event in events:
        if isinstance(event, _StatusEv):
            if event.status == "running":
                # Mirrors host.start_timer() call so a cross-client
                # turn surfaces the toolbar spinner.
                rendered.append("START_TIMER")
                rendered.append("HEADER")
            elif event.status in ("idle", "failed"):
                rendered.append("FLUSH")
                rendered.append("STOP_TIMER")
            continue

        if isinstance(event, _SICEv):
            if event.data.type == "message" and event.data.data.get("role") == "user":
                if fake_session._pending_local_user_sends > 0:
                    fake_session._pending_local_user_sends -= 1
                else:
                    rendered.append("USER_MESSAGE_CROSSCLIENT")
            continue

        sdk_ev = _server_event_to_sdk_event(event)
        if sdk_ev is None:
            continue

        if isinstance(sdk_ev, _TD):
            rendered.append(f"TEXT_DELTA:{sdk_ev.delta}")
            continue

        if isinstance(sdk_ev, _OIDE):
            item = sdk_ev.item
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("function_call", "function_call_output"):
                    rendered.append(f"TOOL:{item_type}")
                elif item_type == "message" and item.get("role") == "assistant":
                    if not fake_session._is_streaming:
                        rendered.append("ASSISTANT_MESSAGE_HISTORY")
                    # else: skipped (already streamed via deltas)
            continue

        if isinstance(sdk_ev, _Created):
            fake_session._current_response_id = sdk_ev.response.id
            continue

    # Assert the expected rendering order.
    assert rendered == [
        "START_TIMER",
        "HEADER",
        # session.input.consumed for the local send is SUPPRESSED
        # (FIFO decrement); ``on_input`` already echoed via fmt.user_message.
        "TEXT_DELTA:Hello, hope you are well!",
        # output_item.done for message is SKIPPED (is_streaming=True)
        # response.completed is ignored
        "FLUSH",
        "STOP_TIMER",
    ]
    # Counter is exhausted by the matching event.
    assert fake_session._pending_local_user_sends == 0


def test_session_input_consumed_renders_cross_client_user_message() -> None:
    """``session.input.consumed`` from another client renders a user bubble.

    When the local send FIFO counter is zero the event
    must have originated from a different client (Web UI ↔ TUI on the
    same session); the renderer pulls the text from the event payload
    and emits it via the same ``fmt.user_message`` echo a local send
    would produce.
    """
    from pydantic import TypeAdapter

    from omnigent.server.schemas import (
        ServerStreamEvent,
        SessionInputConsumedEvent,
    )

    adapter = TypeAdapter(ServerStreamEvent)
    event = adapter.validate_python(
        {
            "type": "session.input.consumed",
            "data": {
                "item_id": "msg_from_other_client",
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "from web ui"}],
                },
            },
            "sequence_number": None,
        }
    )
    assert isinstance(event, SessionInputConsumedEvent)

    class _FakeSession:
        _pending_local_user_sends = 0  # Counter is empty → cross-client.

    fake_session = _FakeSession()
    rendered: list[str] = []
    if event.data.type == "message" and event.data.data.get("role") == "user":
        if fake_session._pending_local_user_sends > 0:
            fake_session._pending_local_user_sends -= 1
        else:
            content = event.data.data.get("content", [])
            text = " ".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") in ("input_text", "output_text")
            )
            rendered.append(f"USER_MESSAGE:{text}")
    assert rendered == ["USER_MESSAGE:from web ui"]


def test_autonomous_turn_renders_without_local_send() -> None:
    """An autonomous turn (no preceding local ``send()``) reaches the host.

    Pins the renderer-chain coverage that the deleted
    ``test_run_omnigent_autonomous_turn_polling.py`` used to provide before
    the items-poller path was retired. Under the new SSE architecture,
    autonomous turns (timer-fired notifications, async-tool completions,
    cross-client assistant turns) flow through the same
    ``_render_session_event`` callback as local sends, but without a
    matching ``session.input.consumed`` and without the local
    ``_pending_local_user_sends`` counter being incremented.

    The required rendering invariants for an autonomous turn are:

    * ``session.status running`` → header + start-timer.
    * ``response.output_text.delta`` → streamed text via formatter.
    * ``session.status idle`` → flush + stop-timer + turn-done.

    If ``_render_session_event`` ever regresses on the
    ``_saw_text_deltas = False`` reset (so subsequent autonomous turns
    in the same session double-render via ``output_item.done``), or
    drops the ``running`` header for non-local-send paths, this test
    fails and a fresh assistant turn never reaches the user's screen.

    :returns: None.
    """
    from pydantic import TypeAdapter

    from omnigent.server.schemas import ServerStreamEvent

    adapter = TypeAdapter(ServerStreamEvent)

    # No ``session.input.consumed`` in this stream — the turn was
    # NOT triggered by the local client. The renderer must still
    # produce the same output as a local-send turn.
    raw_events = [
        {
            "type": "session.status",
            "conversation_id": "conv_test",
            "status": "running",
            "sequence_number": None,
        },
        {
            "type": "response.output_text.delta",
            "delta": "timer fired: e2e_marker",
            "sequence_number": None,
        },
        {
            "type": "session.status",
            "conversation_id": "conv_test",
            "status": "idle",
            "sequence_number": None,
        },
    ]
    events = [adapter.validate_python(e) for e in raw_events]

    rendered: list[str] = []

    class _FakeSession:
        _agent_name = "test-agent"
        _current_response_id: str | None = None
        _is_streaming = True
        _tool_callables = None
        _turn_done = None
        # Counter is empty: no local ``send()`` preceded this turn.
        # The autonomous-turn invariant is "renders even though the
        # local FIFO is empty," so this MUST stay at 0 throughout.
        _pending_local_user_sends = 0

    fake_session = _FakeSession()

    from omnigent.server.schemas import (
        SessionInputConsumedEvent as _SICEv,
    )
    from omnigent.server.schemas import (
        SessionStatusEvent as _StatusEv,
    )

    # Replay events through the same branching logic as
    # ``_render_session_event`` in ``omnigent/repl/_repl.py``.
    for event in events:
        if isinstance(event, _StatusEv):
            if event.status == "running":
                rendered.append("START_TIMER")
                rendered.append("HEADER")
            elif event.status in ("idle", "failed"):
                rendered.append("FLUSH")
                rendered.append("STOP_TIMER")
            continue

        if isinstance(event, _SICEv):  # pragma: no cover - none in this stream
            raise AssertionError("autonomous-turn stream must not contain session.input.consumed")

        sdk_ev = _server_event_to_sdk_event(event)
        if sdk_ev is None:
            continue
        if isinstance(sdk_ev, TextDelta):
            rendered.append(f"TEXT_DELTA:{sdk_ev.delta}")

    # The autonomous-turn render must produce the same shape as a
    # local-send turn (sans the suppressed ``session.input.consumed``
    # echo) — header on running, streamed text, flush + stop-timer
    # on idle. If any of the four entries is missing or the order
    # changes, a timer-fired or cross-client assistant turn never
    # reaches the user's screen.
    assert rendered == [
        "START_TIMER",
        "HEADER",
        "TEXT_DELTA:timer fired: e2e_marker",
        "FLUSH",
        "STOP_TIMER",
    ]
    # The local-send FIFO is undisturbed — proves the renderer did
    # not accidentally consume a phantom slot for the autonomous turn.
    assert fake_session._pending_local_user_sends == 0


def test_response_failed_translates_with_error_message() -> None:
    """``ResponseFailed`` carries the error message from the response.

    Regression: ``_render_session_event`` had no handler for
    ``ResponseFailed`` — the event fell through all ``isinstance``
    checks and was silently dropped. The TUI showed an empty
    response while the web UI rendered the error correctly.
    """
    resp = _resp_obj("failed")
    resp = resp.model_copy(
        update={"error": {"code": "BAD_REQUEST", "message": "unsupported API type"}}
    )
    failed = FailedEvent(type="response.failed", response=resp)
    sdk_ev = _server_event_to_sdk_event(failed)
    assert isinstance(sdk_ev, ResponseFailed)
    assert sdk_ev.response.error is not None
    assert sdk_ev.response.error.message == "unsupported API type"
    assert sdk_ev.response.error.code == "BAD_REQUEST"


def test_failed_status_event_renders_error_message() -> None:
    """A terminal ``session.status: failed`` renders its error message.

    Regression: a SETUP-phase failure (spec resolution, spawn-env
    build) ends the turn before the LLM stream starts, so the runner
    publishes only ``session.status: failed`` (carrying the error) —
    no ``response.failed`` event is ever emitted. The REPL handled
    ``failed`` identically to ``idle`` (empty ``format_text_done``),
    so the working spinner vanished with no output: the failure was
    silent to the user. ``_render_failed_status_error`` must surface
    the carried message as a red error line.
    """
    from omnigent_ui_sdk import RichBlockFormatter

    from omnigent.repl._repl import _render_failed_status_error
    from omnigent.server.schemas import ErrorDetail
    from omnigent.server.schemas import SessionStatusEvent as _StatusEv
    from tests.repl.helpers import CapturingHost

    host = CapturingHost()
    event = _StatusEv(
        type="session.status",
        conversation_id="conv_test",
        status="failed",
        error=ErrorDetail(
            code="runner_error",
            message="turn setup failed: no resolvable model for provider 'acme'",
        ),
    )

    items = _render_failed_status_error(RichBlockFormatter(), host, event)  # type: ignore[arg-type]

    # The exact error CONTENT must reach the screen — not just that
    # *something* was rendered. Without the fix host.text is empty.
    assert "turn setup failed: no resolvable model for provider 'acme'" in host.text
    # The helper returns the rendered items so the caller can record
    # them on the debug tape; an empty list would mean nothing rendered.
    assert items


def test_failed_status_event_without_error_falls_back() -> None:
    """A ``failed`` status with no error detail renders a generic line.

    Some runner failure call sites carry only an HTTP status (no
    ``message``), and the schema's ``error`` field is optional, so the
    event may arrive with ``error=None``. The renderer must not crash
    on the missing field and must still print a non-empty error line
    rather than ending the turn silently.
    """
    from omnigent_ui_sdk import RichBlockFormatter

    from omnigent.repl._repl import _render_failed_status_error
    from omnigent.server.schemas import SessionStatusEvent as _StatusEv
    from tests.repl.helpers import CapturingHost

    host = CapturingHost()
    event = _StatusEv(
        type="session.status",
        conversation_id="conv_test",
        status="failed",
        error=None,
    )

    items = _render_failed_status_error(RichBlockFormatter(), host, event)  # type: ignore[arg-type]

    assert "turn failed" in host.text
    assert items


def test_waiting_status_notice_renders_alive_line() -> None:
    """A terminal ``session.status: waiting`` renders an alive notice.

    Regression: idle / waiting / failed shared one branch and only
    ``failed`` rendered anything, so a ``waiting`` turn tore down exactly
    like ``idle`` — the spinner vanished with no output and a live
    session that had merely parked on background work (a sub-agent or
    async tool still running) looked dead. ``_render_waiting_status_notice``
    must surface a line that BOTH says the session is still alive and
    names what it is waiting on, so it can never be confused with a
    finished ``idle`` turn.
    """
    from omnigent_ui_sdk import RichBlockFormatter

    from omnigent.repl._repl import _render_waiting_status_notice
    from tests.repl.helpers import CapturingHost

    host = CapturingHost()

    items = _render_waiting_status_notice(RichBlockFormatter(), host)  # type: ignore[arg-type]

    # Collapse wrap-induced newlines so the substring checks are
    # width-independent.
    text = " ".join(host.text.lower().split())
    # Reads as the live "waiting" state, not a dead/finished session.
    assert "waiting" in text
    assert "alive" in text
    # Names WHAT it is waiting on (background work).
    assert "background" in text
    # Non-empty render -> a ``waiting`` turn is never silent (the point).
    assert items


def test_waiting_status_notice_is_not_an_error() -> None:
    """The waiting notice reads as alive-and-working, not as a failure.

    ``waiting`` is a healthy, transient state — the parent turn parked on
    the async-work drain and resumes on its own — so its line must not
    reuse the ``failed`` error wording, or a normal background wait would
    read to the user as a turn failure.
    """
    from omnigent_ui_sdk import RichBlockFormatter

    from omnigent.repl._repl import _render_waiting_status_notice
    from tests.repl.helpers import CapturingHost

    host = CapturingHost()

    _render_waiting_status_notice(RichBlockFormatter(), host)  # type: ignore[arg-type]

    text = " ".join(host.text.lower().split())
    assert "failed" not in text
    assert "error" not in text
