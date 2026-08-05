"""Tests for the pure RPC step→item mapper.

These exercise :func:`omnigent.antigravity_native_steps.map_step_to_events`
using the real recorded fixtures captured from live agy sessions — from BOTH
RPC shapes, since they differ (``stream_*`` fixtures are verbatim
``StreamAgentStateUpdates`` frames, the rest are ``GetCascadeTrajectorySteps``
snapshots). No I/O, no live agy: the mapper is driven with fixture dicts and
event shapes are asserted exactly.

Key assertions:
- PLANNER_RESPONSE with text → exactly one ``external_conversation_item``
  ``message`` (role assistant, ``output_text`` content). NO
  ``external_output_text_delta`` / ``output_text_delta`` event.
- USER_INPUT → ``[]`` (skipped — fixes user-dup).
- PLANNER_RESPONSE with tool_calls → ``[]``; the tool step owns the pair.
- A tool step at terminal status → its ``function_call`` AND the matching
  ``function_call_output``, sharing a step-derived call id, on either shape.
- RUN_COMMAND / ASK_QUESTION WAITING → ``[]`` (no result yet).
- CHECKPOINT / CONVERSATION_HISTORY → ``[]``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

from omnigent.antigravity_native_steps import (
    OutboundEvent,
    _execution_discriminator,
    map_step_to_events,
    output_reasoning_delta_event,
    pending_interaction,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "antigravity" / "steps"
_CID = "test-conversation-id"

# Every recorded fixture belongs to this trajectory; tool-call ids are derived
# from ``(trajectory, step index)`` so they are predictable per fixture.
_TRAJ = "efb134b2-d69f-43de-bb54-c9ece346d8a3"


def _call_id(step_index: int) -> str:
    """The call id the mapper derives for a fixture step in this trajectory."""
    return f"agy_call_{_TRAJ}_{step_index}"


def _item(event: OutboundEvent) -> dict[str, Any]:
    """Return one event's ``item_data``, asserting it is a dict."""
    data = event.data["item_data"]
    assert isinstance(data, dict)
    return data


def _load(name: str) -> dict[str, Any]:
    """Load one step fixture by filename (without extension)."""
    path = _FIXTURES / f"{name}.json"
    return cast(dict[str, Any], json.loads(path.read_text()))


# ---------------------------------------------------------------------------
# Helper: assert no delta event at all
# ---------------------------------------------------------------------------


def _assert_no_delta(events: list[OutboundEvent]) -> None:
    """
    Assert that none of the events are delta events.

    The double-render fix requires that map_step_to_events emits NO
    ``external_output_text_delta`` events whatsoever — the old forwarder emitted
    one delta per assistant text step; the new mapper drops it entirely.
    """
    for event in events:
        assert event.event_type != "external_output_text_delta", (
            f"Unexpected delta event in output: {event}"
        )


# ---------------------------------------------------------------------------
# USER_INPUT → committed user message (#1155)
# ---------------------------------------------------------------------------


class TestUserInputCommitted:
    """
    USER_INPUT steps commit the user's turn as a ``message`` item.

    The pure-RPC write path fires no "direct POST /events" to persist the user
    turn, so the read path must mirror it (parity with claude/codex/cursor
    native). Without it the web UI's optimistic bubble has no committed
    counterpart and renders below the assistant reply (#1155).
    """

    def test_user_input_commits_user_message(self) -> None:
        """USER_INPUT step → exactly one committed user ``message`` item."""
        step = _load("user_input")
        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "external_conversation_item"
        assert ev.data["item_type"] == "message"
        assert ev.data["item_data"]["role"] == "user"
        assert ev.data["item_data"]["content"] == [
            {"type": "input_text", "text": "Say hello in one short sentence."}
        ]
        # A user turn carries no response_id (not an assistant response).
        assert "response_id" not in ev.data

    def test_user_input_no_delta(self) -> None:
        """USER_INPUT commits a message but emits no streaming delta events."""
        step = _load("user_input")
        events = map_step_to_events(step, conversation_id=_CID)
        _assert_no_delta(events)

    def test_user_input_without_text_is_skipped(self) -> None:
        """A USER_INPUT step with no recoverable text emits nothing (no empty bubble)."""
        step = {"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {}}
        events = map_step_to_events(step, conversation_id=_CID)
        assert events == []


# ---------------------------------------------------------------------------
# PLANNER_RESPONSE (text only) → one message, NO delta
# ---------------------------------------------------------------------------


class TestPlannerResponseText:
    """PLANNER_RESPONSE with assistant text → exactly one message item, no delta."""

    def test_returns_exactly_one_event(self) -> None:
        """
        A text-only PLANNER_RESPONSE yields exactly one event.

        No delta means no second event; the old forwarder emitted 2 (delta +
        message); the new mapper emits 1.
        """
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1

    def test_event_type_is_conversation_item(self) -> None:
        """The single event has type ``external_conversation_item``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events[0].event_type == "external_conversation_item"

    def test_item_type_is_message(self) -> None:
        """The event's ``item_type`` is ``"message"``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events[0].data["item_type"] == "message"

    def test_message_role_is_assistant(self) -> None:
        """The ``message`` item has role ``"assistant"``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["role"] == "assistant"

    def test_message_content_is_output_text(self) -> None:
        """
        Content list contains exactly one ``output_text`` block with the
        fixture's ``plannerResponse.response`` text.
        """
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        content = item_data["content"]
        assert isinstance(content, list)
        assert len(content) == 1
        assert content[0]["type"] == "output_text"
        expected_text = (
            "Hello! I am Antigravity, your AI coding assistant, ready to help you with your tasks."
        )
        assert content[0]["text"] == expected_text

    def test_no_delta_event(self) -> None:
        """
        No ``external_output_text_delta`` event is emitted.

        This is the primary double-render fix: the old forwarder emitted a delta
        event before the message; the new mapper drops it entirely.
        """
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        _assert_no_delta(events)

    def test_step_index_from_fixture(self) -> None:
        """step_index on the event matches the fixture's sourceTrajectoryStepInfo.stepIndex."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        # planner_response_text.json has stepIndex=2
        assert events[0].step_index == 2

    def test_response_id_stable(self) -> None:
        """response_id is deterministic: ``agy_<conversation_id>_<stepIndex>``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events[0].data["response_id"] == f"agy_{_CID}_2"


class TestPlannerResponseError:
    """An ERROR PLANNER_RESPONSE emits a visible error item (not a silent drop)."""

    def test_error_planner_emits_one_error_message(self) -> None:
        """A terminal-ERROR planner yields one assistant ``message`` error marker.

        Regression for #6: previously an ERROR planner returned ``[]`` (the branch
        committed only at DONE), so a model/turn error looked identical to a normal
        empty reply. It must surface a visible item.
        """
        step = _load("planner_response_text")
        step["status"] = "CORTEX_STEP_STATUS_ERROR"
        step["plannerResponse"] = {}  # no text/error detail -> generic marker
        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "external_conversation_item"
        assert ev.data["item_type"] == "message"
        item = ev.data["item_data"]
        assert isinstance(item, dict)
        assert item["role"] == "assistant"
        text = item["content"][0]["text"]
        assert "status ERROR" in text

    def test_error_planner_includes_error_detail_when_present(self) -> None:
        """Any ``plannerResponse.error`` text is folded into the marker."""
        step = _load("planner_response_text")
        step["status"] = "CORTEX_STEP_STATUS_ERROR"
        step["plannerResponse"] = {"error": "model overloaded (503)"}
        events = map_step_to_events(step, conversation_id=_CID)
        assert "model overloaded (503)" in events[0].data["item_data"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# PLANNER_RESPONSE (tool_calls) → function_call events
# ---------------------------------------------------------------------------


class TestPlannerToolCallsNotMirrored:
    """
    A planner's ``toolCalls`` is never mirrored — the tool step owns the pair.

    The live stream strips ``plannerResponse.toolCalls`` altogether, so mirroring
    from there produced nothing at all for streamed turns. Mapping it on the poll
    shape only would post a SECOND, differently-keyed invocation for every tool
    the moment the reader fell back to polling.
    """

    def test_tool_call_only_planner_emits_nothing(self) -> None:
        """A DONE planner with tool calls but no text maps to no items."""
        for name in (
            "planner_response_tool_call_run_command",
            "planner_response_tool_call_ask_question",
        ):
            assert map_step_to_events(_load(name), conversation_id=_CID) == []


# ---------------------------------------------------------------------------
# RUN_COMMAND DONE → function_call_output
# ---------------------------------------------------------------------------


class TestRunCommandDone:
    """RUN_COMMAND DONE step → the invocation plus its combinedOutput."""

    def test_returns_the_pair(self) -> None:
        """One DONE run_command → its ``function_call`` and its output."""
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert [event.data["item_type"] for event in events] == [
            "function_call",
            "function_call_output",
        ]

    def test_event_type_is_conversation_item(self) -> None:
        """event_type is ``external_conversation_item``."""
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert {event.event_type for event in events} == {"external_conversation_item"}

    def test_output_from_combined_output_full(self) -> None:
        """
        The output text comes from ``runCommand.combinedOutput.full``.

        The fixture has ``combinedOutput.full = '/Users/bryanli/...scratch\\n'``.
        """
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert _item(events[1])["output"] == "/Users/bryanli/.gemini/antigravity-cli/scratch\n"

    def test_pair_shares_the_step_derived_call_id(self) -> None:
        """
        Both items are keyed on the step's own ``(trajectory, index)`` identity.

        agy's ``metadata.toolCall.id`` ("cbawg2v8" here) is deliberately not
        used: streamed steps carry no such id, so keying on it would make the
        id depend on which RPC delivered the step.
        """
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert {_item(event)["call_id"] for event in events} == {_call_id(6)}

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=6."""
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events[0].step_index == 6


# ---------------------------------------------------------------------------
# RUN_COMMAND WAITING → function_call only (no output yet)
# ---------------------------------------------------------------------------


class TestRunCommandWaiting:
    """
    RUN_COMMAND WAITING step → no function_call_output.

    The command has been proposed but not yet approved/executed. The mapper must
    NOT emit a ``function_call_output`` (no result exists). Task 5 extracts the
    pending interaction for the bridge.
    """

    def test_waiting_emits_no_output_event(self) -> None:
        """WAITING run_command → empty list (no function_call_output)."""
        step = _load("run_command_waiting")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events == []

    def test_waiting_no_delta(self) -> None:
        """No delta event from a WAITING run_command."""
        step = _load("run_command_waiting")
        events = map_step_to_events(step, conversation_id=_CID)
        _assert_no_delta(events)


# ---------------------------------------------------------------------------
# RUN_COMMAND ERROR → error-marker output (close the dangling call)
# ---------------------------------------------------------------------------


class TestRunCommandError:
    """A terminal-ERROR tool step must still close its ``function_call``.

    An ERROR result (e.g. an ignored/timed-out interactive prompt that flips
    WAITING→ERROR) carries no result text, but the pair must still be emitted
    with a marker, or the web UI strands a perpetual in-progress tool card.
    """

    def test_error_emits_the_pair(self) -> None:
        """A failed (ERROR-status) run_command still emits both items."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID)
        assert [event.data["item_type"] for event in events] == [
            "function_call",
            "function_call_output",
        ]

    def test_error_output_shares_the_invocation_id(self) -> None:
        """The error output pairs with the invocation emitted beside it."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID)
        assert {_item(event)["call_id"] for event in events} == {_call_id(6)}

    def test_error_output_text_is_nonempty_marker(self) -> None:
        """The output is a non-empty error marker mentioning the ERROR status."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID)
        output = _item(events[1])["output"]
        assert isinstance(output, str) and output
        assert "ERROR" in output

    def test_error_step_index(self) -> None:
        """step_index matches fixture stepIndex=6."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events[0].step_index == 6


# ---------------------------------------------------------------------------
# Tool-result closure: empty output + unmapped result types
# ---------------------------------------------------------------------------


class TestToolResultClosure:
    """Every tool call is closed, even with empty or unmapped results.

    Regression coverage for half-rendered tool cards: a successful command whose
    output proto3-omits empty ``combinedOutput.full``, and a step of a type the
    mapper has no extractor for, must each still emit the full pair so the web
    UI's tool card resolves.
    """

    def test_done_run_command_empty_output_still_closes(self) -> None:
        """DONE run_command with no combinedOutput → the pair, empty output."""
        step = _load("run_command_done")
        # Proto3 omits empty scalars: drop combinedOutput to simulate a
        # ``cd`` / ``mkdir`` / redirect that produced no captured output.
        step["runCommand"].pop("combinedOutput", None)
        events = map_step_to_events(step, conversation_id=_CID)
        assert [event.data["item_type"] for event in events] == [
            "function_call",
            "function_call_output",
        ]
        assert {_item(event)["call_id"] for event in events} == {_call_id(6)}
        assert _item(events[1])["output"] == ""

    def test_tool_step_with_no_typed_body_still_closes(self) -> None:
        """A tool step whose body the mapper cannot read still emits the pair."""
        step = _load("run_command_done")
        # Re-label as a type with no matching body, and drop the body itself:
        # nothing is extractable, but the card must not be left half-open.
        step["type"] = "CORTEX_STEP_TYPE_CODE_ACTION"
        step.pop("runCommand", None)
        events = map_step_to_events(step, conversation_id=_CID)
        assert [event.data["item_type"] for event in events] == [
            "function_call",
            "function_call_output",
        ]
        assert {_item(event)["call_id"] for event in events} == {_call_id(6)}
        assert _item(events[1])["output"] == ""

    def test_system_step_is_skipped(self) -> None:
        """A step agy never marked as a tool action is not a tool result."""
        for name in ("checkpoint", "conversation_history"):
            assert map_step_to_events(_load(name), conversation_id=_CID) == []


# ---------------------------------------------------------------------------
# LIST_DIRECTORY DONE → function_call_output
# ---------------------------------------------------------------------------


class TestListDirectoryDone:
    """LIST_DIRECTORY DONE step → the invocation plus its listing."""

    def test_returns_the_pair(self) -> None:
        """DONE list_directory → its invocation and its output."""
        step = _load("list_directory_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert [event.data["item_type"] for event in events] == [
            "function_call",
            "function_call_output",
        ]

    def test_pair_shares_the_step_derived_call_id(self) -> None:
        """Both items key on the step's own identity, not agy's toolCall.id."""
        step = _load("list_directory_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert {_item(event)["call_id"] for event in events} == {_call_id(10)}

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=10."""
        step = _load("list_directory_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events[0].step_index == 10


# ---------------------------------------------------------------------------
# ASK_QUESTION WAITING → no output (pending interaction)
# ---------------------------------------------------------------------------


class TestAskQuestionWaiting:
    """
    ASK_QUESTION WAITING → no function_call_output.

    The question is awaiting user response. Key on ``status`` NOT on the
    presence of ``requestedInteraction`` (which persists in the DONE fixture).
    """

    def test_waiting_emits_no_event(self) -> None:
        """WAITING ask_question → empty list."""
        step = _load("ask_question_waiting")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events == []


# ---------------------------------------------------------------------------
# ASK_QUESTION DONE → function_call_output
# ---------------------------------------------------------------------------


class TestAskQuestionDone:
    """ASK_QUESTION DONE step → the invocation plus the answer."""

    def test_returns_the_pair(self) -> None:
        """DONE ask_question → its invocation and its output."""
        step = _load("ask_question_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert [event.data["item_type"] for event in events] == [
            "function_call",
            "function_call_output",
        ]

    def test_pair_shares_the_step_derived_call_id(self) -> None:
        """Both items key on the step's own identity, not agy's toolCall.id."""
        step = _load("ask_question_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert {_item(event)["call_id"] for event in events} == {_call_id(12)}

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=12."""
        step = _load("ask_question_done")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events[0].step_index == 12


# ---------------------------------------------------------------------------
# CHECKPOINT and CONVERSATION_HISTORY → []
# ---------------------------------------------------------------------------


class TestSystemStepsSkipped:
    """CHECKPOINT and CONVERSATION_HISTORY system steps produce no events."""

    def test_checkpoint_returns_empty(self) -> None:
        """CHECKPOINT step → ``[]``."""
        step = _load("checkpoint")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events == []

    def test_conversation_history_returns_empty(self) -> None:
        """CONVERSATION_HISTORY step → ``[]``."""
        step = _load("conversation_history")
        events = map_step_to_events(step, conversation_id=_CID)
        assert events == []


# ---------------------------------------------------------------------------
# Slot-0 step index: absent stepIndex → treated as 0 (CDX-IMP1 + OPUS-MIN1)
# ---------------------------------------------------------------------------


class TestSlotZeroStepIndex:
    """
    A PLANNER_RESPONSE step at slot 0 has no ``stepIndex`` in the proto
    (proto omits zero-valued scalars).  The mapper must treat it as index 0,
    not silently drop the step.
    """

    def test_absent_step_index_emits_event_at_zero(self) -> None:
        """Slot-0 PLANNER_RESPONSE (no stepIndex) → message event with step_index=0."""
        import copy

        base = _load("planner_response_text")
        step = copy.deepcopy(base)
        # Strip stepIndex to simulate proto-default omission
        traj_info = step["metadata"]["sourceTrajectoryStepInfo"]
        assert isinstance(traj_info, dict)
        traj_info.pop("stepIndex", None)

        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1
        assert events[0].step_index == 0

    def test_string_encoded_step_index_accepted(self) -> None:
        """String-encoded stepIndex (e.g. ``'2'``) is accepted and parsed."""
        import copy

        base = _load("planner_response_text")
        step = copy.deepcopy(base)
        traj_info = step["metadata"]["sourceTrajectoryStepInfo"]
        assert isinstance(traj_info, dict)
        traj_info["stepIndex"] = "2"  # String form

        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1
        assert events[0].step_index == 2


# ---------------------------------------------------------------------------
# modifiedResponse precedence (OPUS-MIN2 / Task4-M1)
# ---------------------------------------------------------------------------


class TestModifiedResponsePrecedence:
    """
    ``plannerResponse.modifiedResponse`` takes precedence over ``response``
    when both are present and non-empty.  Both fields appear in live fixtures
    and are equal when no moderation has occurred.  The preference is tested
    with a synthetic step where they differ so the behavior is pinned.
    """

    def test_modified_response_wins_when_different(self) -> None:
        """
        When modifiedResponse differs from response, modifiedResponse is used.

        The original fixture has both equal; this synthetic variant sets them
        to different values to verify the precedence rule explicitly.
        """
        import copy

        base = _load("planner_response_text")
        step = copy.deepcopy(base)
        planner = step.get("plannerResponse")
        assert isinstance(planner, dict)
        planner["response"] = "Original text."
        planner["modifiedResponse"] = "Post-moderation text."

        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        content = item_data["content"]
        assert isinstance(content, list)
        # modifiedResponse wins
        assert content[0]["text"] == "Post-moderation text."

    def test_response_used_when_modified_absent(self) -> None:
        """When modifiedResponse is absent, response is used as fallback."""
        import copy

        base = _load("planner_response_text")
        step = copy.deepcopy(base)
        planner = step.get("plannerResponse")
        assert isinstance(planner, dict)
        planner.pop("modifiedResponse", None)
        planner["response"] = "Fallback text."

        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        content = item_data["content"]
        assert isinstance(content, list)
        assert content[0]["text"] == "Fallback text."

    def test_response_used_when_modified_empty(self) -> None:
        """When modifiedResponse is empty string, response is used as fallback."""
        import copy

        base = _load("planner_response_text")
        step = copy.deepcopy(base)
        planner = step.get("plannerResponse")
        assert isinstance(planner, dict)
        planner["modifiedResponse"] = ""
        planner["response"] = "Non-empty response."

        events = map_step_to_events(step, conversation_id=_CID)
        assert len(events) == 1
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        content = item_data["content"]
        assert isinstance(content, list)
        assert content[0]["text"] == "Non-empty response."


# ---------------------------------------------------------------------------
# Real-id pairing: planner → run_command sequence (OPUS-IMP1)
# ---------------------------------------------------------------------------


class TestPairingIsOrderIndependent:
    """
    Each tool step carries its own pair, so arrival order cannot mis-pair.

    The mapper once correlated invocations to results positionally (FIFO): a
    result arriving before an earlier tool finished took the wrong invocation's
    id. Deriving both items from one step makes mis-pairing unrepresentable —
    there is no cross-step state left to get out of step.
    """

    def test_results_delivered_out_of_order_keep_their_own_ids(self) -> None:
        """Two tool steps mapped in reverse order still key on themselves."""
        ask_question = map_step_to_events(_load("ask_question_done"), conversation_id=_CID)
        run_command = map_step_to_events(_load("run_command_done"), conversation_id=_CID)

        assert {_item(event)["call_id"] for event in ask_question} == {_call_id(12)}
        assert {_item(event)["call_id"] for event in run_command} == {_call_id(6)}

    def test_mapping_a_step_twice_is_stable(self) -> None:
        """
        Re-mapping the same step re-derives identical items.

        The reader replays the on-connect snapshot and re-reads steps across a
        stream→poll fallback; ids that drifted between passes would double the
        tool card instead of deduping it.
        """
        step = _load("run_command_done")
        assert map_step_to_events(step, conversation_id=_CID) == map_step_to_events(
            step, conversation_id=_CID
        )


# ---------------------------------------------------------------------------
# Task 5: pending_interaction extractor
# ---------------------------------------------------------------------------


class TestPendingInteractionAskQuestionWaiting:
    """
    ASK_QUESTION WAITING step → PendingInteraction with kind="ask_question".

    The spec comes from requestedInteraction.askQuestion (NOT from the top-level
    askQuestion field).  trajectory_id and step_index come from
    metadata.sourceTrajectoryStepInfo.
    """

    def test_returns_not_none(self) -> None:
        """A WAITING ask_question step returns a PendingInteraction, not None."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None

    def test_kind_is_ask_question(self) -> None:
        """kind is 'ask_question'."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        assert result["kind"] == "ask_question"

    def test_trajectory_id(self) -> None:
        """trajectory_id matches metadata.sourceTrajectoryStepInfo.trajectoryId."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        assert result["trajectory_id"] == "efb134b2-d69f-43de-bb54-c9ece346d8a3"

    def test_step_index(self) -> None:
        """step_index matches metadata.sourceTrajectoryStepInfo.stepIndex (12)."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        assert result["step_index"] == 12

    def test_spec_is_ask_question_block(self) -> None:
        """spec equals the requestedInteraction.askQuestion block."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        spec = result["spec"]
        assert isinstance(spec, dict)
        assert "questions" in spec

    def test_spec_questions_list(self) -> None:
        """spec.questions is a non-empty list."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        assert len(questions) == 1

    def test_spec_question_text(self) -> None:
        """spec.questions[0].question contains the question text."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        q = questions[0]
        assert isinstance(q, dict)
        assert q["question"] == "What type of project or improvement would you like to focus on?"

    def test_spec_options_list(self) -> None:
        """spec.questions[0].options is a list of 4 option dicts."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        first_q = questions[0]
        assert isinstance(first_q, dict)
        options = first_q.get("options")
        assert isinstance(options, list)
        assert len(options) == 4

    def test_spec_option_id_and_text(self) -> None:
        """Each option has 'id' and 'text' keys."""
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        first_q = questions[0]
        assert isinstance(first_q, dict)
        options = first_q.get("options")
        assert isinstance(options, list)
        first_option = options[0]
        assert isinstance(first_option, dict)
        assert first_option["id"] == "1"
        assert "(Recommended)" in str(first_option["text"])


class TestPendingInteractionRunCommandWaiting:
    """
    RUN_COMMAND WAITING step → PendingInteraction with kind="permission".

    The spec comes from requestedInteraction.permission; resource.action and
    resource.target are the authoritative fields.
    """

    def test_returns_not_none(self) -> None:
        """A WAITING run_command step returns a PendingInteraction, not None."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None

    def test_kind_is_permission(self) -> None:
        """kind is 'permission'."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None
        assert result["kind"] == "permission"

    def test_trajectory_id(self) -> None:
        """trajectory_id from metadata.sourceTrajectoryStepInfo.trajectoryId."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None
        assert result["trajectory_id"] == "efb134b2-d69f-43de-bb54-c9ece346d8a3"

    def test_step_index(self) -> None:
        """step_index matches stepIndex=6."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None
        assert result["step_index"] == 6

    def test_spec_is_permission_block(self) -> None:
        """spec equals the requestedInteraction.permission block."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None
        spec = result["spec"]
        assert isinstance(spec, dict)
        assert "resource" in spec

    def test_spec_resource_action(self) -> None:
        """spec.resource.action is 'command'."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None
        resource = result["spec"]["resource"]
        assert isinstance(resource, dict)
        assert resource["action"] == "command"

    def test_spec_resource_target(self) -> None:
        """spec.resource.target is 'pwd'."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None
        resource = result["spec"].get("resource")
        assert isinstance(resource, dict)
        assert resource["target"] == "pwd"

    def test_spec_action_description(self) -> None:
        """spec.actionDescription is present when the fixture has it."""
        step = _load("run_command_waiting")
        result = pending_interaction(step)
        assert result is not None
        assert result["spec"].get("actionDescription") == "Running pwd command"


class TestPendingInteractionDoneReturnsNone:
    """
    DONE steps with requestedInteraction MUST return None.

    This is the adversarial trap (Task1-M2): both DONE fixtures contain
    requestedInteraction, but status == CORTEX_STEP_STATUS_DONE.  The
    implementation must key on status, not on the presence of
    requestedInteraction.
    """

    def test_ask_question_done_returns_none(self) -> None:
        """
        ask_question_done.json has status=DONE and still contains
        requestedInteraction.askQuestion — must return None.
        """
        step = _load("ask_question_done")
        assert pending_interaction(step) is None

    def test_run_command_done_returns_none(self) -> None:
        """
        run_command_done.json has status=DONE and still contains
        requestedInteraction.permission — must return None.
        """
        step = _load("run_command_done")
        assert pending_interaction(step) is None


class TestPendingInteractionIsMultiSelect:
    """
    pending_interaction merges is_multi_select from metadata.toolCall.argumentsJson
    into each spec.questions[i].

    requestedInteraction.askQuestion does not carry is_multi_select; it lives
    in argumentsJson.  The fix builds a fresh spec dict with the flag injected.
    """

    def test_fixture_is_multi_select_false(self) -> None:
        """
        ask_question_waiting.json has is_multi_select=false in argumentsJson.
        The merged spec must expose is_multi_select=False on questions[0].
        """
        step = _load("ask_question_waiting")
        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        q = questions[0]
        assert isinstance(q, dict)
        assert q.get("is_multi_select") is False

    def test_synthetic_is_multi_select_true(self) -> None:
        """
        Synthetic WAITING step with is_multi_select=true in argumentsJson.
        The merged spec must expose is_multi_select=True on questions[0].
        """
        import copy

        base = _load("ask_question_waiting")
        step = copy.deepcopy(base)

        # Patch argumentsJson to set is_multi_select: true
        metadata = step.get("metadata")
        assert isinstance(metadata, dict)
        tool_call = metadata.get("toolCall")
        assert isinstance(tool_call, dict)
        raw = tool_call.get("argumentsJson")
        assert isinstance(raw, str)
        args = json.loads(raw)
        assert isinstance(args, dict)
        aq_list = args.get("questions")
        assert isinstance(aq_list, list)
        first_q = aq_list[0]
        assert isinstance(first_q, dict)
        first_q["is_multi_select"] = True
        tool_call["argumentsJson"] = json.dumps(args)

        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        q = questions[0]
        assert isinstance(q, dict)
        assert q.get("is_multi_select") is True

    def test_absent_arguments_json_defaults_false(self) -> None:
        """
        When argumentsJson is absent, is_multi_select defaults to False without crashing.
        """
        import copy

        base = _load("ask_question_waiting")
        step = copy.deepcopy(base)

        # Remove argumentsJson from toolCall.
        metadata = step.get("metadata")
        assert isinstance(metadata, dict)
        tool_call = metadata.get("toolCall")
        assert isinstance(tool_call, dict)
        tool_call.pop("argumentsJson", None)

        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        q = questions[0]
        assert isinstance(q, dict)
        assert q.get("is_multi_select") is False

    def test_malformed_arguments_json_defaults_false(self) -> None:
        """
        When argumentsJson is not valid JSON, is_multi_select defaults to False without crashing.
        """
        import copy

        base = _load("ask_question_waiting")
        step = copy.deepcopy(base)

        metadata = step.get("metadata")
        assert isinstance(metadata, dict)
        tool_call = metadata.get("toolCall")
        assert isinstance(tool_call, dict)
        tool_call["argumentsJson"] = "{ not valid json !!!"

        result = pending_interaction(step)
        assert result is not None
        questions = result["spec"].get("questions")
        assert isinstance(questions, list)
        q = questions[0]
        assert isinstance(q, dict)
        assert q.get("is_multi_select") is False

    def test_input_step_not_mutated(self) -> None:
        """
        pending_interaction must not mutate the input step dict.
        requestedInteraction.askQuestion.questions[0] must remain unchanged.
        """
        import copy

        step = _load("ask_question_waiting")
        original = copy.deepcopy(step)

        pending_interaction(step)

        # The original requestedInteraction.askQuestion block must be unchanged.
        ri = step.get("requestedInteraction")
        assert isinstance(ri, dict)
        orig_ri = original.get("requestedInteraction")
        assert isinstance(orig_ri, dict)
        assert ri == orig_ri


# ---------------------------------------------------------------------------
# Reasoning delta builder (output_reasoning_delta_event)
# ---------------------------------------------------------------------------


class TestOutputReasoningDeltaEvent:
    """``output_reasoning_delta_event`` builds the transient reasoning delta."""

    def test_shape_started_true(self) -> None:
        """
        The first reasoning delta of a step carries ``started=True``.

        ``started`` tells the server to precede this delta with one
        ``response.reasoning.started`` (the SPA's new-reasoning-block marker).
        """
        event = output_reasoning_delta_event(step_idx=2, delta="Let me think", started=True)
        assert event.event_type == "external_output_reasoning_delta"
        assert event.data == {"delta": "Let me think", "started": True}
        assert event.step_index == 2

    def test_shape_started_false(self) -> None:
        """A continuation reasoning delta carries ``started=False``."""
        event = output_reasoning_delta_event(step_idx=5, delta=" some more", started=False)
        assert event.data == {"delta": " some more", "started": False}
        assert event.step_index == 5

    def test_no_message_id(self) -> None:
        """
        Reasoning deltas carry NO ``message_id``.

        Unlike ``output_text_delta_event`` (whose ``message_id`` lets the SPA
        coalesce text deltas into one block and reconcile against the committed
        message), the SPA reasoning block is not keyed by a per-step id.
        """
        event = output_reasoning_delta_event(step_idx=2, delta="x", started=True)
        assert "message_id" not in event.data


class TestMapStepEmitsNoReasoning:
    """The pure mapper never emits a reasoning item — reasoning is delta-only."""

    def test_done_planner_with_thinking_emits_no_reasoning_item(self) -> None:
        """
        A DONE planner carrying ``thinking`` maps to message (+ tool calls) only.

        Reasoning is surfaced solely as transient deltas by the streaming reader
        (mirroring the in-process executor, which never commits a reasoning item).
        The DONE-commit path must therefore not introduce a ``reasoning`` item or
        any ``external_output_reasoning_delta``.
        """
        step = _load("planner_response_text")
        cast(dict[str, Any], step["plannerResponse"])["thinking"] = "internal chain of thought"
        events = map_step_to_events(step, conversation_id=_CID)

        for event in events:
            assert event.event_type != "external_output_reasoning_delta"
            if event.event_type == "external_conversation_item":
                assert event.data.get("item_type") != "reasoning"
        # The committed assistant message is still produced (text unchanged).
        item_types = [
            e.data.get("item_type") for e in events if e.event_type == "external_conversation_item"
        ]
        assert "message" in item_types


class TestExecutionDiscriminator:
    """``_execution_discriminator`` extracts the per-turn dedup discriminator."""

    def test_prefers_execution_id(self) -> None:
        """``executionId`` is returned when present (preferred over createdAt)."""
        step = {
            "metadata": {
                "executionId": "exec-123",
                "createdAt": "2026-06-22T23:57:36.256051Z",
            }
        }
        assert _execution_discriminator(step) == "exec-123"

    def test_falls_back_to_created_at(self) -> None:
        """``createdAt`` is used when ``executionId`` is absent."""
        step = {"metadata": {"createdAt": "2026-06-22T23:57:36.256051Z"}}
        assert _execution_discriminator(step) == "2026-06-22T23:57:36.256051Z"

    def test_skips_empty_execution_id(self) -> None:
        """An empty ``executionId`` is skipped in favour of ``createdAt``."""
        step = {"metadata": {"executionId": "", "createdAt": "2026-06-22T23:57:36Z"}}
        assert _execution_discriminator(step) == "2026-06-22T23:57:36Z"

    def test_none_when_no_metadata(self) -> None:
        """A step with no ``metadata`` dict yields ``None``."""
        assert _execution_discriminator({}) is None
        assert _execution_discriminator({"metadata": "not-a-dict"}) is None

    def test_none_when_no_discriminating_fields(self) -> None:
        """``metadata`` without ``executionId`` or ``createdAt`` yields ``None``."""
        assert _execution_discriminator({"metadata": {"source": "user"}}) is None

    def test_real_fixture_has_execution_id(self) -> None:
        """The recorded USER_INPUT fixture carries a usable ``executionId``."""
        step = _load("user_input")
        # Confirms the live wire shape this fix relies on (per-turn executionId).
        assert _execution_discriminator(step) == "1df76a5f-0318-4c71-b31d-7e3b51a3d981"


# ---------------------------------------------------------------------------
# Stream projection: agy omits toolCall / toolCalls from streamed steps
# ---------------------------------------------------------------------------


class TestStreamProjection:
    """
    Map tool steps as delivered by ``StreamAgentStateUpdates``.

    agy serves the same step at two fidelities. ``GetCascadeTrajectorySteps``
    (poll) carries ``metadata.toolCall`` and ``plannerResponse.toolCalls``;
    the live stream strips both — they embed ``thinkingSignature`` blobs, and
    the typed body (``runCommand`` / ``viewFile`` / …) already describes the
    call. Every fixture below is a verbatim live stream frame.

    The mapper therefore derives the whole pair from the RESULT step: its
    ``sourceTrajectoryStepInfo`` is the call id, its typed body the arguments
    and the output. The planner's ``toolCalls`` is never the source, so both
    RPC shapes produce identical items.
    """

    def _pair(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Map a stream fixture and return its (function_call, output) item data."""
        return self._pair_step(_load(name), label=name)

    def _pair_step(
        self, step: dict[str, Any], *, label: str = "step"
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Map one step dict and return its (function_call, output) item data."""
        events = map_step_to_events(step, conversation_id=_CID)
        kinds = [event.data.get("item_type") for event in events]
        assert kinds == ["function_call", "function_call_output"], (
            f"{label} mapped to {kinds}, expected an invocation followed by its output"
        )
        call = events[0].data["item_data"]
        output = events[1].data["item_data"]
        assert isinstance(call, dict) and isinstance(output, dict)
        return call, output

    def test_run_command_emits_a_paired_call_and_output(self) -> None:
        """A streamed RUN_COMMAND yields an invocation AND its output, paired."""
        call, output = self._pair("stream_run_command_done")
        assert call["name"] == "run_command"
        assert call["call_id"] == output["call_id"]
        assert "git status" in str(call["arguments"])
        assert "On branch main" in str(output["output"])

    def test_view_file_is_mirrored_rather_than_dropped(self) -> None:
        """
        REGRESSION: a streamed VIEW_FILE reached the web UI as nothing at all.

        VIEW_FILE is not one of the mapper's known result types, and the stream
        strips the ``metadata.toolCall.id`` that would otherwise classify it —
        so the step was read as system noise and silently dropped. Six of them
        vanished from one live conversation.
        """
        call, output = self._pair("stream_view_file_done")
        assert call["name"] == "view_file"
        assert call["call_id"] == output["call_id"]

    def test_a_suffixed_sibling_does_not_shadow_the_exact_argument(self) -> None:
        """
        An argument whose name matches a body key EXACTLY wins over a prefix hit.

        Streamed steps carry no ``argumentsJson``, so arguments are recovered by
        matching ``metadata.argumentsOrder`` against the typed body — by prefix,
        because agy suffixes some of them (``AbsolutePath`` → ``absolutePathUri``).
        A prefix scan alone returns whichever sibling comes first in the body, so
        a body carrying both spellings could surface the wrong value.
        """
        step = copy.deepcopy(_load("stream_view_file_done"))
        step["metadata"]["argumentsOrder"] = ["AbsolutePath"]
        # The suffixed sibling is FIRST, so a prefix-only scan reaches it first.
        step["viewFile"] = {
            "absolutePathUriPreview": "file:///wrong.json",
            "absolutePath": "file:///right.json",
        }

        call, _ = self._pair_step(step)
        assert "right.json" in str(call["arguments"]), (
            f"the exact argument must win over a suffixed sibling; got {call['arguments']}"
        )
        assert "wrong.json" not in str(call["arguments"])

    def test_generic_step_uses_the_agy_tool_name(self) -> None:
        """GENERIC steps keep ``toolCall`` in the stream; prefer that real name."""
        call, _ = self._pair("stream_generic_done")
        assert call["name"] == "manage_subagents"

    def test_list_directory_uses_the_agy_tool_name(self) -> None:
        """agy calls this tool ``list_dir``, not the type-derived ``list_directory``."""
        call, _ = self._pair("stream_list_directory_done")
        assert call["name"] == "list_dir"

    def test_call_id_is_identical_across_both_rpc_shapes(self) -> None:
        """
        The same step mapped from the stream and from the poll snapshot pairs
        under ONE call id.

        Both fixtures are trajectory ``efb134b2…`` step 6. A reader that falls
        back from the stream to the poll mid-conversation must not re-key the
        pair, or the tool card splits in two.
        """
        stream_call, stream_output = self._pair("stream_run_command_done")
        snapshot_events = map_step_to_events(_load("run_command_done"), conversation_id=_CID)
        snapshot_ids = {
            item["call_id"]
            for event in snapshot_events
            if isinstance(item := event.data["item_data"], dict) and "call_id" in item
        }
        assert snapshot_ids == {stream_call["call_id"]} == {stream_output["call_id"]}

    def test_planner_never_emits_a_function_call(self) -> None:
        """
        The result step is the SOLE source of the pair — on both shapes.

        The poll snapshot's planner still carries ``toolCalls``; emitting from
        there too would post a second, differently-keyed invocation for every
        tool whenever the reader falls back to polling.
        """
        for name in ("stream_planner_tool_call", "planner_response_tool_call_run_command"):
            events = map_step_to_events(_load(name), conversation_id=_CID)
            kinds = [event.data.get("item_type") for event in events]
            assert "function_call" not in kinds, f"{name} emitted an invocation: {kinds}"

    def test_planner_text_still_commits(self) -> None:
        """Stripping toolCalls must not disturb the committed assistant message."""
        events = map_step_to_events(_load("stream_planner_text_done"), conversation_id=_CID)
        assert [event.data.get("item_type") for event in events] == ["message"]

    def test_no_fixture_produces_an_orphan_call_id(self) -> None:
        """
        REGRESSION: streamed results were keyed to invented ``_orphan_N`` ids.

        With no ``toolCall.id`` on the step and no pending invocation to pair
        with (the planner never registered one), every streamed result minted a
        standalone id. 611 such outputs were recorded across 10 live
        conversations, against 0 invocations.
        """
        for path in sorted(_FIXTURES.glob("*.json")):
            events = map_step_to_events(_load(path.stem), conversation_id=_CID)
            for event in events:
                item = event.data.get("item_data")
                call_id = item.get("call_id", "") if isinstance(item, dict) else ""
                assert "orphan" not in str(call_id), f"{path.stem} minted {call_id}"
