"""Tests for llms.adapters.litellm -- SDK delegation and streaming."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.llms.adapters.litellm import LiteLLMAdapter

# ── Unit tests (mocked litellm) ──────────────────────────


@pytest.mark.asyncio
async def test_non_streaming_delegates_to_acompletion() -> None:
    adapter = LiteLLMAdapter()

    mock_response = MagicMock()
    mock_response.model_dump.return_value = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }

    with patch.dict("sys.modules", {"litellm": MagicMock()}):
        import sys

        mock_litellm = sys.modules["litellm"]
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        result = await adapter.chat_completions(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o",
            tools=None,
            stream=False,
            extra={"temperature": 0.7},
            connection_params={
                "api_key": "sk-test",
                "base_url": "http://localhost:4000",
            },
        )

    assert isinstance(result, dict)
    assert result["model"] == "gpt-4o"
    assert result["choices"][0]["message"]["content"] == "Hello!"

    call_kwargs = mock_litellm.acompletion.call_args[1]
    assert call_kwargs["model"] == "gpt-4o"
    assert call_kwargs["api_key"] == "sk-test"
    assert call_kwargs["base_url"] == "http://localhost:4000"
    assert call_kwargs["drop_params"] is True
    assert call_kwargs["temperature"] == 0.7
    assert call_kwargs["stream"] is False


@pytest.mark.asyncio
async def test_tools_forwarded() -> None:
    adapter = LiteLLMAdapter()

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    mock_response = MagicMock()
    mock_response.model_dump.return_value = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
    }

    with patch.dict("sys.modules", {"litellm": MagicMock()}):
        import sys

        mock_litellm = sys.modules["litellm"]
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        await adapter.chat_completions(
            messages=[{"role": "user", "content": "Weather?"}],
            model="gpt-4o",
            tools=tools,
            stream=False,
            extra={},
        )

    call_kwargs = mock_litellm.acompletion.call_args[1]
    assert call_kwargs["tools"] == tools


@pytest.mark.asyncio
async def test_streaming_yields_chunks() -> None:
    adapter = LiteLLMAdapter()

    chunk1 = MagicMock()
    chunk1.model_dump.return_value = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o",
        "choices": [
            {"index": 0, "delta": {"content": "Hello"}, "finish_reason": None},
        ],
    }

    chunk2 = MagicMock()
    chunk2.model_dump.return_value = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o",
        "choices": [
            {"index": 0, "delta": {"content": " world"}, "finish_reason": "stop"},
        ],
    }

    async def mock_acompletion(**_kwargs: Any) -> Any:
        class MockStream:
            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                if not hasattr(self, "_items"):
                    self._items = iter([chunk1, chunk2])
                try:
                    return next(self._items)
                except StopIteration:
                    raise StopAsyncIteration from None

        return MockStream()

    with patch.dict("sys.modules", {"litellm": MagicMock()}):
        import sys

        mock_litellm = sys.modules["litellm"]
        mock_litellm.acompletion = mock_acompletion

        result = await adapter.chat_completions(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o",
            tools=None,
            stream=True,
            extra={},
        )

        chunks = []
        async for c in result:  # type: ignore[union-attr]
            chunks.append(c)

    assert len(chunks) == 2
    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello"
    assert chunks[1]["choices"][0]["delta"]["content"] == " world"


@pytest.mark.asyncio
async def test_import_error_raised_when_litellm_missing() -> None:
    adapter = LiteLLMAdapter()

    with patch.dict("sys.modules", {"litellm": None}):
        with pytest.raises(ImportError, match="litellm is required"):
            await adapter.chat_completions(
                messages=[{"role": "user", "content": "Hi"}],
                model="gpt-4o",
                tools=None,
                stream=False,
                extra={},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_string",
    [
        "gpt-4o",
        "anthropic/claude-sonnet-4-6",
        "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "vertex_ai/gemini-2.5-pro",
        "groq/llama-3.3-70b-versatile",
        "deepseek/deepseek-chat",
        "together_ai/meta-llama/Llama-3-8b-chat-hf",
        "ollama/llama3",
    ],
)
async def test_any_model_string_forwarded(model_string: str) -> None:
    adapter = LiteLLMAdapter()

    mock_response = MagicMock()
    mock_response.model_dump.return_value = {
        "id": "chatcmpl-abc",
        "model": model_string,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }

    with patch.dict("sys.modules", {"litellm": MagicMock()}):
        import sys

        mock_litellm = sys.modules["litellm"]
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        await adapter.chat_completions(
            messages=[{"role": "user", "content": "Hi"}],
            model=model_string,
            tools=None,
            stream=False,
            extra={},
        )

    call_kwargs = mock_litellm.acompletion.call_args[1]
    assert call_kwargs["model"] == model_string


# ── Registration ─────────────────────────────────────────


def test_get_adapter_returns_litellm_adapter() -> None:
    from omnigent.llms.adapters import clear_cache, get_adapter

    clear_cache()
    adapter = get_adapter("litellm")
    assert isinstance(adapter, LiteLLMAdapter)


def test_litellm_provider_config_has_auth_mode() -> None:
    from omnigent.onboarding.providers import get_provider_config

    config = get_provider_config("litellm")
    assert config.default_mode == "api_key"
    fields = {f.name for f in config.auth_modes[0].fields}
    assert "api_key" in fields
    assert "base_url" in fields


def test_parse_model_string_litellm_nested_provider() -> None:
    from omnigent.llms.routing import parse_model_string

    routed = parse_model_string("litellm/anthropic/claude-sonnet-4-6")
    assert routed.provider == "litellm"
    assert routed.model == "anthropic/claude-sonnet-4-6"


# ── Live tests (real litellm SDK, @pytest.mark.live) ─────
# Require: pip install litellm, ANTHROPIC_FOUNDRY_API_KEY set


def _skip_unless_litellm_installed() -> None:
    pytest.importorskip("litellm", reason="litellm not installed")


def _get_live_connection_params(provider: str = "anthropic") -> dict[str, str]:
    import os

    api_key = os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_FOUNDRY_API_KEY not set")

    if provider == "openai":
        base_url = "https://amanrai-test-resource.openai.azure.com/openai/v1"
    else:
        base_url = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL", "")
        if base_url and not base_url.startswith("http"):
            base_url = f"https://{base_url}"
    return {"api_key": api_key, "base_url": base_url}


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_tool_calling() -> None:
    """Tool/function calling produces a valid tool_calls response."""
    _skip_unless_litellm_installed()
    import json

    params = _get_live_connection_params()
    adapter = LiteLLMAdapter()

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather in a city",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "City name",
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                        },
                    },
                    "required": ["city"],
                },
            },
        }
    ]
    result = await adapter.chat_completions(
        messages=[{"role": "user", "content": "What is the weather in Tokyo?"}],
        model="anthropic/claude-sonnet-4-6",
        tools=tools,
        stream=False,
        extra={"max_tokens": 200},
        connection_params=params,
    )

    choice = result["choices"][0]
    assert choice["finish_reason"] in ("tool_calls", "stop")
    if choice["finish_reason"] == "tool_calls":
        tc = choice["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        args = json.loads(tc["function"]["arguments"])
        assert "city" in args


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_streaming() -> None:
    """Streaming produces multiple chunks with substantive content."""
    _skip_unless_litellm_installed()
    params = _get_live_connection_params()
    adapter = LiteLLMAdapter()

    result = await adapter.chat_completions(
        messages=[
            {
                "role": "user",
                "content": "Explain what an API gateway is in exactly 2 sentences.",
            },
        ],
        model="anthropic/claude-sonnet-4-6",
        tools=None,
        stream=True,
        extra={"max_tokens": 200},
        connection_params=params,
    )

    chunks = []
    full_content = ""
    async for chunk in result:  # type: ignore[union-attr]
        chunks.append(chunk)
        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
        if delta:
            full_content += delta

    assert len(chunks) >= 3
    assert len(full_content) > 50


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_full_routing_pipeline() -> None:
    """E2E: parse_model_string -> get_adapter -> chat_completions -> Response."""
    _skip_unless_litellm_installed()
    params = _get_live_connection_params()

    from omnigent.llms._responses_to_chat import chat_response_to_response
    from omnigent.llms.adapters import clear_cache, get_adapter
    from omnigent.llms.routing import parse_model_string

    clear_cache()
    routed = parse_model_string("litellm/anthropic/claude-sonnet-4-6")
    adapter = get_adapter(routed.provider)
    assert isinstance(adapter, LiteLLMAdapter)

    result = await adapter.chat_completions(
        messages=[{"role": "user", "content": "Respond with exactly: LITELLM_OK"}],
        model=routed.model,
        tools=None,
        stream=False,
        extra={"max_tokens": 20},
        connection_params=params,
    )

    response = chat_response_to_response(result)
    assert response.output
    assert response.model
    assert response.output[0].content[0].text


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,provider_key",
    [
        ("anthropic/claude-sonnet-4-6", "anthropic"),
        ("openai/gpt-4o-mini", "openai"),
        ("openai/gpt-4o", "openai"),
        ("openai/gpt-4.1-mini", "openai"),
    ],
)
async def test_live_any_model_string(model: str, provider_key: str) -> None:
    """Same adapter handles arbitrary model strings across providers."""
    _skip_unless_litellm_installed()
    params = _get_live_connection_params(provider=provider_key)
    adapter = LiteLLMAdapter()

    result = await adapter.chat_completions(
        messages=[{"role": "user", "content": "Say OK and nothing else."}],
        model=model,
        tools=None,
        stream=False,
        extra={"max_tokens": 10},
        connection_params=params,
    )

    assert isinstance(result, dict)
    assert result["choices"][0]["message"]["content"]
