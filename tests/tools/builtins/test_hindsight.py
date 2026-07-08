"""Tests for the Hindsight long-term memory built-in tools.

The Hindsight client (``hindsight-client``, the optional ``hindsight`` extra,
kept in the ``dev`` set) is mocked by patching ``hindsight_client.Hindsight`` —
no network. Covers registry wiring, schema shape, bank resolution from
``ToolContext``, and the invoke paths for retain / recall / reflect.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins import hindsight as hindsight_mod
from omnigent.tools.builtins.hindsight import (
    HindsightRecallTool,
    HindsightReflectTool,
    HindsightRetainTool,
)


@pytest.fixture(autouse=True)
def _clear_created_banks() -> None:
    """Reset the process-level bank cache so create_bank assertions isolate."""
    hindsight_mod._CREATED_BANKS.clear()
    yield
    hindsight_mod._CREATED_BANKS.clear()


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.retain = MagicMock()
    client.recall = MagicMock()
    client.reflect = MagicMock()
    client.create_bank = MagicMock()
    return client


def _recall_response(texts: list[str]) -> MagicMock:
    response = MagicMock()
    response.results = [MagicMock(text=t) for t in texts]
    return response


def _reflect_response(text: str) -> MagicMock:
    response = MagicMock()
    response.text = text
    return response


def _cfg(**extra: str) -> dict[str, str]:
    return {"api_key": "hsk_test", **extra}


# ---------------------------------------------------------------------------
# Registry + schema
# ---------------------------------------------------------------------------


def test_registry_returns_the_three_tools() -> None:
    assert isinstance(get_builtin_tool("hindsight_retain", _cfg()), HindsightRetainTool)
    assert isinstance(get_builtin_tool("hindsight_recall", _cfg()), HindsightRecallTool)
    assert isinstance(get_builtin_tool("hindsight_reflect", _cfg()), HindsightReflectTool)


def test_names_and_descriptions() -> None:
    assert HindsightRetainTool.name() == "hindsight_retain"
    assert HindsightRecallTool.name() == "hindsight_recall"
    assert HindsightReflectTool.name() == "hindsight_reflect"
    # description() must work without instantiation (used by tool discovery).
    assert "memory" in HindsightRecallTool.description().lower()


@pytest.mark.parametrize(
    ("cls", "param"),
    [
        (HindsightRetainTool, "content"),
        (HindsightRecallTool, "query"),
        (HindsightReflectTool, "query"),
    ],
)
def test_schema_shape(cls: type, param: str) -> None:
    schema = cls(_cfg()).get_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == cls.name()
    assert param in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == [param]


# ---------------------------------------------------------------------------
# Retain
# ---------------------------------------------------------------------------


def test_retain_stores_and_creates_bank(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    tool = HindsightRetainTool(_cfg(bank_id="alice"))
    with patch("hindsight_client.Hindsight", return_value=client):
        result = tool.invoke(json.dumps({"content": "I prefer dark mode."}), tool_ctx)
    assert result == "Stored to long-term memory."
    client.retain.assert_called_once_with(bank_id="alice", content="I prefer dark mode.")
    client.create_bank.assert_called_once_with(bank_id="alice", name="alice")


def test_retain_bank_defaults_to_agent_id(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    tool = HindsightRetainTool(_cfg())  # no bank_id
    with patch("hindsight_client.Hindsight", return_value=client):
        tool.invoke(json.dumps({"content": "x"}), tool_ctx)
    # conftest tool_ctx has agent_id="agent_test"
    assert client.retain.call_args.kwargs["bank_id"] == "agent_test"


def test_retain_with_tags(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    tool = HindsightRetainTool(_cfg(bank_id="b", tags="env:prod, team:core"))
    with patch("hindsight_client.Hindsight", return_value=client):
        tool.invoke(json.dumps({"content": "x"}), tool_ctx)
    assert client.retain.call_args.kwargs["tags"] == ["env:prod", "team:core"]


def test_retain_missing_content_returns_error(tool_ctx: ToolContext) -> None:
    tool = HindsightRetainTool(_cfg())
    assert "content" in tool.invoke(json.dumps({}), tool_ctx).lower()


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


def test_recall_returns_bullet_list(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    client.recall.return_value = _recall_response(["likes tea", "lives in NYC"])
    tool = HindsightRecallTool(_cfg(bank_id="b"))
    with patch("hindsight_client.Hindsight", return_value=client):
        result = tool.invoke(json.dumps({"query": "about the user"}), tool_ctx)
    assert result == "- likes tea\n- lives in NYC"


def test_recall_empty_returns_fallback(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    client.recall.return_value = _recall_response([])
    tool = HindsightRecallTool(_cfg(bank_id="b"))
    with patch("hindsight_client.Hindsight", return_value=client):
        result = tool.invoke(json.dumps({"query": "anything"}), tool_ctx)
    assert result == "No relevant memories found."


def test_recall_passes_budget_max_tokens_and_tags(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    client.recall.return_value = _recall_response(["m"])
    tool = HindsightRecallTool(
        _cfg(
            bank_id="b",
            budget="high",
            max_tokens="2048",
            recall_tags="scope:global",
            recall_tags_match="all",
        )
    )
    with patch("hindsight_client.Hindsight", return_value=client):
        tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    kwargs = client.recall.call_args.kwargs
    assert kwargs["budget"] == "high"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["tags"] == ["scope:global"]
    assert kwargs["tags_match"] == "all"


def test_recall_does_not_create_bank(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    client.recall.return_value = _recall_response(["m"])
    tool = HindsightRecallTool(_cfg(bank_id="b"))
    with patch("hindsight_client.Hindsight", return_value=client):
        tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    client.create_bank.assert_not_called()


# ---------------------------------------------------------------------------
# Reflect
# ---------------------------------------------------------------------------


def test_reflect_returns_answer(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    client.reflect.return_value = _reflect_response("You like tea and live in NYC.")
    tool = HindsightReflectTool(_cfg(bank_id="b"))
    with patch("hindsight_client.Hindsight", return_value=client):
        result = tool.invoke(json.dumps({"query": "what do you know?"}), tool_ctx)
    assert result == "You like tea and live in NYC."
    assert client.reflect.call_args.kwargs["bank_id"] == "b"


def test_reflect_empty_returns_fallback(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    client.reflect.return_value = _reflect_response("")
    tool = HindsightReflectTool(_cfg(bank_id="b"))
    with patch("hindsight_client.Hindsight", return_value=client):
        result = tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    assert result == "No relevant memories found."


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_missing_api_key_returns_error(tool_ctx: ToolContext) -> None:
    tool = HindsightRecallTool({})  # no api_key
    result = tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    assert "api_key" in result.lower()


def test_client_exception_is_caught(tool_ctx: ToolContext) -> None:
    client = _mock_client()
    client.recall.side_effect = RuntimeError("network down")
    tool = HindsightRecallTool(_cfg(bank_id="b"))
    with patch("hindsight_client.Hindsight", return_value=client):
        result = tool.invoke(json.dumps({"query": "q"}), tool_ctx)
    assert result.startswith("Hindsight recall failed:")
