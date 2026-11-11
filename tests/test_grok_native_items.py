"""Unit tests for the grok-native session/update item mapper."""

from __future__ import annotations

import json

from omnigent.harnesses.grok_native.items import session_update_to_items


def test_agent_message_chunk_maps_to_assistant_message() -> None:
    items = session_update_to_items(
        {
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hi"},
            }
        }
    )
    assert [(i.kind, i.role, i.text) for i in items] == [("message", "assistant", "hi")]


def test_thought_chunk_maps_to_reasoning() -> None:
    items = session_update_to_items(
        {
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "hmm"},
            }
        }
    )
    assert [(i.kind, i.role) for i in items] == [("reasoning", "assistant")]


def test_tool_call_maps_to_function_call() -> None:
    items = session_update_to_items(
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "shell",
                "rawInput": {"cmd": "ls"},
            }
        }
    )
    assert len(items) == 1
    call = items[0]
    assert (call.kind, call.call_id, call.name) == ("function_call", "t1", "shell")
    assert json.loads(call.arguments) == {"cmd": "ls"}


def test_tool_call_update_completed_maps_to_output() -> None:
    items = session_update_to_items(
        {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "done"}}],
            }
        }
    )
    assert items == items
    assert (items[0].kind, items[0].call_id, items[0].text) == (
        "function_call_output",
        "t1",
        "done",
    )


def test_unknown_or_empty_update_yields_nothing() -> None:
    assert session_update_to_items({}) == []
    assert session_update_to_items({"update": {"sessionUpdate": "plan"}}) == []
    assert session_update_to_items({"update": "junk"}) == []
