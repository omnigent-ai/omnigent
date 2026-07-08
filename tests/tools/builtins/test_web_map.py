"""Tests for the ``web_map`` built-in tool (Nimble Map backend)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.web_map import WebMapTool, _format_map, _resolve_limit


def test_get_builtin_tool_returns_web_map() -> None:
    """``get_builtin_tool("web_map")`` returns a WebMapTool."""
    tool = get_builtin_tool("web_map")
    assert isinstance(tool, WebMapTool), f"Expected WebMapTool, got {type(tool).__name__}."


def test_tool_name_is_web_map() -> None:
    """Tool name is 'web_map'."""
    assert WebMapTool.name() == "web_map"


def test_schema_requires_url() -> None:
    """The function schema requires a ``url`` parameter."""
    schema = WebMapTool().get_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "web_map"
    assert "url" in schema["function"]["parameters"]["required"]


def test_is_sync() -> None:
    """web_map runs synchronously."""
    assert WebMapTool().is_async() is False


def test_missing_api_key_returns_error(tool_ctx: ToolContext) -> None:
    """Without api_key the tool returns a clear error, never raises."""
    tool = WebMapTool(config={})
    result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "api_key" in result


def test_missing_url_returns_error(tool_ctx: ToolContext) -> None:
    """Without a url the tool returns a clear error, no HTTP call."""
    tool = WebMapTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.web_map.httpx.post") as mock_post:
        result = tool.invoke(json.dumps({}), tool_ctx)
    assert "url" in result.lower()
    assert mock_post.call_count == 0


def test_map_sends_bearer_client_source_and_body(tool_ctx: ToolContext) -> None:
    """api_key → Bearer header, X-Client-Source set, body carries url + limit."""
    fake = MagicMock()
    fake.json.return_value = {"links": [], "success": True, "task_id": "t"}
    tool = WebMapTool(config={"api_key": "spec-key", "limit": "10"})
    with patch("omnigent.tools.builtins.web_map.httpx.post") as mock_post:
        mock_post.return_value = fake
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer spec-key", (
        f"Expected spec config api_key in header, got {headers['Authorization']!r}"
    )
    assert headers["X-Client-Source"] == "omnigent", (
        f"Expected X-Client-Source 'omnigent', got {headers.get('X-Client-Source')!r}"
    )
    body = mock_post.call_args.kwargs["json"]
    assert body["url"] == "https://example.com"
    # limit comes from config as a str ("10") and must be coerced to int.
    assert body["limit"] == 10, f"Expected int 10, got {body['limit']!r}"


def test_format_links_numbered(tool_ctx: ToolContext) -> None:
    """Discovered links are formatted as a numbered list with titles when present."""
    fake = MagicMock()
    fake.json.return_value = {
        "links": [
            {"url": "https://example.com/a", "title": "Page A"},
            {"url": "https://example.com/b"},
        ],
        "success": True,
        "task_id": "t",
    }
    tool = WebMapTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.web_map.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "1. https://example.com/a" in result
    assert "Page A" in result
    assert "2. https://example.com/b" in result


def test_no_links_message(tool_ctx: ToolContext) -> None:
    """An empty link list returns a short message, not a crash."""
    fake = MagicMock()
    fake.json.return_value = {"links": [], "success": True, "task_id": "t"}
    tool = WebMapTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.web_map.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "No links" in result


def test_http_error_returns_string(tool_ctx: ToolContext) -> None:
    """An HTTP error is returned as a string, never raised."""
    fake = MagicMock()
    fake.status_code = 403
    tool = WebMapTool(config={"api_key": "k"})
    with patch("omnigent.tools.builtins.web_map.httpx.post") as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError("403", request=MagicMock(), response=fake)
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "Nimble map error" in result
    assert "403" in result


def test_limit_clamped() -> None:
    """``limit`` is coerced + clamped to 1-1000; junk → default 50."""
    assert _resolve_limit({}) == 50  # missing → default
    assert _resolve_limit({"limit": "0"}) == 1  # below min → clamped up
    assert _resolve_limit({"limit": "5000"}) == 1000  # above max → clamped down
    assert _resolve_limit({"limit": "abc"}) == 50  # non-numeric → default
    assert _resolve_limit({"limit": "25"}) == 25


def test_format_map_direct() -> None:
    """``_format_map`` numbers links and indents the title."""
    out = _format_map({"links": [{"url": "https://x.test", "title": "X"}]})
    assert out == "1. https://x.test\n   X"
