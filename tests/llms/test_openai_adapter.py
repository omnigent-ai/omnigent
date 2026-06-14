"""Tests for llms.adapters.openai — payload building and SSE parsing."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

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
