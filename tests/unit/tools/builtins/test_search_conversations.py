"""Unit tests for :mod:`omnigent.tools.builtins.search_conversations`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
)
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.search_conversations import (
    SearchConversationsTool,
    _extract_text,
    _format_results,
)

_CTX = ToolContext(task_id="task_test", agent_id="agent_test")


# ── Stubs ────────────────────────────────────────────────


@dataclass
class _FakeItem:
    """Minimal stub for ConversationItem."""

    id: str
    response_id: str
    created_at: int
    type: str
    data: Any


class _FakeConversationStore:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def search(self, query: str, limit: int = 10) -> list[Any]:
        return self._items[:limit]


def _message_data(text: str = "Hello world", role: str = "assistant") -> MessageData:
    """Build a MessageData with a single text block."""
    return MessageData(
        role=role,
        content=[{"text": text}],
        agent="test-agent" if role == "assistant" else None,
    )


def _function_call_data() -> FunctionCallData:
    return FunctionCallData(
        agent="test-agent",
        name="web_search",
        arguments='{"query": "test"}',
        call_id="call_1",
    )


def _function_call_output_data() -> FunctionCallOutputData:
    return FunctionCallOutputData(
        call_id="call_1",
        output="Search results here",
    )


# ── Schema ───────────────────────────────────────────────


def test_schema_shape() -> None:
    """Schema requires 'query' and has optional 'limit'."""
    tool = SearchConversationsTool()
    schema = tool.get_schema()
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "search_conversations"
    assert "query" in func["parameters"]["required"]
    props = func["parameters"]["properties"]
    assert "query" in props
    assert "limit" in props
    assert props["query"]["type"] == "string"
    assert props["limit"]["type"] == "integer"


def test_name_and_description() -> None:
    assert SearchConversationsTool.name() == "search_conversations"
    assert len(SearchConversationsTool.description()) > 0


# ── Invoke ───────────────────────────────────────────────


def test_invoke_returns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """invoke() returns formatted search results."""
    items = [
        _FakeItem(
            id="item_1",
            response_id="conv_1",
            created_at=1000,
            type="message",
            data=_message_data(),
        ),
    ]
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(items),
    )

    tool = SearchConversationsTool()
    result = json.loads(tool.invoke('{"query": "hello"}', _CTX))
    assert len(result["results"]) == 1
    assert result["results"][0]["conversation_id"] == "conv_1"
    assert result["results"][0]["text"] == "Hello world"
    assert result["results"][0]["role"] == "assistant"


def test_invoke_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """invoke() returns empty results with a message."""
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore([]),
    )

    tool = SearchConversationsTool()
    result = json.loads(tool.invoke('{"query": "nothing"}', _CTX))
    assert result["results"] == []
    assert "message" in result


def test_invoke_missing_query() -> None:
    """invoke() returns error when query is missing."""
    tool = SearchConversationsTool()
    result = json.loads(tool.invoke("{}", _CTX))
    assert "error" in result


def test_invoke_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """invoke() passes limit to the store."""
    items = [_FakeItem(f"item_{i}", f"conv_{i}", i, "message", _message_data()) for i in range(20)]
    store = _FakeConversationStore(items)
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: store,
    )

    tool = SearchConversationsTool()
    result = json.loads(tool.invoke('{"query": "test", "limit": 3}', _CTX))
    assert len(result["results"]) == 3


# ── _extract_text ────────────────────────────────────────


def test_extract_text_message() -> None:
    """Extract text from a message item."""
    item = _FakeItem("i", "r", 0, "message", _message_data("Hello world"))
    assert _extract_text(item) == "Hello world"


def test_extract_text_function_call() -> None:
    """Extract text from a function call item."""
    item = _FakeItem("i", "r", 0, "function_call", _function_call_data())
    text = _extract_text(item)
    assert "web_search" in text
    assert '{"query": "test"}' in text


def test_extract_text_function_call_output() -> None:
    """Extract text from a function call output item."""
    item = _FakeItem("i", "r", 0, "function_call_output", _function_call_output_data())
    assert _extract_text(item) == "Search results here"


def test_extract_text_unknown_type() -> None:
    """Unknown data type returns empty string."""

    @dataclass
    class _Unknown:
        pass

    item = _FakeItem("i", "r", 0, "unknown", _Unknown())
    assert _extract_text(item) == ""


# ── _format_results ──────────────────────────────────────


def test_format_results_includes_all_fields() -> None:
    """Each result has conversation_id, item_id, created_at, type."""
    items = [
        _FakeItem("i1", "conv_1", 1000, "message", _message_data()),
    ]
    results = _format_results(items)
    assert len(results) == 1
    r = results[0]
    assert r["conversation_id"] == "conv_1"
    assert r["item_id"] == "i1"
    assert r["created_at"] == 1000
    assert r["type"] == "message"
    assert r["role"] == "assistant"
    assert r["text"] == "Hello world"
