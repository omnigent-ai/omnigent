"""Tests for first-turn automatic-title instruction gating."""

from omnigent.runner.app import _is_first_user_turn
from omnigent.tools.builtins.session_rename import SESSION_RENAME_INSTRUCTION


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
