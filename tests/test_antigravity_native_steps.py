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

from omnigent.antigravity_native_forwarder import OutboundEvent, _ToolCallIdAllocator
from omnigent.antigravity_native_steps import map_step_to_events

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

    def test_function_call_id_from_allocator(self) -> None:
        """call_id is the first id minted by the allocator."""
        alloc = _allocator()
        step = _load("planner_response_tool_call_run_command")
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["call_id"] == f"agy_call_{_CID}_0"

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

    def test_allocator_advances(self) -> None:
        """The allocator's invocation count advances by 1 after emitting the call."""
        alloc = _allocator()
        step = _load("planner_response_tool_call_ask_question")
        map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        # After one call emitted, next call_id would be index 1
        assert alloc.invocation_count == 1


# ---------------------------------------------------------------------------
# RUN_COMMAND DONE → function_call_output
# ---------------------------------------------------------------------------


class TestRunCommandDone:
    """RUN_COMMAND DONE step → ``function_call_output`` with combinedOutput."""

    def test_returns_one_event(self) -> None:
        """One DONE run_command → one event."""
        step = _load("run_command_done")
        # Seed allocator with one pending call (from the planner step)
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert len(events) == 1

    def test_event_type_is_conversation_item(self) -> None:
        """event_type is ``external_conversation_item``."""
        step = _load("run_command_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert events[0].event_type == "external_conversation_item"

    def test_item_type_is_function_call_output(self) -> None:
        """item_type is ``function_call_output``."""
        step = _load("run_command_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert events[0].data["item_type"] == "function_call_output"

    def test_output_from_combined_output_full(self) -> None:
        """
        The output text comes from ``runCommand.combinedOutput.full``.

        The fixture has ``combinedOutput.full = '/Users/bryanli/...scratch\\n'``.
        """
        step = _load("run_command_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["output"] == "/Users/bryanli/.gemini/antigravity-cli/scratch\n"

    def test_call_id_matched_from_allocator(self) -> None:
        """call_id is the oldest pending id (FIFO match from allocator)."""
        alloc = _allocator()
        call_id = alloc.claim_call_id()
        step = _load("run_command_done")
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        item_data = events[0].data["item_data"]
        assert isinstance(item_data, dict)
        assert item_data["call_id"] == call_id

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=6."""
        step = _load("run_command_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
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
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert events == []

    def test_waiting_no_delta(self) -> None:
        """No delta event from a WAITING run_command."""
        step = _load("run_command_waiting")
        alloc = _allocator()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        _assert_no_delta(events)


# ---------------------------------------------------------------------------
# RUN_COMMAND ERROR → no output (graceful skip)
# ---------------------------------------------------------------------------


class TestRunCommandError:
    """RUN_COMMAND ERROR step → empty list (no output to report)."""

    def test_error_emits_no_event(self) -> None:
        """A failed (ERROR-status) run_command step is skipped."""
        step = _load("run_command_error")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert events == []


# ---------------------------------------------------------------------------
# LIST_DIRECTORY DONE → function_call_output
# ---------------------------------------------------------------------------


class TestListDirectoryDone:
    """LIST_DIRECTORY DONE step → ``function_call_output``."""

    def test_returns_one_function_call_output(self) -> None:
        """DONE list_directory → one function_call_output event."""
        step = _load("list_directory_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call_output"

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=10."""
        step = _load("list_directory_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
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
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert events == []


# ---------------------------------------------------------------------------
# ASK_QUESTION DONE → function_call_output
# ---------------------------------------------------------------------------


class TestAskQuestionDone:
    """ASK_QUESTION DONE step → function_call_output."""

    def test_returns_function_call_output(self) -> None:
        """DONE ask_question → one function_call_output event."""
        step = _load("ask_question_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
        assert len(events) == 1
        assert events[0].data["item_type"] == "function_call_output"

    def test_step_index(self) -> None:
        """step_index matches fixture stepIndex=12."""
        step = _load("ask_question_done")
        alloc = _allocator()
        alloc.claim_call_id()
        events = map_step_to_events(step, conversation_id=_CID, allocator=alloc)
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
# Allocator pairing: planner → run_command sequence
# ---------------------------------------------------------------------------


class TestAllocatorFifoOrdering:
    """
    Verify that FIFO pairing works across the planner→result sequence:
    the call_id minted on the planner step is matched by the result step.
    """

    def test_planner_then_run_command_done(self) -> None:
        """
        PLANNER_RESPONSE (tool call) then RUN_COMMAND DONE → the output's
        call_id equals the call emitted by the planner step.
        """
        alloc = _allocator()
        planner_step = _load("planner_response_tool_call_run_command")
        planner_events = map_step_to_events(planner_step, conversation_id=_CID, allocator=alloc)
        assert len(planner_events) == 1
        planner_item_data = planner_events[0].data["item_data"]
        assert isinstance(planner_item_data, dict)
        emitted_call_id = planner_item_data["call_id"]

        result_step = _load("run_command_done")
        result_events = map_step_to_events(result_step, conversation_id=_CID, allocator=alloc)
        assert len(result_events) == 1
        result_item = result_events[0].data["item_data"]
        assert isinstance(result_item, dict)
        assert result_item["call_id"] == emitted_call_id
