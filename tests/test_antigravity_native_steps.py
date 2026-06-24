"""Tests for the pure RPC step→item mapper.

These exercise :func:`omnigent.antigravity_native_steps.map_step_to_events`
using the real recorded fixtures captured from live agy sessions (Task 1).
No I/O, no live agy: the mapper is driven with fixture dicts and event shapes
are asserted exactly.

Key assertions:
- PLANNER_RESPONSE with text → exactly one ``external_conversation_item``
  ``message`` (role assistant, ``output_text`` content). NO
  ``external_output_text_delta`` / ``output_text_delta`` event.
- USER_INPUT → ``[]`` (skipped — fixes user-dup).
- PLANNER_RESPONSE with tool_calls → ``function_call`` item(s) via allocator.
- RUN_COMMAND DONE → ``function_call_output`` carrying
  ``runCommand.combinedOutput.full``.
- RUN_COMMAND WAITING → ``function_call`` only (no output yet).
- ASK_QUESTION WAITING → ``function_call`` only (no output yet).
- ASK_QUESTION DONE → ``function_call_output`` carrying the formatted answer.
- CHECKPOINT / CONVERSATION_HISTORY → ``[]``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from omnigent.antigravity_native_steps import (
    OutboundEvent,
    _execution_discriminator,
    _ToolCallIdAllocator,
    map_step_to_events,
    output_reasoning_delta_event,
    pending_interaction,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "antigravity" / "steps"
_CID = "test-conversation-id"


def _load(name: str) -> dict[str, Any]:
    """Load one step fixture by filename (without extension)."""
    path = _FIXTURES / f"{name}.json"
    return cast(dict[str, Any], json.loads(path.read_text()))


def _allocator() -> _ToolCallIdAllocator:
    """Fresh allocator for each test."""
    return _ToolCallIdAllocator(conversation_id=_CID)


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
# USER_INPUT → [] (user-dup fix)
# ---------------------------------------------------------------------------


class TestUserInputSkipped:
    """USER_INPUT steps must produce no events (fixes user message duplication)."""

    def test_user_input_returns_empty(self) -> None:
        """
        USER_INPUT step → empty event list.

        The user turn is already persisted by the direct POST /events; emitting
        it again from the RPC stream would duplicate the user message in the UI.
        """
        step = _load("user_input")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events == []

    def test_user_input_no_delta(self) -> None:
        """USER_INPUT step produces no delta events (belt-and-suspenders)."""
        step = _load("user_input")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        _assert_no_delta(events)


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
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1

    def test_event_type_is_conversation_item(self) -> None:
        """The single event has type ``external_conversation_item``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].event_type == "external_conversation_item"

    def test_item_type_is_message(self) -> None:
        """The event's ``item_type`` is ``"message"``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].data["item_type"] == "message"

    def test_message_role_is_assistant(self) -> None:
        """The ``message`` item has role ``"assistant"``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["role"] == "assistant"

    def test_message_content_is_output_text(self) -> None:
        """
        Content list contains exactly one ``output_text`` block with the
        fixture's ``plannerResponse.response`` text.
        """
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        _assert_no_delta(events)

    def test_step_index_from_fixture(self) -> None:
        """step_index on the event matches the fixture's sourceTrajectoryStepInfo.stepIndex."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        # planner_response_text.json has stepIndex=2
        assert events[0].step_index == 2

    def test_response_id_stable(self) -> None:
        """response_id is deterministic: ``agy_<conversation_id>_<stepIndex>``."""
        step = _load("planner_response_text")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].data["response_id"] == f"agy_{_CID}_2"


# ---------------------------------------------------------------------------
# PLANNER_RESPONSE (tool_calls) → function_call events
# ---------------------------------------------------------------------------


class TestPlannerResponseToolCallRunCommand:
    """PLANNER_RESPONSE with run_command tool call → function_call event(s)."""

    def test_returns_one_function_call(self) -> None:
        """One tool call → one ``function_call`` event."""
        step = _load("planner_response_tool_call_run_command")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call"

    def test_function_call_name(self) -> None:
        """The function_call name matches the fixture's toolCall name."""
        step = _load("planner_response_tool_call_run_command")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["name"] == "run_command"

    def test_function_call_id_is_real_agy_id(self) -> None:
        """
        call_id is the real agy-assigned id from plannerResponse.toolCalls[].id.

        The fixture carries id="cbawg2v8"; the mapper must use that directly,
        NOT synthesize a positional id from the allocator.
        """
        alloc = _allocator()
        step = _load("planner_response_tool_call_run_command")
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        # Real agy id from the fixture
        assert item_data["call_id"] == "cbawg2v8"
        # Allocator must NOT have been advanced (real id was used instead)
        assert alloc.invocation_count == 0

    def test_function_call_arguments_strip_display_keys(self) -> None:
        """
        ``toolAction`` and ``toolSummary`` are stripped from the function
        arguments; the real command args remain.
        """
        step = _load("planner_response_tool_call_run_command")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        args_text = item_data["arguments"]
        assert isinstance(args_text, str)
        args = json.loads(args_text)
        assert "toolAction" not in args
        assert "toolSummary" not in args
        # Real args remain
        assert "CommandLine" in args

    def test_no_delta_event(self) -> None:
        """No delta event is emitted for a tool-call-only PLANNER_RESPONSE."""
        step = _load("planner_response_tool_call_run_command")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        _assert_no_delta(events)

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=5."""
        step = _load("planner_response_tool_call_run_command")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].step_index == 5


class TestPlannerResponseToolCallAskQuestion:
    """PLANNER_RESPONSE with ask_question tool call → function_call event."""

    def test_returns_one_function_call(self) -> None:
        """One ask_question tool call → one ``function_call`` event."""
        step = _load("planner_response_tool_call_ask_question")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call"

    def test_function_call_name(self) -> None:
        """The function_call name is ``ask_question``."""
        step = _load("planner_response_tool_call_ask_question")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["name"] == "ask_question"

    def test_function_call_id_is_real_agy_id(self) -> None:
        """
        call_id is the real agy-assigned id from plannerResponse.toolCalls[].id.

        The fixture carries id="jfizoalt"; the allocator must NOT advance.
        """
        alloc = _allocator()
        step = _load("planner_response_tool_call_ask_question")
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["call_id"] == "jfizoalt"
        assert alloc.invocation_count == 0


# ---------------------------------------------------------------------------
# RUN_COMMAND DONE → function_call_output
# ---------------------------------------------------------------------------


class TestRunCommandDone:
    """RUN_COMMAND DONE step → ``function_call_output`` with combinedOutput."""

    def test_returns_one_event(self) -> None:
        """One DONE run_command → one event."""
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1

    def test_event_type_is_conversation_item(self) -> None:
        """event_type is ``external_conversation_item``."""
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].event_type == "external_conversation_item"

    def test_item_type_is_function_call_output(self) -> None:
        """item_type is ``function_call_output``."""
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].data["item_type"] == "function_call_output"

    def test_output_from_combined_output_full(self) -> None:
        """
        The output text comes from ``runCommand.combinedOutput.full``.

        The fixture has ``combinedOutput.full = '/Users/bryanli/...scratch\\n'``.
        """
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["output"] == "/Users/bryanli/.gemini/antigravity-cli/scratch\n"

    def test_call_id_is_real_agy_id(self) -> None:
        """
        call_id is the real agy-assigned id from metadata.toolCall.id.

        The fixture carries toolCall.id="cbawg2v8", matching the invocation
        step's plannerResponse.toolCalls[0].id.  The allocator must NOT be
        consulted (no pending ids needed).
        """
        step = _load("run_command_done")
        alloc = _allocator()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["call_id"] == "cbawg2v8"
        # Allocator was not used (no orphan id minted)
        assert alloc.orphan_output_count == 0

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=6."""
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events == []

    def test_waiting_no_delta(self) -> None:
        """No delta event from a WAITING run_command."""
        step = _load("run_command_waiting")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        _assert_no_delta(events)


# ---------------------------------------------------------------------------
# RUN_COMMAND ERROR → error-marker output (close the dangling call)
# ---------------------------------------------------------------------------


class TestRunCommandError:
    """A terminal-ERROR tool step must still close its ``function_call``.

    The invocation side emits a ``function_call`` for the tool unconditionally,
    so an ERROR result (e.g. an ignored/timed-out interactive prompt that flips
    WAITING→ERROR) must emit a paired ``function_call_output`` keyed on the same
    id, or the web UI strands a perpetual in-progress tool card.
    """

    def test_error_emits_one_output_event(self) -> None:
        """A failed (ERROR-status) run_command emits one function_call_output."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call_output"

    def test_error_output_keyed_on_real_id(self) -> None:
        """The error output pairs by the real agy id (matches the invocation)."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = cast(dict[str, Any], events[0].data["item_data"])
        # Fixture carries toolCall.id="cbawg2v8" — the same id the planner
        # invocation emits, so the pair correlates.
        assert item_data["call_id"] == "cbawg2v8"

    def test_error_output_text_is_nonempty_marker(self) -> None:
        """The output is a non-empty error marker mentioning the ERROR status."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = cast(dict[str, Any], events[0].data["item_data"])
        output = item_data["output"]
        assert isinstance(output, str) and output
        assert "ERROR" in output

    def test_error_step_index(self) -> None:
        """step_index matches fixture stepIndex=6."""
        step = _load("run_command_error")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].step_index == 6


# ---------------------------------------------------------------------------
# Tool-result closure: empty output + unmapped result types
# ---------------------------------------------------------------------------


class TestToolResultClosure:
    """Every tool call is closed, even with empty or unmapped results.

    Regression coverage for dangling ``function_call``s: a successful command
    whose output proto3-omits empty ``combinedOutput.full``, and a result step
    of a type the mapper has no extractor for (e.g. VIEW_FILE / CODE_ACTION),
    must each still emit a ``function_call_output`` keyed on the real
    ``metadata.toolCall.id`` so the web UI's tool card resolves.
    """

    def test_done_run_command_empty_output_still_closes(self) -> None:
        """DONE run_command with no combinedOutput → one event, empty output."""
        step = _load("run_command_done")
        # Proto3 omits empty scalars: drop combinedOutput to simulate a
        # ``cd`` / ``mkdir`` / redirect that produced no captured output.
        step["runCommand"].pop("combinedOutput", None)
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call_output"
        item_data = cast(dict[str, Any], events[0].data["item_data"])
        assert item_data["call_id"] == "cbawg2v8"
        assert item_data["output"] == ""

    def test_unmapped_tool_result_type_with_id_closes(self) -> None:
        """A result type with no extractor but a toolCall.id still closes the call."""
        step = _load("run_command_done")
        # Re-label as a result type the mapper has no extractor for; keep the
        # real toolCall.id so the pair still correlates.
        step["type"] = "CORTEX_STEP_TYPE_VIEW_FILE"
        step.pop("runCommand", None)
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call_output"
        item_data = cast(dict[str, Any], events[0].data["item_data"])
        assert item_data["call_id"] == "cbawg2v8"
        assert item_data["output"] == ""

    def test_system_step_without_tool_id_is_skipped(self) -> None:
        """A non-tool step with no toolCall.id is NOT treated as a tool result."""
        step = _load("checkpoint")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events == []


# ---------------------------------------------------------------------------
# LIST_DIRECTORY DONE → function_call_output
# ---------------------------------------------------------------------------


class TestListDirectoryDone:
    """LIST_DIRECTORY DONE step → ``function_call_output``."""

    def test_returns_one_function_call_output(self) -> None:
        """DONE list_directory → one function_call_output event."""
        step = _load("list_directory_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call_output"

    def test_call_id_is_real_agy_id(self) -> None:
        """call_id is the real agy-assigned id from metadata.toolCall.id."""
        step = _load("list_directory_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        # Fixture carries toolCall.id="h510vxi0"
        assert item_data["call_id"] == "h510vxi0"

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=10."""
        step = _load("list_directory_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events == []


# ---------------------------------------------------------------------------
# ASK_QUESTION DONE → function_call_output
# ---------------------------------------------------------------------------


class TestAskQuestionDone:
    """ASK_QUESTION DONE step → function_call_output."""

    def test_returns_function_call_output(self) -> None:
        """DONE ask_question → one function_call_output event."""
        step = _load("ask_question_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call_output"

    def test_call_id_is_real_agy_id(self) -> None:
        """call_id is the real agy-assigned id from metadata.toolCall.id."""
        step = _load("ask_question_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        # Fixture carries toolCall.id="jfizoalt", matching the planner invocation
        assert item_data["call_id"] == "jfizoalt"

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=12."""
        step = _load("ask_question_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events[0].step_index == 12


# ---------------------------------------------------------------------------
# CHECKPOINT and CONVERSATION_HISTORY → []
# ---------------------------------------------------------------------------


class TestSystemStepsSkipped:
    """CHECKPOINT and CONVERSATION_HISTORY system steps produce no events."""

    def test_checkpoint_returns_empty(self) -> None:
        """CHECKPOINT step → ``[]``."""
        step = _load("checkpoint")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert events == []

    def test_conversation_history_returns_empty(self) -> None:
        """CONVERSATION_HISTORY step → ``[]``."""
        step = _load("conversation_history")
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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

        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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

        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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

        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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

        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
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

        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())
        assert len(events) == 1
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        content = item_data["content"]
        assert isinstance(content, list)
        assert content[0]["text"] == "Non-empty response."


# ---------------------------------------------------------------------------
# Real-id pairing: planner → run_command sequence (OPUS-IMP1)
# ---------------------------------------------------------------------------


class TestRealIdPairing:
    """
    Verify that real agy tool-call ids are used for invocation↔output pairing.

    The RPC carries the same id on both the invocation
    (``plannerResponse.toolCalls[].id``) and the result
    (``metadata.toolCall.id``).  The mapper uses those ids directly —
    no FIFO position, no allocator — so pairing is order-independent.
    """

    def test_planner_then_run_command_done_share_real_id(self) -> None:
        """
        PLANNER_RESPONSE and RUN_COMMAND DONE → both carry the same real agy id.

        The invocation call_id and the output call_id must equal the fixture's
        agy-assigned id ("cbawg2v8"), not a positional allocator id.
        """
        alloc = _allocator()
        planner_step = _load("planner_response_tool_call_run_command")
        planner_events = map_step_to_events(planner_step, conversation_id=_CID, allocator=alloc)
        assert len(planner_events) == 1
        planner_item_data = planner_events[0].data["item_data"]
        assert isinstance(planner_item_data, dict)
        invocation_call_id = planner_item_data["call_id"]
        assert invocation_call_id == "cbawg2v8"

        result_step = _load("run_command_done")
        result_events = map_step_to_events(result_step, conversation_id=_CID, allocator=alloc)
        assert len(result_events) == 1
        result_item = result_events[0].data["item_data"]
        assert isinstance(result_item, dict)
        # Same real id — not a FIFO-synthesized orphan id
        assert result_item["call_id"] == invocation_call_id

    def test_two_results_out_of_order_pair_by_real_id(self) -> None:
        """
        REGRESSION: two tool-result steps with DIFFERENT real ids delivered
        out of order each pair to the correct invocation by real id.

        FIFO would mis-pair: if result-B arrives before result-A, FIFO gives
        result-B the call_id of invocation-A and result-A gets invocation-B's
        id.  Real-id pairing is immune to arrival order.

        We simulate two consecutive PLANNER_RESPONSE steps each invoking a
        different tool (run_command id="cbawg2v8", ask_question id="jfizoalt")
        then deliver their result steps OUT OF ORDER (ask_question result first,
        run_command result second).
        """

        alloc = _allocator()

        # Emit PLANNER_RESPONSE for run_command (id="cbawg2v8")
        rc_planner = _load("planner_response_tool_call_run_command")
        rc_planner_events = map_step_to_events(rc_planner, conversation_id=_CID, allocator=alloc)
        assert len(rc_planner_events) == 1
        rc_item = rc_planner_events[0].data["item_data"]
        assert isinstance(rc_item, dict)
        assert rc_item["call_id"] == "cbawg2v8"

        # Emit PLANNER_RESPONSE for ask_question (id="jfizoalt")
        aq_planner = _load("planner_response_tool_call_ask_question")
        aq_planner_events = map_step_to_events(aq_planner, conversation_id=_CID, allocator=alloc)
        assert len(aq_planner_events) == 1
        aq_item = aq_planner_events[0].data["item_data"]
        assert isinstance(aq_item, dict)
        assert aq_item["call_id"] == "jfizoalt"

        # Now deliver ask_question DONE result FIRST (out of order vs run_command)
        aq_done = _load("ask_question_done")
        aq_result_events = map_step_to_events(aq_done, conversation_id=_CID, allocator=alloc)
        assert len(aq_result_events) == 1
        aq_result_item = aq_result_events[0].data["item_data"]
        assert isinstance(aq_result_item, dict)
        # Must pair with ask_question id, NOT run_command id
        assert aq_result_item["call_id"] == "jfizoalt"

        # Then deliver run_command DONE result
        rc_done = _load("run_command_done")
        rc_result_events = map_step_to_events(rc_done, conversation_id=_CID, allocator=alloc)
        assert len(rc_result_events) == 1
        rc_result_item = rc_result_events[0].data["item_data"]
        assert isinstance(rc_result_item, dict)
        # Must pair with run_command id, NOT ask_question id
        assert rc_result_item["call_id"] == "cbawg2v8"

        # Allocator must not have been used at all (all ids were real)
        assert alloc.invocation_count == 0
        assert alloc.orphan_output_count == 0


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
        events = map_step_to_events(step, conversation_id=_CID, allocator=_allocator())

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
