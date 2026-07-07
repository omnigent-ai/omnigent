"""Tests for the ``nimble_agent`` built-in tool (Nimble WSA backend)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.nimble_agent import NimbleAgentTool, _format_agent


def test_get_builtin_tool_returns_nimble_agent() -> None:
    """``get_builtin_tool("nimble_agent")`` returns a NimbleAgentTool."""
    tool = get_builtin_tool("nimble_agent")
    assert isinstance(tool, NimbleAgentTool), (
        f"Expected NimbleAgentTool, got {type(tool).__name__}."
    )


def test_tool_name() -> None:
    """Tool name is 'nimble_agent'."""
    assert NimbleAgentTool.name() == "nimble_agent"


def test_schema_requires_query() -> None:
    """The function schema requires a ``query`` parameter."""
    schema = NimbleAgentTool().get_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "nimble_agent"
    assert "query" in schema["function"]["parameters"]["required"]


def test_is_sync() -> None:
    """nimble_agent runs synchronously."""
    assert NimbleAgentTool().is_async() is False


def test_missing_api_key_returns_error(tool_ctx: ToolContext) -> None:
    """Without api_key the tool returns a clear error, never raises."""
    tool = NimbleAgentTool(config={})
    result = tool.invoke(json.dumps({"query": "x"}), tool_ctx)
    assert "api_key" in result


def test_missing_query_returns_error(tool_ctx: ToolContext) -> None:
    """Without a query the tool returns a clear error, no HTTP call."""
    tool = NimbleAgentTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        result = tool.invoke(json.dumps({}), tool_ctx)
    assert "query" in result.lower()
    assert mock_post.call_count == 0


def test_sends_bearer_client_source_agent_and_params(tool_ctx: ToolContext) -> None:
    """api_key → Bearer, X-Client-Source set, body carries agent + params{query}."""
    fake = MagicMock()
    fake.json.return_value = {"data": {"parsing": {"entities": {}}}, "status": "success"}
    tool = NimbleAgentTool(config={"api_key": "spec-key", "agent": "google_search"})
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        mock_post.return_value = fake
        tool.invoke(json.dumps({"query": "nimbleway"}), tool_ctx)

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer spec-key", (
        f"Expected spec config api_key in header, got {headers['Authorization']!r}"
    )
    assert headers["X-Client-Source"] == "omnigent", (
        f"Expected X-Client-Source 'omnigent', got {headers.get('X-Client-Source')!r}"
    )
    body = mock_post.call_args.kwargs["json"]
    assert body["agent"] == "google_search"
    assert body["params"] == {"query": "nimbleway"}


def test_default_agent_is_google_search(tool_ctx: ToolContext) -> None:
    """When no ``agent`` is configured, the default WSA template is used."""
    fake = MagicMock()
    fake.json.return_value = {"data": {"parsing": {"entities": {}}}, "status": "success"}
    tool = NimbleAgentTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        mock_post.return_value = fake
        tool.invoke(json.dumps({"query": "x"}), tool_ctx)
    assert mock_post.call_args.kwargs["json"]["agent"] == "google_search"


def test_returns_structured_entities_json(tool_ctx: ToolContext) -> None:
    """The parsed ``entities`` are returned as structured JSON."""
    entities = {"OrganicResult": [{"title": "Nimble", "url": "https://nimbleway.com"}]}
    fake = MagicMock()
    fake.json.return_value = {"data": {"parsing": {"entities": entities}}, "status": "success"}
    tool = NimbleAgentTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = tool.invoke(json.dumps({"query": "nimble"}), tool_ctx)

    assert "OrganicResult" in result
    assert "https://nimbleway.com" in result
    assert json.loads(result) == entities, "Result must be valid JSON of the entities."


def test_markdown_fallback_when_no_entities(tool_ctx: ToolContext) -> None:
    """When there are no parsed entities, markdown is used as the fallback."""
    fake = MagicMock()
    fake.json.return_value = {"data": {"parsing": None, "markdown": "# md"}, "status": "success"}
    tool = NimbleAgentTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = tool.invoke(json.dumps({"query": "x"}), tool_ctx)
    assert "# md" in result


def test_no_data_returns_status_message(tool_ctx: ToolContext) -> None:
    """An empty response returns a short status message."""
    fake = MagicMock()
    fake.json.return_value = {"data": {}, "status": "blocked"}
    tool = NimbleAgentTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = tool.invoke(json.dumps({"query": "x"}), tool_ctx)
    assert "No structured data" in result
    assert "blocked" in result


def test_http_error_returns_string(tool_ctx: ToolContext) -> None:
    """An HTTP error is returned as a string, never raised."""
    fake = MagicMock()
    fake.status_code = 500
    tool = NimbleAgentTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=fake)
        result = tool.invoke(json.dumps({"query": "x"}), tool_ctx)
    assert "Nimble agent error" in result
    assert "500" in result


def test_format_agent_direct() -> None:
    """``_format_agent`` returns the entities as JSON text."""
    out = _format_agent({"data": {"parsing": {"entities": {"a": 1}}}, "status": "success"})
    assert json.loads(out) == {"a": 1}
