"""Tests for the unified ``web_search`` built-in tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.web_search import WebSearchTool
from omnigent.tools.builtins.web_search_nimble import _resolve_max_results

# ── Registry ─────────────────────────────────────────


def test_get_builtin_tool_returns_web_search() -> None:
    """``get_builtin_tool("web_search")`` returns a WebSearchTool."""
    tool = get_builtin_tool("web_search")
    assert isinstance(tool, WebSearchTool), f"Expected WebSearchTool, got {type(tool).__name__}."


def test_get_builtin_tool_unknown_returns_none() -> None:
    """``get_builtin_tool`` returns ``None`` for unregistered names."""
    assert get_builtin_tool("nonexistent") is None


def test_old_provider_names_not_registered() -> None:
    """Provider-specific names are removed from the registry."""
    for name in ("web_search_openai", "web_search_google", "web_search_perplexity"):
        assert get_builtin_tool(name) is None, (
            f"{name!r} should not be in the registry — use 'web_search' instead."
        )


# ── OpenAI passthrough mode ─────────────────────────


def test_openai_mode_schema_is_passthrough() -> None:
    """
    When llm_provider is 'openai', schema is the native
    ``web_search_preview`` passthrough (not a function schema).
    """
    tool = WebSearchTool(llm_provider="openai")
    schema = tool.get_schema()
    assert schema == {"type": "web_search_preview"}, (
        f"Expected passthrough schema for OpenAI, got {schema}."
    )


def test_openai_mode_invoke_raises(tool_ctx: ToolContext) -> None:
    """
    In OpenAI mode, invoke() raises because execution is server-side.
    """
    tool = WebSearchTool(llm_provider="openai")
    with pytest.raises(RuntimeError, match="passthrough"):
        tool.invoke("{}", tool_ctx)


# ── Function tool mode (non-OpenAI) ─────────────────


def test_non_openai_schema_is_function() -> None:
    """
    For non-OpenAI providers, schema is a standard function schema.
    """
    tool = WebSearchTool(llm_provider="anthropic")
    schema = tool.get_schema()
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "web_search"
    assert "query" in func["parameters"]["required"]


def test_no_provider_defaults_to_function() -> None:
    """When llm_provider is None, tool defaults to function mode."""
    tool = WebSearchTool()
    schema = tool.get_schema()
    assert schema["type"] == "function"


def test_missing_query_returns_error(tool_ctx: ToolContext) -> None:
    """Tool returns error when query param is missing."""
    tool = WebSearchTool(
        config={"search_provider": "perplexity", "api_key": "k"},
        llm_provider="anthropic",
    )
    result = tool.invoke(json.dumps({}), tool_ctx)
    assert "query" in result.lower()


# ── search_provider: google ──────────────────────────


def test_google_backend_via_spec_config(tool_ctx: ToolContext) -> None:
    """
    With search_provider=google and credentials in spec config,
    the tool delegates to Google Custom Search.
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "items": [
            {
                "title": "Python Docs",
                "link": "https://docs.python.org",
                "snippet": "Welcome to Python.",
            },
        ],
    }

    tool = WebSearchTool(
        config={
            "search_provider": "google",
            "api_key": "spec-key",
            "engine_id": "spec-engine",
        },
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_google.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        result = tool.invoke(json.dumps({"query": "python"}), tool_ctx)

    # Google result made it through the unified tool pipeline.
    assert "1. Python Docs" in result
    assert "https://docs.python.org" in result


def test_google_missing_credentials_returns_error(tool_ctx: ToolContext) -> None:
    """
    With search_provider=google but no api_key/engine_id, returns
    a clear error (not a crash).
    """
    tool = WebSearchTool(
        config={"search_provider": "google"},
        llm_provider="anthropic",
    )
    result = tool.invoke(json.dumps({"query": "test"}), tool_ctx)
    # Error message tells the user what's missing.
    assert "api_key" in result
    assert "engine_id" in result


# ── search_provider: perplexity ──────────────────────


def test_perplexity_backend_via_spec_config(tool_ctx: ToolContext) -> None:
    """
    With search_provider=perplexity and api_key in spec config,
    the tool delegates to Perplexity.
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "choices": [
            {"message": {"content": "Python is a language."}},
        ],
        "citations": ["https://python.org"],
    }

    tool = WebSearchTool(
        config={
            "search_provider": "perplexity",
            "api_key": "spec-pplx-key",
        },
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_perplexity.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"query": "python"}), tool_ctx)

    # Perplexity answer + citation made it through.
    assert "Python is a language." in result
    assert "[1] https://python.org" in result


def test_perplexity_missing_key_returns_error(tool_ctx: ToolContext) -> None:
    """
    With search_provider=perplexity but no api_key, returns error.
    """
    tool = WebSearchTool(
        config={"search_provider": "perplexity"},
        llm_provider="anthropic",
    )
    result = tool.invoke(json.dumps({"query": "test"}), tool_ctx)
    assert "api_key" in result


# ── search_provider: nimble ──────────────────────────


def test_nimble_backend_via_spec_config(tool_ctx: ToolContext) -> None:
    """
    With search_provider=nimble and api_key in spec config,
    the tool delegates to Nimble AI web search.
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "results": [
            {
                "title": "Nimble Docs",
                "url": "https://docs.nimbleway.com",
                "description": "Web data platform.",
            },
        ],
        "answer": None,
        "total_results": 1,
    }

    tool = WebSearchTool(
        config={
            "search_provider": "nimble",
            "api_key": "spec-nimble-key",
        },
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"query": "nimble"}), tool_ctx)

    # Nimble result list made it through the unified tool pipeline.
    assert "1. Nimble Docs" in result
    assert "https://docs.nimbleway.com" in result
    assert "Web data platform." in result


def test_nimble_answer_shown_first_when_present(tool_ctx: ToolContext) -> None:
    """
    A non-null ``answer`` is shown before the result list.
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "answer": "Nimble is a web data platform.",
        "results": [
            {"title": "Home", "url": "https://nimbleway.com", "description": "..."},
        ],
    }

    tool = WebSearchTool(
        config={"search_provider": "nimble", "api_key": "k"},
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"query": "nimble"}), tool_ctx)

    assert result.startswith("Nimble is a web data platform.")
    assert "1. Home" in result


def test_nimble_missing_key_returns_error(tool_ctx: ToolContext) -> None:
    """
    With search_provider=nimble but no api_key, returns error.
    """
    tool = WebSearchTool(
        config={"search_provider": "nimble"},
        llm_provider="anthropic",
    )
    result = tool.invoke(json.dumps({"query": "test"}), tool_ctx)
    assert "api_key" in result


def test_nimble_spec_config_used_in_http_call(tool_ctx: ToolContext) -> None:
    """
    api_key from spec config is sent as a Bearer header, and the
    request body carries query / max_results / search_depth.
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {"results": []}

    tool = WebSearchTool(
        config={
            "search_provider": "nimble",
            "api_key": "spec-nimble",
            "max_results": "7",
        },
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"query": "test"}), tool_ctx)

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer spec-nimble", (
        f"Expected spec config api_key in header, got {headers['Authorization']!r}"
    )
    body = mock_post.call_args.kwargs["json"]
    assert body["query"] == "test"
    # max_results comes from config as a str ("7") and must be coerced to int.
    assert body["max_results"] == 7, f"Expected int 7, got {body['max_results']!r}"
    # Default tier is 'lite'.
    assert body["search_depth"] == "lite"


def test_nimble_http_error_returns_error_string(tool_ctx: ToolContext) -> None:
    """An HTTP error (e.g. 401) is returned as a string, never raised."""
    fake_response = MagicMock()
    fake_response.status_code = 401
    tool = WebSearchTool(
        config={"search_provider": "nimble", "api_key": "k"},
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_nimble.httpx.post") as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=fake_response
        )
        result = tool.invoke(json.dumps({"query": "test"}), tool_ctx)
    assert "Nimble search error" in result
    assert "401" in result


def test_nimble_answer_kept_when_no_results(tool_ctx: ToolContext) -> None:
    """A non-null ``answer`` is returned even when ``results`` is empty."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"answer": "Direct answer.", "results": []}
    tool = WebSearchTool(
        config={"search_provider": "nimble", "api_key": "k"},
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"query": "test"}), tool_ctx)
    assert result == "Direct answer.", f"Answer must not be dropped, got {result!r}"


def test_nimble_rejects_unsupported_search_depth(tool_ctx: ToolContext) -> None:
    """An unsupported ``search_depth`` is rejected with a clear error, no HTTP call."""
    tool = WebSearchTool(
        config={"search_provider": "nimble", "api_key": "k", "search_depth": "fast"},
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_nimble.httpx.post") as mock_post:
        result = tool.invoke(json.dumps({"query": "test"}), tool_ctx)
    assert "search_depth" in result
    assert mock_post.call_count == 0, "Must not call the API for an invalid search_depth."


def test_nimble_max_results_clamped() -> None:
    """``max_results`` is coerced + clamped to Nimble's 1-100 range; junk → default."""
    assert _resolve_max_results({}) == 5  # missing → default
    assert _resolve_max_results({"max_results": "0"}) == 1  # below min → clamped up
    assert _resolve_max_results({"max_results": "500"}) == 100  # above max → clamped down
    assert _resolve_max_results({"max_results": "abc"}) == 5  # non-numeric → default


# ── No search_provider set ───────────────────────────


def test_no_search_provider_returns_help_message(tool_ctx: ToolContext) -> None:
    """
    Without search_provider in config, the tool returns a message
    explaining how to configure it. Lists all supported backends.
    """
    tool = WebSearchTool(llm_provider="anthropic")
    result = tool.invoke(json.dumps({"query": "test"}), tool_ctx)

    assert "search_provider" in result, f"Should tell user to set search_provider. Got: {result}"
    assert "google" in result.lower()
    assert "perplexity" in result.lower()
    assert "nimble" in result.lower()


# ── Spec config passed through ───────────────────────


def test_google_spec_config_used_in_http_call(tool_ctx: ToolContext) -> None:
    """
    api_key and engine_id from spec config are passed to the
    Google HTTP call (not from env vars).
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {"items": []}

    tool = WebSearchTool(
        config={
            "search_provider": "google",
            "api_key": "spec-key",
            "engine_id": "spec-engine",
        },
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_google.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        tool.invoke(json.dumps({"query": "test"}), tool_ctx)

    params = mock_get.call_args.kwargs["params"]
    assert params["key"] == "spec-key", f"Expected spec config api_key, got {params['key']!r}"
    assert params["cx"] == "spec-engine", f"Expected spec config engine_id, got {params['cx']!r}"


def test_perplexity_spec_config_used_in_http_call(tool_ctx: ToolContext) -> None:
    """
    api_key from spec config is passed to the Perplexity HTTP call.
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "answer"}}],
    }

    tool = WebSearchTool(
        config={
            "search_provider": "perplexity",
            "api_key": "spec-pplx",
        },
        llm_provider="anthropic",
    )
    with patch("omnigent.tools.builtins.web_search_perplexity.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"query": "test"}), tool_ctx)

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer spec-pplx", (
        f"Expected spec config api_key in header, got {headers['Authorization']!r}"
    )


def test_tool_name_is_web_search() -> None:
    """Tool name is 'web_search' regardless of mode."""
    assert WebSearchTool.name() == "web_search"
    assert WebSearchTool(llm_provider="openai").name() == "web_search"


# ── Async dispatch contract ───────────────────────────────────────────────


def test_non_openai_mode_is_sync_in_sessions_native_mode() -> None:
    """
    ``web_search.is_async()`` returns ``False`` for non-OpenAI mode
    after the DBOS removal.

    The previous non-OpenAI path dispatched a ``kind="tool"``
    background DBOS workflow per search via
    ``_dispatch_server_tool_async``; that helper and the workflow
    were deleted with the durability layer. Until a sessions-native
    async dispatch surface is wired, ``web_search`` runs through
    the synchronous ``invoke`` path for every backend.
    """
    tool = WebSearchTool(
        config={"search_provider": "perplexity", "api_key": "k"},
        llm_provider="anthropic",
    )
    assert tool.is_async() is False


def test_openai_mode_is_not_async() -> None:
    """OpenAI passthrough mode should not enter Omnigent async dispatch."""
    tool = WebSearchTool(llm_provider="openai")
    assert tool.is_async() is False
