"""Tests for the REPL ``/revert`` slash command."""

from __future__ import annotations

import pytest

from omnigent.repl import _repl as repl_mod

pytestmark = pytest.mark.asyncio


async def test_revert_command_registered_and_filters_system_messages() -> None:
    """``/revert`` is discoverable and only offers real user prompts."""

    def message(item_id: str, text: str, **extra: object) -> dict[str, object]:
        return {
            "id": item_id,
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            **extra,
        }

    assert "/revert" in repl_mod.COMMANDS
    candidates = repl_mod._revert_user_messages(
        [
            message("msg_real", "retry me"),
            message("msg_system", "[System: timer fired]"),
            message("msg_meta", "hidden", is_meta=True),
        ]
    )
    assert [item["id"] for item in candidates] == ["msg_real"]
