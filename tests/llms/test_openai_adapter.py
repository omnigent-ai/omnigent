"""Tests for llms.adapters.openai — payload building and SSE parsing."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from omnigent.llms.adapters.openai import (
    OpenAIAdapter,
    OpenAICompatibleAdapter,
    _parse_sse_line,
)
from omnigent.llms.types import ResponseTextDeltaEvent

# ── Payload building ─────────────────────────────────────


def test_basic_payload_structure() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key_env=None,
    )
    payload = adapter._build_payload(
        messages=[{"role": "user", "content": "Hi"}],
        model="gpt-5.4",
        tools=None,
        stream=False,
        extra={},
    )
    assert payload["model"] == "gpt-5.4"
    assert payload["messages"] == [{"role": "user", "content": "Hi"}]
    assert "tools" not in payload
    assert "stream" not in payload


def test_tools_included_when_provided() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key_env=None,
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {},
            },
        }
    ]
    payload = adapter._build_payload(
        messages=[{"role": "user", "content": "Hi"}],
        model="gpt-5.4",
        tools=tools,
        stream=False,
        extra={},
    )
    assert payload["tools"] == tools


def test_stream_options_added_for_streaming() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key_env=None,
    )
    payload = adapter._build_payload(
        messages=[{"role": "user", "content": "Hi"}],
        model="gpt-5.4",
        tools=None,
        stream=True,
        extra={},
    )
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


def test_extra_kwargs_merged_into_payload() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key_env=None,
    )
    payload = adapter._build_payload(
        messages=[{"role": "user", "content": "Hi"}],
        model="gpt-5.4",
        tools=None,
        stream=False,
        extra={"temperature": 0.5, "top_p": 0.9},
    )
    assert payload["temperature"] == 0.5
    assert payload["top_p"] == 0.9


def test_base_url_trailing_slash_stripped() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1/",
        api_key_env=None,
    )
    assert adapter._base_url == "https://api.openai.com/v1"


# ── Headers ──────────────────────────────────────────────


def test_headers_without_api_key() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://localhost",
        api_key_env=None,
    )
    headers = adapter._build_headers()
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_headers_with_api_key() -> None:
    """
    API key from connection_params is set in the Authorization header.
    """
    adapter = OpenAICompatibleAdapter(
        base_url="https://localhost",
    )
    headers = adapter._build_headers(api_key_override="sk-test-123")
    assert headers["Authorization"] == "Bearer sk-test-123"


# ── SSE parsing ──────────────────────────────────────────


def test_parse_sse_data_line() -> None:
    data = {"id": "chatcmpl-1", "choices": []}
    line = f"data: {json.dumps(data)}"
    result = _parse_sse_line(line)
    assert result == data


def test_parse_sse_done_sentinel() -> None:
    assert _parse_sse_line("data: [DONE]") is None


def test_parse_sse_non_data_line() -> None:
    assert _parse_sse_line("event: message") is None
    assert _parse_sse_line("") is None
    assert _parse_sse_line(": comment") is None


# ── Error body buffering ──────────────────────────────────


def test_stream_request_aread_called_before_raise_for_status() -> None:
    """
    ``_stream_request`` calls ``aread()`` before ``raise_for_status()`` on
    4xx/5xx responses so that ``exc.response.text`` is available when
    ``_classify_http_error`` formats the error message.

    Without ``aread()`` the body is lost when the streaming context manager
    closes, and the error message degrades to ``"<unreadable response body>"``.

    The test monkeypatches ``httpx.AsyncClient`` so no real HTTP call is
    made. The mock response starts with ``_content == b""`` (simulating an
    unread streaming response). A real ``aread()`` call populates
    ``_content``; if the code path skips it, the assertion fires.

    Failure meaning: if the assertion on ``aread_called`` fails, the fix
    has been reverted and bad-model 404s will again show
    ``"<unreadable response body>"``.
    """
    adapter = OpenAICompatibleAdapter(base_url="https://fake-host/v1", api_key_env=None)

    aread_called = False

    # Build a mock response that simulates a 404 streaming response whose
    # body has not yet been buffered (content is empty until aread() runs).
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404

    async def _fake_aread() -> bytes:
        nonlocal aread_called
        aread_called = True
        mock_response.content = b'{"error": "model not found"}'
        return mock_response.content

    mock_response.aread = _fake_aread
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=mock_response
    )

    # Build the mock context managers so ``async with client.stream(...)``
    # hands back our fake response.
    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    mock_client_ctx = AsyncMock()
    mock_client_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_ctx.__aexit__ = AsyncMock(return_value=False)

    async def _run() -> None:
        with patch("httpx.AsyncClient", return_value=mock_client_ctx):
            gen = adapter._stream_request(
                "https://fake-host/v1/chat/completions",
                {},
                {"model": "dummy", "stream": True, "messages": []},
            )
            try:
                async for _ in gen:
                    pass
            except httpx.HTTPStatusError:
                return
        raise AssertionError("Expected HTTPStatusError was not raised")

    asyncio.run(_run())

    assert aread_called, (
        "aread() was not called before raise_for_status(). "
        "The error body will be unreadable when classify_llm_error formats "
        "the message, producing '<unreadable response body>' instead of the "
        "actual provider error text."
    )


def test_stream_responses_decodes_utf8_split_across_chunks() -> None:
    """
    ``_stream_responses`` must decode the byte stream incrementally so a
    multi-byte UTF-8 character split across two ``aiter_bytes`` chunks is
    reassembled, not turned into U+FFFD replacement characters.

    ``httpx`` yields arbitrary network-sized byte chunks, so the two bytes of
    ``é`` (0xC3 0xA9) can land in different chunks. Decoding each chunk in
    isolation corrupts the character; an incremental decoder preserves it.

    Failure meaning: if the assertion fires, per-chunk decoding has been
    reintroduced and non-ASCII streamed output (accents, CJK, emoji) is being
    silently corrupted.
    """
    adapter = OpenAIAdapter(base_url="https://fake-host/v1")

    # SSE for one text delta containing 'é', split mid-character.
    sse = 'event: response.output_text.delta\ndata: {"delta": "café"}\n\n'.encode()
    split = sse.index(b"\xc3\xa9") + 1  # between the two bytes of 'é'
    chunks = [sse[:split], sse[split:]]

    async def _aiter_bytes():
        for c in chunks:
            yield c

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = _aiter_bytes

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    mock_client_ctx = AsyncMock()
    mock_client_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_ctx.__aexit__ = AsyncMock(return_value=False)

    async def _run() -> str:
        with patch("httpx.AsyncClient", return_value=mock_client_ctx):
            deltas = [
                event.delta
                async for event in adapter._stream_responses(
                    "https://fake-host/v1/responses",
                    {},
                    {"stream": True},
                )
                if isinstance(event, ResponseTextDeltaEvent)
            ]
        return "".join(deltas)

    assert asyncio.run(_run()) == "café"


# ── URL resolution ──────────────────────────────────────


def test_resolve_base_url_override_wins() -> None:
    from omnigent.llms.adapters.openai import _resolve_base_url

    assert (
        _resolve_base_url("https://custom.api/v1/", "https://default.api/v1")
        == "https://custom.api/v1"
    )


def test_resolve_base_url_falls_back_to_default() -> None:
    from omnigent.llms.adapters.openai import _resolve_base_url

    assert _resolve_base_url(None, "https://default.api/v1") == "https://default.api/v1"


def test_resolve_base_url_raises_when_both_none() -> None:
    from omnigent.errors import OmnigentError
    from omnigent.llms.adapters.openai import _resolve_base_url

    with pytest.raises(OmnigentError, match="base_url"):
        _resolve_base_url(None, None)


# ── Responses API tool conversion ───────────────────────


def test_to_responses_tools_flattens_chat_format() -> None:
    from omnigent.llms.adapters.openai import _to_responses_tools

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    result = _to_responses_tools(tools)
    assert len(result) == 1
    assert result[0] == {
        "type": "function",
        "name": "get_weather",
        "description": "Get the weather",
        "parameters": {"type": "object", "properties": {}},
    }


def test_to_responses_tools_passes_through_responses_format() -> None:
    from omnigent.llms.adapters.openai import _to_responses_tools

    tools = [
        {
            "type": "function",
            "name": "already_flat",
            "parameters": {},
        }
    ]
    result = _to_responses_tools(tools)
    assert result[0] is tools[0]


def test_to_responses_tools_no_description() -> None:
    from omnigent.llms.adapters.openai import _to_responses_tools

    tools = [
        {
            "type": "function",
            "function": {
                "name": "fn",
                "parameters": {},
            },
        }
    ]
    result = _to_responses_tools(tools)
    assert "description" not in result[0]


# ── Responses API output parsing ────────────────────────


def test_parse_responses_output_message() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_output
    from omnigent.llms.types import MessageOutput

    items = [
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "Hello!"}],
        }
    ]
    output = _parse_responses_output(items)
    assert len(output) == 1
    assert isinstance(output[0], MessageOutput)
    assert output[0].content[0].text == "Hello!"


def test_parse_responses_output_function_call() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_output
    from omnigent.llms.types import FunctionCallOutput

    items = [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city": "London"}',
        }
    ]
    output = _parse_responses_output(items)
    assert len(output) == 1
    assert isinstance(output[0], FunctionCallOutput)
    assert output[0].call_id == "call_1"
    assert output[0].name == "get_weather"


def test_parse_responses_output_native_tool() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_output
    from omnigent.llms.types import NativeToolOutput

    items = [
        {
            "type": "web_search_call",
            "id": "ws_1",
            "status": "completed",
        }
    ]
    output = _parse_responses_output(items)
    assert len(output) == 1
    assert isinstance(output[0], NativeToolOutput)
    assert output[0].data["type"] == "web_search_call"


def test_parse_responses_output_reasoning_item() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_output
    from omnigent.llms.types import NativeToolOutput

    items = [
        {
            "type": "reasoning",
            "content": [{"type": "text", "text": "thinking..."}],
        }
    ]
    output = _parse_responses_output(items)
    assert len(output) == 1
    assert isinstance(output[0], NativeToolOutput)


def test_parse_responses_output_ignores_unknown_type() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_output

    items = [{"type": "unknown_future_type", "data": "something"}]
    output = _parse_responses_output(items)
    assert len(output) == 0


# ── Responses API response parsing ──────────────────────


def test_parse_responses_response_full() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_response
    from omnigent.llms.types import MessageOutput, Usage

    data = {
        "model": "gpt-5.4",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hi"}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    }
    resp = _parse_responses_response(data)
    assert resp.model == "gpt-5.4"
    assert isinstance(resp.output[0], MessageOutput)
    assert resp.usage == Usage(input_tokens=10, output_tokens=5, total_tokens=15)


def test_parse_responses_response_missing_model_raises() -> None:
    from omnigent.errors import OmnigentError
    from omnigent.llms.adapters.openai import _parse_responses_response

    with pytest.raises(OmnigentError, match="model"):
        _parse_responses_response({"output": []})


def test_parse_responses_response_no_usage() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_response

    data = {"model": "gpt-5.4", "output": []}
    resp = _parse_responses_response(data)
    assert resp.usage is None


# ── Responses API SSE event parsing ─────────────────────


def test_parse_responses_event_text_delta() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event
    from omnigent.llms.types import ResponseTextDeltaEvent

    event = _parse_responses_event("response.output_text.delta", {"delta": "Hello"})
    assert isinstance(event, ResponseTextDeltaEvent)
    assert event.delta == "Hello"


def test_parse_responses_event_reasoning_delta() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event
    from omnigent.llms.types import ResponseReasoningTextDeltaEvent

    event = _parse_responses_event("response.reasoning_text.delta", {"delta": "thinking"})
    assert isinstance(event, ResponseReasoningTextDeltaEvent)
    assert event.delta == "thinking"


def test_parse_responses_event_reasoning_summary_delta() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event
    from omnigent.llms.types import ResponseReasoningSummaryTextDeltaEvent

    event = _parse_responses_event("response.reasoning_summary_text.delta", {"delta": "summary"})
    assert isinstance(event, ResponseReasoningSummaryTextDeltaEvent)
    assert event.delta == "summary"


def test_parse_responses_event_reasoning_started() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event
    from omnigent.llms.types import ResponseReasoningStartedEvent

    event = _parse_responses_event("response.output_item.added", {"item": {"type": "reasoning"}})
    assert isinstance(event, ResponseReasoningStartedEvent)


def test_parse_responses_event_native_tool_done() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event
    from omnigent.llms.types import NativeToolOutputAddedEvent

    event = _parse_responses_event(
        "response.output_item.done",
        {"item": {"type": "web_search_call", "id": "ws_1", "status": "completed"}},
    )
    assert isinstance(event, NativeToolOutputAddedEvent)
    assert event.item["type"] == "web_search_call"


def test_parse_responses_event_reasoning_done() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event
    from omnigent.llms.types import NativeToolOutputAddedEvent

    event = _parse_responses_event(
        "response.output_item.done",
        {"item": {"type": "reasoning", "content": []}},
    )
    assert isinstance(event, NativeToolOutputAddedEvent)


def test_parse_responses_event_completed() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event
    from omnigent.llms.types import ResponseCompletedEvent

    event = _parse_responses_event(
        "response.completed",
        {
            "response": {
                "model": "gpt-5.4",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Done"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        },
    )
    assert isinstance(event, ResponseCompletedEvent)
    assert event.response.model == "gpt-5.4"


def test_parse_responses_event_unknown_returns_none() -> None:
    from omnigent.llms.adapters.openai import _parse_responses_event

    assert _parse_responses_event("response.some_future_event", {}) is None


def test_parse_responses_event_non_reasoning_item_added_returns_none() -> None:
    """output_item.added for non-reasoning types returns None."""
    from omnigent.llms.adapters.openai import _parse_responses_event

    event = _parse_responses_event("response.output_item.added", {"item": {"type": "message"}})
    assert event is None


def test_parse_responses_event_non_native_item_done_returns_none() -> None:
    """output_item.done for non-native types returns None."""
    from omnigent.llms.adapters.openai import _parse_responses_event

    event = _parse_responses_event("response.output_item.done", {"item": {"type": "message"}})
    assert event is None


def test_streamed_400_overflow_classified_as_context_window_exceeded(
    serve_streamed_response,
) -> None:
    """
    A streamed HTTP 400 buffers the error body before raising so
    ``classify_llm_error`` can detect context-window overflow.

    End-to-end companion to the ``aread()`` test above: a genuine
    OpenAI overflow body must survive the streaming error path and
    classify as ``ContextWindowExceededError`` (not a plain
    ``PermanentLLMError``) so the workflow can compact and retry.

    Failure meaning: the ``aread()`` guard has been removed and
    streaming OpenAI overflow errors no longer trigger compaction.
    """
    import asyncio

    import httpx

    from omnigent.llms.errors import ContextWindowExceededError
    from omnigent.runtime.llm_retry import classify_llm_error

    adapter = OpenAICompatibleAdapter(base_url="https://fake-host/v1", api_key_env=None)
    overflow_body = json.dumps(
        {
            "error": {
                "message": (
                    "This model's maximum context length is 128000 tokens."
                    " However, you requested 131015 tokens (127015 in the"
                    " messages, 4000 in the completion). Please reduce the"
                    " length of the messages or completion."
                ),
                "type": "invalid_request_error",
                "param": "messages",
                "code": "context_length_exceeded",
            }
        }
    ).encode()
    serve_streamed_response(400, overflow_body)

    async def _run() -> Exception:
        gen = adapter._stream_request(
            "https://fake-host/v1/chat/completions",
            {},
            {"model": "dummy", "stream": True, "messages": []},
        )
        try:
            async for _ in gen:
                pass
        except httpx.HTTPStatusError as exc:
            return classify_llm_error(exc, [429, 500, 502, 503])
        raise AssertionError("Expected HTTPStatusError was not raised")

    err = asyncio.run(_run())

    assert isinstance(err, ContextWindowExceededError)
    assert err.max_context_tokens == 128000
    assert err.actual_tokens == 131015
    assert err.detail is not None
    assert "maximum context length" in (err.detail.response_body or "")
