"""Compaction must keep both halves of every protected tool exchange."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.entities import ConversationItem, parse_item_data
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.runtime.compaction import compact
from omnigent.runtime.prompt import history_to_input_items
from omnigent.spec.types import CompactionConfig


def _item(kind: str, item_id: str, **data: object) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type=kind,
        status="completed",
        response_id="response_test",
        created_at=1,
        data=parse_item_data(kind, data),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interleaved", [False, True])
async def test_layer_two_preserves_tool_pairs_across_the_recent_boundary(
    interleaved: bool,
) -> None:
    history = [
        _item("message", "old_user", role="user", content=[{"type": "input_text", "text": "old"}]),
        _item(
            "message",
            "old_reply",
            role="assistant",
            agent="test-agent",
            content=[{"type": "output_text", "text": "old reply"}],
        ),
        _item(
            "function_call", "call_a", agent="test-agent", name="read", arguments="{}", call_id="a"
        ),
        _item(
            "function_call", "call_b", agent="test-agent", name="read", arguments="{}", call_id="b"
        ),
        _item("function_call_output", "output_a", call_id="a", output="result a"),
        _item("function_call_output", "output_b", call_id="b", output="result b"),
    ]
    if interleaved:
        history.insert(
            5,
            _item(
                "function_call",
                "call_c",
                agent="test-agent",
                name="read",
                arguments="{}",
                call_id="c",
            ),
        )
        history.append(_item("function_call_output", "output_c", call_id="c", output="result c"))
    messages = history_to_input_items(history)
    summarize = AsyncMock(
        return_value=Response(
            output=[MessageOutput(content=[OutputText(text="Earlier conversation summary.")])],
            model="test-model",
        )
    )

    result = await compact(
        messages,
        history,
        config=CompactionConfig(recent_window=1),
        context_window=8192,
        system_token_budget=0,
        model="test-model",
        task_id="task_compaction_tool_pairs",
        llm_client=SimpleNamespace(responses=SimpleNamespace(create=summarize)),
        force=True,
    )

    calls = {item["call_id"] for item in result.messages if item.get("type") == "function_call"}
    outputs = {
        item["call_id"]: item["output"]
        for item in result.messages
        if item.get("type") == "function_call_output"
    }
    expected = {"a", "b", "c"} if interleaved else {"a", "b"}
    assert calls == expected
    assert outputs == {call_id: f"result {call_id}" for call_id in expected}
    assert result.summary_metadata is not None
    assert result.summary_metadata.last_item_id == "old_reply"
    summarize.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_older_tool_pairs_remain_eligible_for_summarization() -> None:
    history = [
        _item(
            "function_call",
            "old_call",
            agent="test-agent",
            name="read",
            arguments="{}",
            call_id="old",
        ),
        _item("function_call_output", "old_result", call_id="old", output="old output"),
        _item(
            "function_call",
            "new_call",
            agent="test-agent",
            name="read",
            arguments="{}",
            call_id="new",
        ),
        _item("function_call_output", "new_result", call_id="new", output="new output"),
    ]
    summarize = AsyncMock(
        return_value=Response(
            output=[MessageOutput(content=[OutputText(text="Old tool result summary.")])],
            model="test-model",
        )
    )
    result = await compact(
        history_to_input_items(history),
        history,
        config=CompactionConfig(recent_window=1),
        context_window=8192,
        system_token_budget=0,
        model="test-model",
        task_id="task_old_tool_pair",
        llm_client=SimpleNamespace(responses=SimpleNamespace(create=summarize)),
        force=True,
    )
    assert [
        item.get("call_id") for item in result.messages if item.get("type") == "function_call"
    ] == ["new"]
    assert result.summary_metadata is not None
    assert result.summary_metadata.last_item_id == "old_result"
