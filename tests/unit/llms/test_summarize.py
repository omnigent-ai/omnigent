"""Unit tests for ``omnigent.llms.summarize`` helpers."""

from __future__ import annotations

from omnigent.llms.summarize import build_summarization_input


def test_build_summarization_input_appends_trigger_when_last_role_is_assistant() -> None:
    """
    A conversation ending in an assistant message must gain a
    trailing user turn so providers that reject assistant-message
    prefill (e.g. Databricks Claude) accept the request.
    """
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    result = build_summarization_input(messages)

    # Original messages preserved at the front.
    assert result[:2] == messages, (
        "Original conversation must be preserved; failure means the helper rewrote prior turns."
    )
    # A new user turn was appended.
    assert len(result) == 3, (
        f"Expected 3 items (2 original + trigger), got {len(result)}. "
        "Failure means the trigger turn was not appended after an assistant "
        "final message."
    )
    assert result[-1]["role"] == "user", (
        "Trigger turn must be role=user so the chat-completions request ends on a user turn."
    )
    # Original list must not be mutated.
    assert len(messages) == 2, "Helper must not mutate the caller's list."


def test_build_summarization_input_skips_trigger_when_last_role_is_user() -> None:
    """
    A conversation already ending in a user message must NOT gain a
    second user turn — some providers reject consecutive same-role
    messages and the existing user turn is already a valid final
    position.
    """
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "summarize"},
    ]

    result = build_summarization_input(messages)

    assert result == messages, (
        "Helper must return the conversation unchanged when it already "
        "ends with a user message; failure means a redundant trigger turn "
        "was appended, which can produce consecutive user turns."
    )
    # Must be a copy — mutating result must not affect caller.
    result.append({"role": "user", "content": "extra"})
    assert len(messages) == 3, "Helper must return a copy, not the input list."


def test_build_summarization_input_appends_trigger_when_last_item_is_tool_output() -> None:
    """
    A conversation ending in a function_call_output (becomes
    role=tool after chat-completions conversion) must gain a
    trailing user turn — tool messages aren't a valid final position
    for the summarization request.
    """
    messages = [
        {"role": "user", "content": "run X"},
        {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "done"},
    ]

    result = build_summarization_input(messages)

    assert len(result) == 4, (
        f"Expected trigger turn appended after tool output, got {len(result)} items."
    )
    assert result[-1]["role"] == "user"
