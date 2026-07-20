"""Tests for first-turn automatic-title instruction gating."""

from omnigent.runner.app import _is_first_user_turn
from omnigent.tools.builtins.session_rename import (
    CLAUDE_NATIVE_SESSION_RENAME_TOOL,
    SESSION_RENAME_INSTRUCTION,
    session_rename_allowed_tools,
    session_rename_instruction,
)


def test_first_user_turn_requires_one_user_message_and_no_assistant() -> None:
    first = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]
    with_metadata = [{"type": "error"}, *first]
    replied = [*first, {"type": "message", "role": "assistant", "content": []}]
    second_user = [*first, {"type": "message", "role": "user", "content": []}]

    assert _is_first_user_turn(first) is True
    assert _is_first_user_turn(with_metadata) is True
    assert _is_first_user_turn(replied) is False
    assert _is_first_user_turn(second_user) is False
    assert "sys_session_rename" in SESSION_RENAME_INSTRUCTION
    assert "3-6 words" in SESSION_RENAME_INSTRUCTION
    assert "Strip filler; keep the noun + verb" in SESSION_RENAME_INSTRUCTION
    assert 'title:  "Debug double React re-render"' in SESSION_RENAME_INSTRUCTION
    assert "ToolSearch" in SESSION_RENAME_INSTRUCTION


def test_session_rename_instruction_uses_shared_initial_session_gate() -> None:
    """History and native launch paths share one instruction selector."""
    assert session_rename_instruction(initial_session=True) == SESSION_RENAME_INSTRUCTION
    assert session_rename_instruction(initial_session=False) is None


def test_session_rename_instruction_requires_every_fresh_session() -> None:
    """Fresh sessions rename even when the prompt already resembles a title."""
    assert 'prompt: "What should we work on today?"' in SESSION_RENAME_INSTRUCTION
    assert 'title:  "Plan today\'s priorities"' in SESSION_RENAME_INSTRUCTION
    assert "Every fresh session must call sys_session_rename" in SESSION_RENAME_INSTRUCTION
    assert "resembles a finished title" in SESSION_RENAME_INSTRUCTION
    assert "Skip sys_session_rename only" not in SESSION_RENAME_INSTRUCTION


def test_session_rename_allowed_tools_are_fresh_session_only() -> None:
    """Claude preapproves only the silent metadata tool on fresh sessions."""
    assert session_rename_allowed_tools(initial_session=True) == (
        CLAUDE_NATIVE_SESSION_RENAME_TOOL,
    )
    assert session_rename_allowed_tools(initial_session=False) == ()
