"""Tests for llms.adapters.anthropic — translation logic."""

import base64
import json

import pytest

from omnigent.llms.adapters.anthropic import (
    _anthropic_to_chat,
    _chat_to_anthropic,
    _convert_tool_choice,
    _convert_tools,
    _translate_part_to_anthropic,
)


def test_system_messages_extracted() -> None:
    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "user", "content": "Hi"},
    ]
    payload = _chat_to_anthropic(messages, "claude-test", None, {})
    assert payload["system"] == "Be helpful."
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"


def test_multiple_system_messages_joined() -> None:
    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Hi"},
    ]
    payload = _chat_to_anthropic(messages, "claude-test", None, {})
    assert payload["system"] == "Be helpful.\nBe concise."


def test_assistant_tool_calls_converted() -> None:
    messages = [
        {"role": "user", "content": "Weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "London"}',
                    },
                }
            ],
        },
    ]
    payload = _chat_to_anthropic(messages, "claude-test", None, {})
    assistant_msg = payload["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"][0]["type"] == "tool_use"
    assert assistant_msg["content"][0]["id"] == "call_1"
    assert assistant_msg["content"][0]["input"] == {"city": "London"}


def test_tool_messages_converted_to_tool_result() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Sunny, 22C",
        }
    ]
    payload = _chat_to_anthropic(messages, "claude-test", None, {})
    msg = payload["messages"][0]
    assert msg["role"] == "user"
    assert msg["content"][0]["type"] == "tool_result"
    assert msg["content"][0]["tool_use_id"] == "call_1"


def test_temperature_halved() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {"temperature": 1.0})
    assert payload["temperature"] == 0.5


def test_default_max_tokens() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {})
    assert payload["max_tokens"] == 16384


def test_tools_converted() -> None:
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
    result = _convert_tools(tools)
    assert len(result) == 1
    assert result[0] == {
        "name": "get_weather",
        "description": "Get weather",
        "input_schema": {"type": "object", "properties": {}},
    }


@pytest.mark.parametrize(
    ("openai_choice", "expected"),
    [
        ("none", {"type": "none"}),
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        (
            {"type": "function", "function": {"name": "foo"}},
            {"type": "tool", "name": "foo"},
        ),
    ],
)
def test_tool_choice_mapping(
    openai_choice: str | dict,
    expected: dict,
) -> None:
    assert _convert_tool_choice(openai_choice) == expected


def test_anthropic_text_response_to_chat() -> None:
    resp = {
        "id": "msg_123",
        "model": "claude-test",
        "content": [{"type": "text", "text": "Hello!"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    chat = _anthropic_to_chat(resp)
    assert chat["model"] == "claude-test"
    assert chat["choices"][0]["message"]["content"] == "Hello!"
    assert chat["choices"][0]["finish_reason"] == "stop"
    assert chat["usage"]["prompt_tokens"] == 10
    assert chat["usage"]["completion_tokens"] == 5


def test_anthropic_tool_use_response_to_chat() -> None:
    resp = {
        "id": "msg_456",
        "model": "claude-test",
        "content": [
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "get_weather",
                "input": {"city": "London"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    chat = _anthropic_to_chat(resp)
    tool_calls = chat["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "tu_1"
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"city": "London"}
    assert chat["choices"][0]["finish_reason"] == "tool_calls"


def test_anthropic_max_tokens_stop_reason() -> None:
    resp = {
        "id": "msg_789",
        "model": "claude-test",
        "content": [{"type": "text", "text": "Truncat"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 5, "output_tokens": 100},
    }
    chat = _anthropic_to_chat(resp)
    assert chat["choices"][0]["finish_reason"] == "length"


# ── Multimodal content translation ──────────────────────


def test_user_message_with_image_data_uri() -> None:
    """
    User message with image_url data URI translates to Anthropic
    base64 image source.
    """
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,abc123"},
                },
            ],
        },
    ]
    payload = _chat_to_anthropic(messages, "claude-test", None, {})
    content = payload["messages"][0]["content"]
    # Two blocks: text + image.
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": "Describe this"}
    assert content[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "abc123",
        },
    }


def test_user_message_with_external_url() -> None:
    """
    External image URL translates to Anthropic URL source type.
    """
    part = {
        "type": "image_url",
        "image_url": {"url": "https://example.com/photo.png"},
    }
    result = _translate_part_to_anthropic(part)
    assert result == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.com/photo.png"},
    }


def test_user_message_with_file_data() -> None:
    """
    input_file with file_data translates to Anthropic document type.
    """
    part = {
        "type": "input_file",
        "file_data": "data:application/pdf;base64,JVBERi0xLjQK",
    }
    result = _translate_part_to_anthropic(part)
    assert result == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": "JVBERi0xLjQK",
        },
    }


def test_user_message_with_file_data_text_markdown() -> None:
    """
    input_file with a text/markdown MIME uses Anthropic's "text" source
    type (decoded UTF-8 string), not "base64".

    Anthropic's document block only accepts application/pdf for the
    base64 source type; all other text-like files must use source.type
    "text" with the decoded content.  Sending text/markdown as base64
    triggers a 400: "Input should be 'application/pdf'".
    """
    md_content = "# Hello\nThis is **markdown**."
    encoded = base64.b64encode(md_content.encode()).decode()
    part = {
        "type": "input_file",
        "file_data": f"data:text/markdown;base64,{encoded}",
    }
    result = _translate_part_to_anthropic(part)
    assert result == {
        "type": "document",
        "source": {
            "type": "text",
            "media_type": "text/plain",
            "data": md_content,
        },
    }


def test_user_message_with_file_data_text_plain() -> None:
    """
    input_file with text/plain MIME also uses the "text" source type.

    Same rule as markdown: only application/pdf goes through base64.
    """
    txt_content = "plain text content"
    encoded = base64.b64encode(txt_content.encode()).decode()
    part = {
        "type": "input_file",
        "file_data": f"data:text/plain;base64,{encoded}",
    }
    result = _translate_part_to_anthropic(part)
    assert result == {
        "type": "document",
        "source": {
            "type": "text",
            "media_type": "text/plain",
            "data": txt_content,
        },
    }


def test_string_user_content_passes_through() -> None:
    """
    String user content passes through unchanged — no translation
    needed for text-only messages.
    """
    messages = [{"role": "user", "content": "Hello"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {})
    # String content passed through as-is.
    assert payload["messages"][0]["content"] == "Hello"


# ── Header building ──────────────────────────────────────


def test_build_headers_with_api_key() -> None:
    """API key is set in the x-api-key header."""
    from omnigent.llms.adapters.anthropic import _build_headers

    headers = _build_headers(api_key_override="sk-test-123")
    assert headers["x-api-key"] == "sk-test-123"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["Content-Type"] == "application/json"


def test_build_headers_raises_without_api_key() -> None:
    """Missing API key raises OmnigentError."""
    from omnigent.errors import OmnigentError
    from omnigent.llms.adapters.anthropic import _build_headers

    with pytest.raises(OmnigentError, match="api_key"):
        _build_headers(api_key_override=None)


def test_build_headers_raises_for_empty_api_key() -> None:
    """Empty string API key raises OmnigentError."""
    from omnigent.errors import OmnigentError
    from omnigent.llms.adapters.anthropic import _build_headers

    with pytest.raises(OmnigentError, match="api_key"):
        _build_headers(api_key_override="")


# ── Reasoning effort ─────────────────────────────────────


def test_effort_to_budget_low() -> None:
    from omnigent.llms.adapters.anthropic import _effort_to_budget

    assert _effort_to_budget("low", 16384) == 1024


def test_effort_to_budget_medium() -> None:
    from omnigent.llms.adapters.anthropic import _effort_to_budget

    assert _effort_to_budget("medium", 16384) == 4096


def test_effort_to_budget_high() -> None:
    from omnigent.llms.adapters.anthropic import _effort_to_budget

    assert _effort_to_budget("high", 16384) == 8192


def test_effort_to_budget_low_clamped_to_max_tokens() -> None:
    """When max_tokens is less than the effort's budget, clamp to max_tokens."""
    from omnigent.llms.adapters.anthropic import _effort_to_budget

    assert _effort_to_budget("low", 512) == 512


def test_reasoning_effort_adds_thinking_to_payload() -> None:
    """reasoning_effort in extra adds thinking config to the payload."""
    messages = [{"role": "user", "content": "Hi"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {"reasoning_effort": "high"})
    assert payload["thinking"]["type"] == "enabled"
    assert payload["thinking"]["budget_tokens"] == 8192


# ── Stop sequences ───────────────────────────────────────


def test_stop_string_wrapped_in_list() -> None:
    """A single stop string is wrapped in a list."""
    messages = [{"role": "user", "content": "Hi"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {"stop": "END"})
    assert payload["stop_sequences"] == ["END"]


def test_stop_list_passed_through() -> None:
    """A list of stop sequences passes through unchanged."""
    messages = [{"role": "user", "content": "Hi"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {"stop": ["END", "STOP"]})
    assert payload["stop_sequences"] == ["END", "STOP"]


# ── Streaming SSE parsing ────────────────────────────────


@pytest.mark.asyncio
async def test_stream_to_chat_chunks_text_delta() -> None:
    """Text deltas in the SSE stream produce Chat Completions chunks."""
    from omnigent.llms.adapters.anthropic import _stream_to_chat_chunks

    lines = [
        "data: "
        + '{"type": "message_start", "message": {"id": "msg_1",'
        + ' "model": "claude-test",'
        + ' "usage": {"input_tokens": 10}}}',
        'data: {"type": "content_block_start", "content_block": {"type": "text"}}',
        'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}',
        'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " world"}}',
        "data: "
        + '{"type": "message_delta",'
        + ' "delta": {"stop_reason": "end_turn"},'
        + ' "usage": {"output_tokens": 5}}',
    ]

    async def _aiter():
        for line in lines:
            yield line

    chunks = [c async for c in _stream_to_chat_chunks(_aiter())]
    # Two text delta chunks + one final chunk with usage
    text_chunks = [c for c in chunks if c["choices"][0]["delta"].get("content")]
    assert len(text_chunks) == 2
    assert text_chunks[0]["choices"][0]["delta"]["content"] == "Hello"
    assert text_chunks[1]["choices"][0]["delta"]["content"] == " world"

    # Final chunk has usage
    final = chunks[-1]
    assert final["usage"]["prompt_tokens"] == 10
    assert final["usage"]["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_stream_to_chat_chunks_tool_use() -> None:
    """Tool use blocks in the SSE stream produce tool_calls in chunks."""
    from omnigent.llms.adapters.anthropic import _stream_to_chat_chunks

    lines = [
        "data: "
        + '{"type": "message_start", "message": {"id": "msg_2",'
        + ' "model": "claude-test",'
        + ' "usage": {"input_tokens": 5}}}',
        "data: "
        + '{"type": "content_block_start",'
        + ' "content_block": {"type": "tool_use",'
        + ' "id": "tu_1", "name": "get_weather"}}',
        "data: "
        + '{"type": "content_block_delta",'
        + ' "delta": {"type": "input_json_delta",'
        + ' "partial_json": "{\\"city\\":"}}',
        "data: "
        + '{"type": "content_block_delta",'
        + ' "delta": {"type": "input_json_delta",'
        + ' "partial_json": "\\"London\\"}"}}',
        "data: "
        + '{"type": "message_delta",'
        + ' "delta": {"stop_reason": "tool_use"},'
        + ' "usage": {"output_tokens": 10}}',
    ]

    async def _aiter():
        for line in lines:
            yield line

    chunks = [c async for c in _stream_to_chat_chunks(_aiter())]
    # First chunk: tool_call start with id and name
    tool_start = chunks[0]
    tc = tool_start["choices"][0]["delta"]["tool_calls"][0]
    assert tc["id"] == "tu_1"
    assert tc["function"]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_stream_skips_non_data_lines() -> None:
    """Non-data lines are silently skipped."""
    from omnigent.llms.adapters.anthropic import _stream_to_chat_chunks

    lines = [
        "event: message_start",
        "data: "
        + '{"type": "message_start", "message":'
        + ' {"id": "msg_3", "model": "claude-test",'
        + ' "usage": {}}}',
        ": comment line",
        "",
        "data: "
        + '{"type": "message_delta",'
        + ' "delta": {"stop_reason": "end_turn"},'
        + ' "usage": {"output_tokens": 1}}',
    ]

    async def _aiter():
        for line in lines:
            yield line

    chunks = [c async for c in _stream_to_chat_chunks(_aiter())]
    # Only the message_delta produces a chunk; message_start only sets metadata
    assert len(chunks) == 1


# ── Tool choice edge case ────────────────────────────────


def test_tool_choice_unknown_falls_back_to_auto() -> None:
    """Unknown tool_choice values fall back to auto."""
    assert _convert_tool_choice("unknown_value") == {"type": "auto"}


# ── Top P passthrough ────────────────────────────────────


def test_top_p_passed_through() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {"top_p": 0.9})
    assert payload["top_p"] == 0.9


# ── Non-function tools skipped ───────────────────────────


def test_non_function_tools_skipped() -> None:
    """Non-function tool types are filtered out."""
    tools = [
        {"type": "not_function", "whatever": {}},
        {
            "type": "function",
            "function": {
                "name": "real_fn",
                "parameters": {},
            },
        },
    ]
    result = _convert_tools(tools)
    assert len(result) == 1
    assert result[0]["name"] == "real_fn"


# ── Unrecognized content part passthrough ────────────────


def test_unrecognized_part_passes_through() -> None:
    """Unrecognized content part types pass through as-is."""
    part = {"type": "input_audio", "data": "base64data"}
    result = _translate_part_to_anthropic(part)
    assert result is part


# ── Max completion tokens alias ──────────────────────────


def test_max_completion_tokens_alias() -> None:
    """max_completion_tokens is an alias for max_tokens."""
    messages = [{"role": "user", "content": "Hi"}]
    payload = _chat_to_anthropic(messages, "claude-test", None, {"max_completion_tokens": 2048})
    assert payload["max_tokens"] == 2048
