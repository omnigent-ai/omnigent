"""Tests for shared harness-bench session-item parsing."""

from tests.harness_bench.session_items import (
    assistant_text,
    contains_user_text,
    function_calls,
    item_role,
    item_type,
    tool_output_states,
)


def test_item_type_accepts_top_level_and_nested_shapes() -> None:
    assert item_type({"type": "message"}) == "message"
    assert item_type({"data": {"type": "function_call"}}) == "function_call"
    assert item_role({"data": {"role": "assistant"}}) == "assistant"


def test_assistant_text_reads_both_item_shapes() -> None:
    items = [
        {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
        {
            "data": {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "two"}],
            }
        },
    ]
    assert assistant_text(items) == "one\ntwo"
    assert assistant_text(items, separator=" ") == "one two"


def test_assistant_text_accepts_untyped_text_and_ignores_non_text_blocks() -> None:
    item = {
        "role": "assistant",
        "content": [
            {"text": "plain"},
            {"type": "reasoning", "text": "internal"},
            {"type": "image", "text": "metadata"},
        ],
    }
    assert assistant_text(item) == "plain"


def test_function_calls_and_output_states_are_normalized() -> None:
    items = [
        {"data": {"type": "function_call", "call_id": "c1", "name": "Bash"}},
        {"type": "function_call_output", "data": {"output": "ok"}},
        {"type": "function_call_output", "data": {"status": "blocked", "output": "no"}},
    ]
    assert function_calls(items) == [{"call_id": "c1", "name": "Bash", "arguments": None}]
    assert tool_output_states(items, deny_marker="bench-deny") == (True, True)


def test_contains_user_text_reads_message_blocks() -> None:
    items = [
        {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "text", "text": "interrupted"}]},
        }
    ]
    assert contains_user_text(items, "interrupt")
