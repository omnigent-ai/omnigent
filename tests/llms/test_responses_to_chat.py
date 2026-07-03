"""Tests for llms._responses_to_chat — bidirectional translation."""

from typing import Any

import pytest

from omnigent.llms._responses_to_chat import (
    _translate_block,
    _translate_content,
    chat_response_to_response,
    chat_stream_to_response_events,
    responses_input_to_chat_messages,
)
from omnigent.llms.types import (
    FunctionCallOutput,
    MessageOutput,
    Response,
    ResponseCompletedEvent,
    ResponseReasoningStartedEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
    Usage,
)

# ── Input direction: Responses API -> Chat Completions ────


def test_instructions_become_system_message() -> None:
    messages = responses_input_to_chat_messages([], "Be helpful.")
    assert messages == [{"role": "system", "content": "Be helpful."}]


def test_no_instructions_no_system_message() -> None:
    items = [{"role": "user", "content": "Hi"}]
    messages = responses_input_to_chat_messages(items, None)
    assert messages == [{"role": "user", "content": "Hi"}]


def test_user_and_assistant_messages_passthrough() -> None:
    items = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    messages = responses_input_to_chat_messages(items, None)
    assert messages == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]


def test_function_call_items_grouped_into_assistant_message() -> None:
    items = [
        {"role": "user", "content": "What's the weather?"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city": "London"}',
        },
        {
            "type": "function_call",
            "call_id": "call_2",
            "name": "get_time",
            "arguments": '{"tz": "UTC"}',
        },
    ]
    messages = responses_input_to_chat_messages(items, None)
    assert len(messages) == 2
    assert messages[0] == {"role": "user", "content": "What's the weather?"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] is None
    assert len(messages[1]["tool_calls"]) == 2
    assert messages[1]["tool_calls"][0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "London"}'},
    }


def test_function_call_output_becomes_tool_message() -> None:
    items = [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "Sunny, 22C",
        },
    ]
    messages = responses_input_to_chat_messages(items, None)
    # First: assistant with tool_calls, second: tool message
    assert len(messages) == 2
    assert messages[1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Sunny, 22C",
    }


def test_full_conversation_round_trip() -> None:
    """
    Test a realistic conversation: user -> assistant tool call ->
    tool output -> assistant follow-up.
    """
    items = [
        {"role": "user", "content": "Weather in London?"},
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "get_weather",
            "arguments": '{"city": "London"}',
        },
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": "Rainy, 15C",
        },
        {"role": "assistant", "content": "It's rainy and 15C in London."},
    ]
    messages = responses_input_to_chat_messages(items, "Be concise.")
    assert len(messages) == 5
    assert messages[0] == {"role": "system", "content": "Be concise."}
    assert messages[1] == {"role": "user", "content": "Weather in London?"}
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["id"] == "c1"
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "Rainy, 15C",
    }
    assert messages[4] == {
        "role": "assistant",
        "content": "It's rainy and 15C in London.",
    }


# ── Content block translation (multimodal) ─────────────────


def test_text_only_content_blocks_collapse_to_string() -> None:
    """
    When all content blocks are text, _translate_content collapses
    them to a plain string — avoids sending content arrays to
    providers that only support string content for text-only messages.
    """
    content = [
        {"type": "input_text", "text": "Hello"},
        {"type": "input_text", "text": "World"},
    ]
    result = _translate_content(content)
    # Collapsed to a single joined string, not a list.
    assert result == "Hello\nWorld"


def test_single_text_block_collapses_to_string() -> None:
    """
    Even a single text block collapses to a plain string — this is
    the common case and ensures backward compatibility with providers
    that expect string content.
    """
    content = [{"type": "input_text", "text": "Hello"}]
    assert _translate_content(content) == "Hello"


def test_string_content_passes_through() -> None:
    """
    Plain string content (legacy path) passes through unchanged.
    """
    assert _translate_content("Hello") == "Hello"


def test_none_content_passes_through() -> None:
    """
    None content (e.g. assistant messages with only tool_calls)
    passes through unchanged.
    """
    assert _translate_content(None) is None


def test_image_block_translated_to_chat_format() -> None:
    """
    input_image with image_url translates to Chat Completions
    image_url format with nested url field.
    """
    block = {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc123",
        "detail": "high",
    }
    result = _translate_block(block)
    assert result == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,abc123",
            "detail": "high",
        },
    }


def test_image_block_without_detail() -> None:
    """
    input_image without detail field omits detail from output —
    we don't invent a default.
    """
    block = {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc123",
    }
    result = _translate_block(block)
    assert result == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,abc123"},
    }
    # detail must NOT be present — no invented default.
    assert "detail" not in result["image_url"]


def test_input_file_block_passes_through() -> None:
    """
    input_file blocks pass through as-is — provider adapters
    (Phase 3) are responsible for translating to native format.
    """
    block = {
        "type": "input_file",
        "file_data": "data:application/pdf;base64,JVBERi0xLjQK",
        "filename": "report.pdf",
    }
    result = _translate_block(block)
    # Passed through unchanged — same object.
    assert result is block


def test_unknown_block_type_passes_through() -> None:
    """
    Unrecognized block types (e.g. input_audio) pass through
    as-is for forward compatibility.
    """
    block = {"type": "input_audio", "file_data": "base64data"}
    result = _translate_block(block)
    assert result is block


def test_mixed_text_and_image_stays_as_list() -> None:
    """
    When content contains both text and non-text blocks, the
    result is a list (not collapsed to string) so multimodal
    content reaches the provider.
    """
    content = [
        {"type": "input_text", "text": "What's in this image?"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,abc",
            "detail": "auto",
        },
    ]
    result = _translate_content(content)
    assert isinstance(result, list)
    # Two translated blocks: text + image_url.
    assert len(result) == 2
    assert result[0] == {"type": "text", "text": "What's in this image?"}
    assert result[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,abc", "detail": "auto"},
    }


def test_multimodal_content_in_full_message() -> None:
    """
    End-to-end: a user message with content blocks flows through
    responses_input_to_chat_messages with correct translation.
    """
    items = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,img",
                },
            ],
        },
    ]
    messages = responses_input_to_chat_messages(items, None)
    # One user message with translated content blocks.
    assert len(messages) == 1
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Describe this"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,img"},
    }


@pytest.mark.parametrize(
    ("block_type", "text_key"),
    [
        pytest.param("input_text", "text", id="input_text"),
        pytest.param("output_text", "text", id="output_text"),
        pytest.param("text", "text", id="bare_text"),
    ],
)
def test_all_text_block_types_translate(
    block_type: str,
    text_key: str,
) -> None:
    """
    All recognized text block types (input_text, output_text, text)
    translate to Chat Completions ``{"type": "text", ...}`` format.
    """
    block = {"type": block_type, text_key: "Hello"}
    result = _translate_block(block)
    assert result == {"type": "text", "text": "Hello"}


# ── Output direction: Chat Completions -> Responses API ───


def test_chat_text_response_to_response() -> None:
    chat_dict = {
        "id": "chatcmpl-123",
        "model": "gpt-5.4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello!",
                    "tool_calls": None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
    resp = chat_response_to_response(chat_dict)
    assert isinstance(resp, Response)
    assert resp.model == "gpt-5.4"
    assert len(resp.output) == 1
    assert isinstance(resp.output[0], MessageOutput)
    assert resp.output[0].content[0].text == "Hello!"
    assert resp.usage == Usage(input_tokens=10, output_tokens=5, total_tokens=15)


def test_chat_tool_calls_to_response() -> None:
    chat_dict = {
        "id": "chatcmpl-456",
        "model": "gpt-5.4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "London"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": None,
    }
    resp = chat_response_to_response(chat_dict)
    assert len(resp.output) == 1
    assert isinstance(resp.output[0], FunctionCallOutput)
    assert resp.output[0].call_id == "call_abc"
    assert resp.output[0].name == "get_weather"
    assert resp.output[0].arguments == '{"city": "London"}'


def test_chat_mixed_text_and_tool_calls() -> None:
    chat_dict = {
        "model": "gpt-5.4",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Let me check.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"q": "test"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    resp = chat_response_to_response(chat_dict)
    assert len(resp.output) == 2
    assert isinstance(resp.output[0], MessageOutput)
    assert isinstance(resp.output[1], FunctionCallOutput)


# ── Streaming: Chat Completions chunks -> events ──────────


async def _aiter(
    items: list[dict[str, Any]],
) -> Any:
    """
    Wrap a list as an async iterator for testing.

    :param items: Items to yield.
    """
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_streaming_text_deltas() -> None:
    chunks = [
        {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = [e async for e in chat_stream_to_response_events(_aiter(chunks), model="test")]

    # Two text deltas + one completed event
    assert len(events) == 3
    assert isinstance(events[0], ResponseTextDeltaEvent)
    assert events[0].delta == "Hello"
    assert isinstance(events[1], ResponseTextDeltaEvent)
    assert events[1].delta == " world"
    assert isinstance(events[2], ResponseCompletedEvent)
    assert events[2].response.output[0].content[0].text == "Hello world"


@pytest.mark.asyncio
async def test_streaming_tool_calls() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"city":'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"London"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = [e async for e in chat_stream_to_response_events(_aiter(chunks), model="test")]

    # No text deltas, just the completed event
    completed = events[-1]
    assert isinstance(completed, ResponseCompletedEvent)
    fc = completed.response.output[0]
    assert isinstance(fc, FunctionCallOutput)
    assert fc.call_id == "call_1"
    assert fc.name == "get_weather"
    assert fc.arguments == '{"city":"London"}'


@pytest.mark.asyncio
async def test_streaming_with_usage() -> None:
    chunks = [
        {"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "total_tokens": 6,
            },
        },
    ]
    events = [e async for e in chat_stream_to_response_events(_aiter(chunks), model="test")]
    completed = events[-1]
    assert isinstance(completed, ResponseCompletedEvent)
    assert completed.response.usage == Usage(input_tokens=5, output_tokens=1, total_tokens=6)


# ── Kimi / list-content streaming ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_list_content_text_only() -> None:
    """
    When ``delta.content`` is a list of text blocks (Kimi-style), text is
    extracted and emitted as ``ResponseTextDeltaEvent`` — no reasoning events.
    """
    chunks = [
        {
            "choices": [
                {
                    "delta": {"content": [{"type": "text", "text": "Hello"}]},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"content": [{"type": "text", "text": " world"}]},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = [e async for e in chat_stream_to_response_events(_aiter(chunks), model="kimi")]

    text_events = [e for e in events if isinstance(e, ResponseTextDeltaEvent)]
    reasoning_events = [e for e in events if isinstance(e, ResponseReasoningTextDeltaEvent)]
    completed = events[-1]

    assert [e.delta for e in text_events] == ["Hello", " world"]
    assert reasoning_events == []
    assert isinstance(completed, ResponseCompletedEvent)
    assert completed.response.output[0].content[0].text == "Hello world"


@pytest.mark.asyncio
async def test_kimi_reasoning_then_text() -> None:
    """
    Kimi prefixes answers with reasoning blocks. Reasoning deltas become
    ``ResponseReasoningStartedEvent`` + ``ResponseReasoningTextDeltaEvent``
    and are NOT included in the final message text.
    """
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "content": [
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": "Let me think..."}],
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"content": [{"type": "text", "text": "The answer is 42."}]},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = [e async for e in chat_stream_to_response_events(_aiter(chunks), model="kimi")]

    reasoning_started = [e for e in events if isinstance(e, ResponseReasoningStartedEvent)]
    reasoning_deltas = [e for e in events if isinstance(e, ResponseReasoningTextDeltaEvent)]
    text_deltas = [e for e in events if isinstance(e, ResponseTextDeltaEvent)]
    completed = events[-1]

    # One reasoning.started sentinel, one reasoning delta with the thinking text.
    assert len(reasoning_started) == 1
    assert len(reasoning_deltas) == 1
    assert reasoning_deltas[0].delta == "Let me think..."

    # Reasoning is hidden from the main answer text.
    assert len(text_deltas) == 1
    assert text_deltas[0].delta == "The answer is 42."

    assert isinstance(completed, ResponseCompletedEvent)
    # Only the answer is in the response output — reasoning is NOT included.
    assert completed.response.output[0].content[0].text == "The answer is 42."


@pytest.mark.asyncio
async def test_kimi_reasoning_started_emitted_once_per_run() -> None:
    """
    ``ResponseReasoningStartedEvent`` is emitted only once even when
    multiple consecutive reasoning chunks arrive.
    """
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "content": [
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": "part 1"}],
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "content": [
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": " part 2"}],
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = [e async for e in chat_stream_to_response_events(_aiter(chunks), model="kimi")]

    reasoning_started = [e for e in events if isinstance(e, ResponseReasoningStartedEvent)]
    reasoning_deltas = [e for e in events if isinstance(e, ResponseReasoningTextDeltaEvent)]

    assert len(reasoning_started) == 1
    assert [e.delta for e in reasoning_deltas] == ["part 1", " part 2"]


@pytest.mark.asyncio
async def test_grok_top_level_reasoning_content_streams() -> None:
    """
    xAI Grok / DeepSeek emit chain-of-thought as a top-level
    ``delta.reasoning_content`` string (with ``content`` null during the
    thinking phase), not as typed blocks nested inside ``content``. It must
    surface as ``ResponseReasoningStartedEvent`` + ``ResponseReasoningTextDeltaEvent``
    and stay out of the final answer text.
    """
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "Let me think..."}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": " still thinking"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "42"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = [
        e async for e in chat_stream_to_response_events(_aiter(chunks), model="xai/grok-4.3")
    ]

    reasoning_started = [e for e in events if isinstance(e, ResponseReasoningStartedEvent)]
    reasoning_deltas = [e for e in events if isinstance(e, ResponseReasoningTextDeltaEvent)]
    text_deltas = [e for e in events if isinstance(e, ResponseTextDeltaEvent)]
    completed = events[-1]

    # One reasoning.started sentinel; both reasoning deltas surfaced in order.
    assert len(reasoning_started) == 1
    assert [e.delta for e in reasoning_deltas] == ["Let me think...", " still thinking"]

    # Reasoning is hidden from the main answer text.
    assert len(text_deltas) == 1
    assert text_deltas[0].delta == "42"

    assert isinstance(completed, ResponseCompletedEvent)
    assert completed.response.output[0].content[0].text == "42"


# ── _extract_delta_content unit tests ──────────────────────────────


def test_extract_delta_content_plain_string() -> None:
    """Plain string content returns (text, empty_reasoning)."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    text, reasoning = _extract_delta_content("Hello")
    assert text == "Hello"
    assert reasoning == ""


def test_extract_delta_content_non_string_non_list() -> None:
    """Non-string, non-list content returns empty strings."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    text, reasoning = _extract_delta_content(42)  # type: ignore[arg-type]
    assert text == ""
    assert reasoning == ""


def test_extract_delta_content_list_with_text_blocks() -> None:
    """List of text blocks extracts text."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    content = [{"type": "text", "text": "Hello"}, {"type": "text", "text": " world"}]
    text, reasoning = _extract_delta_content(content)
    assert text == "Hello world"
    assert reasoning == ""


def test_extract_delta_content_list_with_output_text_blocks() -> None:
    """output_text blocks also count as text."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    content = [{"type": "output_text", "text": "Hello"}]
    text, _reasoning = _extract_delta_content(content)
    assert text == "Hello"


def test_extract_delta_content_list_with_reasoning_blocks() -> None:
    """Reasoning blocks extract summary text into reasoning output."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    content = [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "thinking..."}],
        }
    ]
    text, reasoning = _extract_delta_content(content)
    assert text == ""
    assert reasoning == "thinking..."


def test_extract_delta_content_list_with_bare_strings() -> None:
    """Bare strings in the list are treated as text."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    content = ["Hello", " world"]
    text, _reasoning = _extract_delta_content(content)
    assert text == "Hello world"


def test_extract_delta_content_list_skips_non_dict_non_string() -> None:
    """Non-dict, non-string items in the list are skipped."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    content = [42, {"type": "text", "text": "ok"}]
    text, _reasoning = _extract_delta_content(content)
    assert text == "ok"


def test_extract_delta_content_reasoning_without_summary() -> None:
    """Reasoning block without summary key yields no reasoning text."""
    from omnigent.llms._responses_to_chat import _extract_delta_content

    content = [{"type": "reasoning"}]
    _text, reasoning = _extract_delta_content(content)
    assert reasoning == ""


# ── _extract_usage unit tests ──────────────────────────────────────


def test_extract_usage_returns_none_for_none() -> None:
    from omnigent.llms._responses_to_chat import _extract_usage

    assert _extract_usage(None) is None


def test_extract_usage_returns_none_for_empty_dict() -> None:
    from omnigent.llms._responses_to_chat import _extract_usage

    assert _extract_usage({}) is None


def test_extract_usage_maps_fields() -> None:
    from omnigent.llms._responses_to_chat import _extract_usage

    usage = _extract_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert usage is not None
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    assert usage.total_tokens == 15


# ── Streaming: usage-only final chunk ──────────────────────────────


@pytest.mark.asyncio
async def test_streaming_usage_only_chunk() -> None:
    """A trailing chunk with only usage (no choices) captures the usage."""
    chunks = [
        {"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            }
        },
    ]
    events = [e async for e in chat_stream_to_response_events(_aiter(chunks), model="test")]
    completed = events[-1]
    assert isinstance(completed, ResponseCompletedEvent)
    assert completed.response.usage == Usage(input_tokens=10, output_tokens=2, total_tokens=12)


# ── Trailing tool calls flushed ────────────────────────────────────


def test_trailing_function_calls_flushed() -> None:
    """Function call items at the end of input are flushed into assistant msg."""
    items = [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "fn",
            "arguments": "{}",
        },
    ]
    messages = responses_input_to_chat_messages(items, None)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert len(messages[0]["tool_calls"]) == 1
