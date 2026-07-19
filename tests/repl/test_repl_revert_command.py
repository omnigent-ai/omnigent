"""Tests for the REPL ``/revert`` slash command."""

from __future__ import annotations

import pytest

from omnigent.repl import _repl as repl_mod

pytestmark = pytest.mark.asyncio


async def test_revert_command_registered_and_filters_system_messages() -> None:
    """``/revert`` is discoverable and only offers real user prompts."""
    assert "/revert" in repl_mod.COMMANDS
    candidates = repl_mod._revert_user_messages(
        [
            {
                "id": "msg_real",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "retry me"}],
            },
            {
                "id": "msg_system",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "[System: timer fired]"}],
            },
            {
                "id": "msg_meta",
                "type": "message",
                "role": "user",
                "is_meta": True,
                "content": [{"type": "input_text", "text": "hidden"}],
            },
        ]
    )
    assert [item["id"] for item in candidates] == ["msg_real"]
