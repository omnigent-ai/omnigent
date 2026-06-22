"""Blast-radius tests for routing ``web_search`` through runner-local dispatch.

These lock in the expectations behind the ``web_search`` dispatch fix: the tool
must be runner-local (so a non-OpenAI model's call resolves to its backend), and
it must NOT be advertised to native harnesses (which use their own web search).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_web_search_tool,
    should_dispatch_locally,
)


def _spec_with_model(model: str) -> SimpleNamespace:
    """Minimal agent_spec stub: an executor model + a web_search builtin config.

    Uses the in-tree ``perplexity`` backend so this test stands alone (the
    dispatch fix is provider-agnostic; the invariants under test are too).
    """
    return SimpleNamespace(
        executor=SimpleNamespace(model=model),
        tools=SimpleNamespace(
            builtins=[
                SimpleNamespace(
                    name="web_search",
                    config={"search_provider": "perplexity", "api_key": "k"},
                )
            ]
        ),
    )


def test_web_search_is_runner_local() -> None:
    """``web_search`` dispatches locally, like ``web_fetch``."""
    assert "web_search" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("web_search") is True


def test_web_search_not_relayed_to_native_harnesses() -> None:
    """Native harnesses (claude-native / codex-native) use their own web search."""
    assert "web_search" not in _NATIVE_RELAY_BUILTIN_TOOLS


def test_dispatch_preserves_openai_passthrough_fence() -> None:
    """
    For an OpenAI model the handler builds the tool in passthrough mode, so
    ``invoke()`` raises its fence — the third-party backend is NEVER called.
    """
    spec = _spec_with_model("gpt-5.4-mini")  # provider → openai
    with patch("omnigent.tools.builtins.web_search_perplexity.httpx.post") as mock_post:
        with pytest.raises(RuntimeError, match="passthrough"):
            asyncio.run(
                _execute_web_search_tool({"query": "x"}, agent_spec=spec, conversation_id="c")
            )
    assert mock_post.call_count == 0, "OpenAI passthrough must never hit a search backend."


def test_dispatch_databricks_model_uses_function_mode() -> None:
    """A ``databricks-*`` model skips passthrough and runs the configured backend."""
    spec = _spec_with_model("databricks-claude-sonnet-4-6")
    fake_response = MagicMock()
    fake_response.json.return_value = {"choices": [{"message": {"content": "answer"}}]}
    with patch("omnigent.tools.builtins.web_search_perplexity.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = asyncio.run(
            _execute_web_search_tool({"query": "x"}, agent_spec=spec, conversation_id="c")
        )
    assert mock_post.call_count == 1, "databricks-* must run the backend (function mode)."
    assert "answer" in result
