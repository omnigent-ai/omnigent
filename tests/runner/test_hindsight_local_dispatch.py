"""Tests for routing the Hindsight memory builtins through runner-local dispatch.

Without runner-local dispatch a wrapped harness's (claude-sdk / codex / …) call
to hindsight_retain falls through to the harness, which has no such tool, and
silently no-ops. These lock in that the tools dispatch locally, are relayed to
native harnesses, and resolve the bank from the threaded agent identity.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _HINDSIGHT_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_hindsight_tool,
    should_dispatch_locally,
)


def _spec(config: dict[str, str], name: str = "hindsight_retain") -> SimpleNamespace:
    return SimpleNamespace(
        executor=SimpleNamespace(model="claude-opus-4-8"),
        tools=SimpleNamespace(builtins=[SimpleNamespace(name=name, config=config)]),
    )


def test_hindsight_tool_set_is_exactly_the_three() -> None:
    assert set(_HINDSIGHT_TOOLS) == {"hindsight_retain", "hindsight_recall", "hindsight_reflect"}


@pytest.mark.parametrize("name", ["hindsight_retain", "hindsight_recall", "hindsight_reflect"])
def test_hindsight_tools_are_runner_local(name: str) -> None:
    assert name in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally(name) is True


@pytest.mark.parametrize("name", ["hindsight_retain", "hindsight_recall", "hindsight_reflect"])
def test_hindsight_tools_relayed_to_native_harnesses(name: str) -> None:
    # Unlike web_search, native harnesses have no built-in memory of their own.
    assert name in _NATIVE_RELAY_BUILTIN_TOOLS


def test_retain_dispatch_uses_agent_id_as_bank() -> None:
    """With no config bank_id, the bank defaults to the threaded agent_id."""
    client = MagicMock()
    spec = _spec({"api_key": "hsk_test"})
    with patch("hindsight_client.Hindsight", return_value=client):
        result = asyncio.run(
            _execute_hindsight_tool(
                {"content": "DuckDB is my favorite db"},
                tool_name="hindsight_retain",
                agent_spec=spec,
                conversation_id="conv_1",
                agent_id="ag_remy",
            )
        )
    assert result == "Stored to long-term memory."
    assert client.retain.call_args.kwargs["bank_id"] == "ag_remy"
    assert client.retain.call_args.kwargs["content"] == "DuckDB is my favorite db"


def test_recall_dispatch_honors_config_bank_id() -> None:
    """An explicit config bank_id overrides the agent_id default."""
    client = MagicMock()
    response = MagicMock()
    response.results = [MagicMock(text="DuckDB is my favorite db")]
    client.recall.return_value = response
    spec = _spec({"api_key": "hsk_test", "bank_id": "remy-ui-test"}, name="hindsight_recall")
    with patch("hindsight_client.Hindsight", return_value=client):
        result = asyncio.run(
            _execute_hindsight_tool(
                {"query": "favorite db"},
                tool_name="hindsight_recall",
                agent_spec=spec,
                conversation_id="conv_1",
                agent_id="ag_remy",
            )
        )
    assert "DuckDB" in result
    assert client.recall.call_args.kwargs["bank_id"] == "remy-ui-test"


def test_dispatch_missing_api_key_returns_error() -> None:
    spec = _spec({})  # no api_key
    result = asyncio.run(
        _execute_hindsight_tool(
            {"content": "x"},
            tool_name="hindsight_retain",
            agent_spec=spec,
            agent_id="ag_remy",
        )
    )
    assert "api_key" in result.lower()
    # never echoes the success string when it couldn't store
    assert result != "Stored to long-term memory."
    json.dumps(result)  # result is a plain string
