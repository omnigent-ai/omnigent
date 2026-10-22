"""Dispatch tests for the ``web_read`` builtin (runner-local, like web_search).

Lock in that ``web_read`` dispatches locally (a non-OpenAI model's call
resolves to ``WebReadTool.invoke`` → the configured backend) and is not
relayed to native harnesses, and that the runner threads the spec's backend
config through to the tool.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_web_read_tool,
    _web_read_config_from_spec,
    should_dispatch_locally,
)


def _spec_with_web_read(config: dict[str, str]) -> SimpleNamespace:
    """Minimal agent_spec stub: a single ``web_read`` builtin carrying ``config``."""
    return SimpleNamespace(
        tools=SimpleNamespace(
            builtins=[SimpleNamespace(name="web_read", config=config)],
        ),
    )


def test_web_read_is_runner_local() -> None:
    """``web_read`` dispatches locally, like ``web_search``."""
    assert "web_read" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("web_read") is True


def test_web_read_not_relayed_to_native_harnesses() -> None:
    """Native harnesses use their own tooling; web_read is not relayed."""
    assert "web_read" not in _NATIVE_RELAY_BUILTIN_TOOLS


def test_config_from_spec_reads_config() -> None:
    """The config helper returns the ``web_read`` builtin's config dict."""
    config = {"read_provider": "nimble", "api_key": "k", "driver": "vx10"}
    spec = _spec_with_web_read(config)
    assert _web_read_config_from_spec(spec) == config


def test_config_from_spec_empty_when_absent() -> None:
    """No ``web_read`` entry (or no spec) → empty config."""
    spec = SimpleNamespace(tools=SimpleNamespace(builtins=[]))
    assert _web_read_config_from_spec(spec) == {}
    assert _web_read_config_from_spec(None) == {}


def test_dispatch_executes_backend_with_spec_config() -> None:
    """
    End-to-end: the runner reads the spec's read_provider, builds the tool,
    runs the (blocking) backend off the event loop, and returns its content.
    """
    fake_response = MagicMock()
    fake_response.text = "Dispatched page body."
    spec = _spec_with_web_read({"read_provider": "jina"})

    with patch("omnigent.tools.builtins.web_read_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        out = asyncio.run(
            _execute_web_read_tool(
                {"url": "https://example.com"},
                agent_spec=spec,
                conversation_id="c",
            )
        )

    assert "Dispatched page body." in out
    assert out.startswith("Source: https://example.com")


def test_dispatch_returns_error_string_not_exception() -> None:
    """A misconfigured spec (no read_provider) yields a loud error, never raises."""
    spec = _spec_with_web_read({})
    out = asyncio.run(
        _execute_web_read_tool(
            {"url": "https://example.com"},
            agent_spec=spec,
            conversation_id="c",
        )
    )
    assert out.startswith("web_read error: no read_provider")
