"""Tests for llms.adapters.gemini — translation logic."""

import asyncio
import json

import httpx
import pytest

from omnigent.llms._responses_to_chat import (
    chat_stream_to_response_events,
    responses_input_to_chat_messages,
)
from omnigent.llms.adapters.gemini import (
    GeminiAdapter,
    _build_model_path,
    _chat_to_gemini,
    _convert_tools,
    _extract_usage,
    _gemini_stream_chunk_to_chat,
    _gemini_to_chat,
    _normalize_finish_reason,
    _translate_part_to_gemini,
)
from omnigent.llms.errors import ContextWindowExceededError, PermanentLLMError
from omnigent.llms.types import (
    FunctionCallOutput,
    MessageOutput,
    ResponseReasoningStartedEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
)
from omnigent.runtime.llm_retry import classify_llm_error

# ── Request translation ──────────────────────────────────


def test_system_messages_become_system_instruction() -> None:
    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "user", "content": "Hi"},
    ]
    payload = _chat_to_gemini(messages, None, {})
    assert payload["system_instruction"] == {"parts": [{"text": "Be helpful."}]}
    # System message should not appear in contents
    assert len(payload["contents"]) == 1
    assert payload["contents"][0]["role"] == "user"


def test_multiple_system_messages_joined_as_parts() -> None:
    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Hi"},
    ]
    payload = _chat_to_gemini(messages, None, {})
    assert payload["system_instruction"]["parts"] == [
        {"text": "Be helpful."},
        {"text": "Be concise."},
    ]


def test_assistant_role_remapped_to_model() -> None:
    messages = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ]
    payload = _chat_to_gemini(messages, None, {})
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][1]["role"] == "model"


def test_assistant_tool_calls_become_function_call_parts() -> None:
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
    payload = _chat_to_gemini(messages, None, {})
    model_msg = payload["contents"][1]
    assert model_msg["role"] == "model"
    fc_part = model_msg["parts"][0]
    assert fc_part["functionCall"]["name"] == "get_weather"
    assert fc_part["functionCall"]["args"] == {"city": "London"}


def test_tool_messages_become_function_response() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "_tool_name": "get_weather",
            "content": "Sunny, 22C",
        }
    ]
    payload = _chat_to_gemini(messages, None, {})
    msg = payload["contents"][0]
    assert msg["role"] == "user"
    fr = msg["parts"][0]["functionResponse"]
    assert fr["name"] == "get_weather"
    assert fr["response"] == {"result": "Sunny, 22C"}


def test_generation_config_keys_mapped() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    extra = {
        "temperature": 0.7,
        "max_tokens": 100,
        "top_p": 0.9,
        "stop": ["END"],
    }
    payload = _chat_to_gemini(messages, None, extra)
    gen = payload["generationConfig"]
    assert gen["temperature"] == 0.7
    assert gen["maxOutputTokens"] == 100
    assert gen["topP"] == 0.9
    assert gen["stopSequences"] == ["END"]


def test_tools_converted_to_function_declarations() -> None:
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
    assert result[0]["name"] == "get_weather"
    assert result[0]["description"] == "Get weather"
    assert result[0]["parameters"] == {"type": "object", "properties": {}}


def test_tools_payload_wraps_declarations() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "fn",
                "description": "d",
                "parameters": {},
            },
        }
    ]
    payload = _chat_to_gemini(messages, tools, {})
    assert "functionDeclarations" in payload["tools"][0]


# ── Response translation ─────────────────────────────────


def test_gemini_text_response_to_chat() -> None:
    resp = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Hello!"}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
    }
    chat = _gemini_to_chat(resp, "gemini-test")
    assert chat["model"] == "gemini-test"
    assert chat["choices"][0]["message"]["content"] == "Hello!"
    assert chat["choices"][0]["finish_reason"] == "stop"
    assert chat["usage"]["prompt_tokens"] == 10
    assert chat["usage"]["completion_tokens"] == 5


def test_gemini_function_call_response_to_chat() -> None:
    resp = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "London"},
                            }
                        }
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    chat = _gemini_to_chat(resp, "gemini-test")
    tool_calls = chat["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"].startswith("call_")
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"city": "London"}


def test_gemini_empty_candidates_returns_empty_response() -> None:
    resp = {"candidates": []}
    chat = _gemini_to_chat(resp, "gemini-test")
    assert chat["choices"][0]["message"]["content"] is None
    assert chat["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize(
    ("gemini_reason", "expected"),
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "safety"),
        (None, None),
    ],
)
def test_finish_reason_normalization(
    gemini_reason: str | None,
    expected: str | None,
) -> None:
    assert _normalize_finish_reason(gemini_reason) == expected


def test_usage_extraction() -> None:
    meta = {
        "promptTokenCount": 10,
        "candidatesTokenCount": 20,
        "totalTokenCount": 30,
    }
    usage = _extract_usage(meta)
    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }


# ── Multimodal content translation ──────────────────────


def test_user_message_with_image_data_uri() -> None:
    """
    User message with image_url data URI translates to Gemini
    inlineData part.
    """
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,/9j/abc"},
                },
            ],
        },
    ]
    payload = _chat_to_gemini(messages, None, {})
    parts = payload["contents"][0]["parts"]
    # Two parts: text + inlineData.
    assert len(parts) == 2
    assert parts[0] == {"text": "Describe this"}
    assert parts[1] == {
        "inlineData": {"mimeType": "image/jpeg", "data": "/9j/abc"},
    }


def test_user_message_with_external_url_becomes_text() -> None:
    """
    External URL falls back to text placeholder since Gemini
    does not support URL references in content parts.
    """
    part = {
        "type": "image_url",
        "image_url": {"url": "https://example.com/photo.png"},
    }
    result = _translate_part_to_gemini(part)
    assert result == {"text": "[image: https://example.com/photo.png]"}


def test_user_message_with_file_data() -> None:
    """
    input_file with file_data translates to Gemini inlineData.
    """
    part = {
        "type": "input_file",
        "file_data": "data:application/pdf;base64,JVBERi0xLjQK",
    }
    result = _translate_part_to_gemini(part)
    assert result == {
        "inlineData": {
            "mimeType": "application/pdf",
            "data": "JVBERi0xLjQK",
        },
    }


def test_string_user_content_becomes_text_part() -> None:
    """
    String user content becomes a single text part —
    backward compatibility with text-only messages.
    """
    messages = [{"role": "user", "content": "Hello"}]
    payload = _chat_to_gemini(messages, None, {})
    assert payload["contents"][0]["parts"] == [{"text": "Hello"}]


# ── Streaming ─────────────────────────────────────────────


def _parallel_function_call_chunk() -> dict:
    """A single Gemini stream chunk with two parallel function calls."""
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "get_weather", "args": {"city": "London"}}},
                        {"functionCall": {"name": "get_time", "args": {"tz": "UTC"}}},
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }


def test_stream_parallel_function_calls_get_distinct_indices() -> None:
    """
    Parallel ``functionCall`` parts in one chunk must each receive a
    distinct ``tool_calls`` index. A fixed index of 0 makes the downstream
    accumulator collapse them into one call with concatenated arguments.
    """
    chunks = list(_gemini_stream_chunk_to_chat(_parallel_function_call_chunk()))
    indices = [
        tc["index"]
        for chunk in chunks
        for choice in chunk["choices"]
        for tc in (choice["delta"].get("tool_calls") or [])
    ]
    assert indices == [0, 1]


async def test_stream_parallel_function_calls_survive_accumulation() -> None:
    """
    Two parallel Gemini function calls in a streamed response are assembled
    into two separate, uncorrupted ``FunctionCallOutput``s — matching what the
    non-streaming path produces for the same content.
    """

    async def _chunks():
        for chunk in _gemini_stream_chunk_to_chat(_parallel_function_call_chunk()):
            yield chunk

    events = [e async for e in chat_stream_to_response_events(_chunks(), model="gemini-test")]
    response = events[-1].response
    calls = [o for o in response.output if isinstance(o, FunctionCallOutput)]

    assert len(calls) == 2
    by_name = {c.name: c for c in calls}
    assert json.loads(by_name["get_weather"].arguments) == {"city": "London"}
    assert json.loads(by_name["get_time"].arguments) == {"tz": "UTC"}


# ── Streaming chunk translation ──────────────────────────


def test_gemini_stream_text_chunk() -> None:
    """A streaming chunk with text produces a Chat Completions text delta."""
    from omnigent.llms.adapters.gemini import _gemini_stream_chunk_to_chat

    data = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Hello"}], "role": "model"},
            }
        ],
    }
    chunks = list(_gemini_stream_chunk_to_chat(data))
    assert len(chunks) == 1
    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello"
    assert chunks[0]["choices"][0]["finish_reason"] is None


def test_gemini_stream_function_call_chunk() -> None:
    """A streaming chunk with functionCall produces a tool_calls delta."""
    from omnigent.llms.adapters.gemini import _gemini_stream_chunk_to_chat

    data = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "London"},
                            }
                        }
                    ],
                    "role": "model",
                },
            }
        ],
    }
    chunks = list(_gemini_stream_chunk_to_chat(data))
    assert len(chunks) == 1
    tc = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "London"}


def test_gemini_stream_finish_reason_chunk() -> None:
    """A streaming chunk with finishReason emits a separate finish chunk."""
    from omnigent.llms.adapters.gemini import _gemini_stream_chunk_to_chat

    data = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Done"}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
    }
    chunks = list(_gemini_stream_chunk_to_chat(data))
    # Text chunk + finish reason chunk
    assert len(chunks) == 2
    assert chunks[1]["choices"][0]["finish_reason"] == "stop"


def test_gemini_stream_usage_only_chunk() -> None:
    """A streaming chunk with no candidates but usageMetadata yields usage."""
    from omnigent.llms.adapters.gemini import _gemini_stream_chunk_to_chat

    data = {
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
    }
    chunks = list(_gemini_stream_chunk_to_chat(data))
    assert len(chunks) == 1
    assert chunks[0]["usage"]["prompt_tokens"] == 10
    assert chunks[0]["usage"]["completion_tokens"] == 5


def test_gemini_stream_empty_candidates_no_usage() -> None:
    """A streaming chunk with empty candidates and no usage yields nothing."""
    from omnigent.llms.adapters.gemini import _gemini_stream_chunk_to_chat

    chunks = list(_gemini_stream_chunk_to_chat({"candidates": []}))
    assert chunks == []


# ── Empty response ───────────────────────────────────────


def test_empty_chat_response_structure() -> None:
    """_empty_chat_response returns a well-formed empty response."""
    from omnigent.llms.adapters.gemini import _empty_chat_response

    resp = _empty_chat_response("gemini-test")
    assert resp["model"] == "gemini-test"
    assert resp["choices"][0]["message"]["content"] is None
    assert resp["choices"][0]["message"]["tool_calls"] is None
    assert resp["choices"][0]["finish_reason"] == "stop"


# ── None content becomes empty parts ────────────────────


def test_none_content_becomes_empty_parts() -> None:
    """None content (e.g. assistant with tool_calls only) yields empty parts."""
    from omnigent.llms.adapters.gemini import _content_to_gemini_parts

    assert _content_to_gemini_parts(None) == []


# ── Gemini headers ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_headers_with_api_key() -> None:
    """API key is set in x-goog-api-key header."""
    from omnigent.llms.adapters.gemini import GeminiAdapter

    adapter = GeminiAdapter()
    headers = await adapter._get_headers(api_key_override="test-key")
    assert headers["x-goog-api-key"] == "test-key"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_get_headers_raises_without_api_key() -> None:
    """Missing API key raises OmnigentError."""
    from omnigent.errors import OmnigentError
    from omnigent.llms.adapters.gemini import GeminiAdapter

    adapter = GeminiAdapter()
    with pytest.raises(OmnigentError, match="api_key"):
        await adapter._get_headers(api_key_override=None)


# ── Tool without description ─────────────────────────────


def test_tool_without_description_omits_description() -> None:
    """Tool declarations without description omit the field."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "fn",
                "parameters": {},
            },
        }
    ]
    result = _convert_tools(tools)
    assert "description" not in result[0]


# ── Non-function tools skipped ───────────────────────────


def test_non_function_tools_skipped() -> None:
    """Non-function tool types are filtered out."""
    tools = [
        {"type": "not_function", "whatever": {}},
        {"type": "function", "function": {"name": "fn", "parameters": {}}},
    ]
    result = _convert_tools(tools)
    assert len(result) == 1
    assert result[0]["name"] == "fn"


# ── Unrecognized part passthrough ────────────────────────


def test_unrecognized_part_passes_through() -> None:
    """Unrecognized content part types pass through as-is."""
    part = {"type": "input_audio", "data": "base64data"}
    result = _translate_part_to_gemini(part)
    assert result is part


# ── file_data without data URI raises ────────────────────


def test_file_data_without_data_uri_raises() -> None:
    """input_file without a data: URI prefix raises ValueError."""
    part = {
        "type": "input_file",
        "file_data": "https://example.com/file.pdf",
    }
    with pytest.raises(ValueError, match="data: URI"):
        _translate_part_to_gemini(part)


# ── Streaming error body buffering ───────────────────────


def test_streamed_400_overflow_classified_as_context_window_exceeded(
    serve_streamed_response,
) -> None:
    """
    A streamed HTTP 400 buffers the error body before raising so
    ``classify_llm_error`` can detect context-window overflow.

    Without the ``aread()`` guard in ``_stream_request`` the body of a
    streamed error response is never read; ``exc.response.text`` then
    raises ``ResponseNotRead``, degrades to
    ``"<unreadable response body>"``, and a genuine overflow 400 is
    misclassified as a plain ``PermanentLLMError`` — the workflow's
    compact-and-retry path never fires.

    Failure meaning: the guard has been removed and streaming Gemini
    (and Vertex, which inherits ``_stream_request``) overflow errors
    no longer trigger compaction.
    """
    overflow_body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": (
                    "The input token count (1194139) exceeds the maximum"
                    " number of tokens allowed (1048576)."
                ),
                "status": "INVALID_ARGUMENT",
            }
        }
    ).encode()
    serve_streamed_response(400, overflow_body)

    adapter = GeminiAdapter()

    async def _run() -> Exception:
        gen = adapter._stream_request(
            "https://fake-host/v1beta/models/gemini-test:streamGenerateContent",
            {},
            {"contents": []},
        )
        try:
            async for _ in gen:
                pass
        except httpx.HTTPStatusError as exc:
            return classify_llm_error(exc, [429, 500, 502, 503])
        raise AssertionError("Expected HTTPStatusError was not raised")

    err = asyncio.run(_run())

    assert isinstance(err, ContextWindowExceededError)
    assert err.max_context_tokens == 1048576
    assert err.actual_tokens == 1194139
    assert err.detail is not None
    assert "input token count" in (err.detail.response_body or "")


# ── Gemini 3.x thinking, thoughtSignature & multi-turn tools ──


def test_gemini_thought_signature_preserved_non_streaming() -> None:
    """``thoughtSignature`` on ``functionCall`` parts survives ``_gemini_to_chat``."""
    resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "fc_native_1",
                                "name": "get_weather",
                                "args": {"city": "London"},
                            },
                            "thoughtSignature": "sig_turn1_abc",
                        },
                        {
                            "functionCall": {
                                "name": "get_time",
                                "args": {"tz": "UTC"},
                            },
                        },
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    chat = _gemini_to_chat(resp, "gemini-3-pro-preview")
    tool_calls = chat["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 2
    assert tool_calls[0]["id"] == "fc_native_1"
    assert tool_calls[0]["thoughtSignature"] == "sig_turn1_abc"
    assert tool_calls[1]["id"].startswith("call_")
    assert "thoughtSignature" not in tool_calls[1]


def test_identical_parallel_function_calls_get_distinct_call_ids() -> None:
    """Identical parallel ``functionCall`` parts without native ids never share a ``call_id``."""
    resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "get_time", "args": {"tz": "UTC"}}},
                        {"functionCall": {"name": "get_time", "args": {"tz": "UTC"}}},
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    chat = _gemini_to_chat(resp, "gemini-3-pro-preview")
    tool_calls = chat["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 2
    assert tool_calls[0]["id"] != tool_calls[1]["id"]
    assert tool_calls[0]["id"].startswith("call_")
    assert tool_calls[1]["id"].startswith("call_")


def test_empty_and_none_text_parts_ignored_and_do_not_shadow_function_call() -> None:
    """Empty/None text parts neither corrupt ``content`` nor shadow ``functionCall``."""
    resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Hello"},
                        {"text": "", "thoughtSignature": "sig_trailing"},
                        {"text": None},
                        {
                            "text": "",
                            "functionCall": {"name": "ping", "args": {}},
                            "thoughtSignature": "sig_fc",
                        },
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    chat = _gemini_to_chat(resp, "gemini-3-pro-preview")
    msg = chat["choices"][0]["message"]
    assert msg["content"] == "Hello"
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "ping"
    assert msg["tool_calls"][0]["thoughtSignature"] == "sig_fc"

    # Streaming skips empty/None text chunks and still emits functionCall.
    chunks = list(_gemini_stream_chunk_to_chat(resp))
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"][0]["delta"]]
    assert deltas[0] == {"content": "Hello"}
    assert len(deltas) == 2
    assert deltas[1]["tool_calls"][0]["function"]["name"] == "ping"


def test_gemini_thought_signature_reattached_on_request() -> None:
    """Outgoing ``functionCall`` parts echo ``thoughtSignature`` from native or compat fields."""
    messages = [
        {"role": "user", "content": "Check London weather and UTC time"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "London"}'},
                    "thoughtSignature": "sig_direct",
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": '{"tz": "UTC"}'},
                    "extra_content": {"google": {"thought_signature": "sig_compat"}},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Rainy, 15C"},
        {"role": "tool", "tool_call_id": "call_2", "content": "14:00 UTC"},
        {"role": "user", "content": "Thanks!"},
    ]
    payload = _chat_to_gemini(messages, None, {}, model="gemini-3-pro-preview")

    # Model turn carries both signatures verbatim on their respective parts.
    model_parts = payload["contents"][1]["parts"]
    assert model_parts[0]["thoughtSignature"] == "sig_direct"
    assert model_parts[1]["thoughtSignature"] == "sig_compat"

    # Parallel tool responses are merged into one user turn with resolved function names.
    tool_turn = payload["contents"][2]
    assert tool_turn["role"] == "user"
    assert tool_turn["parts"] == [
        {"functionResponse": {"name": "get_weather", "response": {"result": "Rainy, 15C"}}},
        {"functionResponse": {"name": "get_time", "response": {"result": "14:00 UTC"}}},
    ]

    # Subsequent regular user message stays a separate user turn.
    assert payload["contents"][3] == {"role": "user", "parts": [{"text": "Thanks!"}]}


def test_gemini_thought_parts_separated_non_streaming() -> None:
    """``thought: true`` parts go to ``reasoning_content`` and never leak into ``content``."""
    resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Let me reason carefully.", "thought": True},
                        {"text": "", "thought": True},
                        {"text": '{"action": "allow"}'},
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    chat = _gemini_to_chat(resp, "gemini-3-pro-preview")
    msg = chat["choices"][0]["message"]
    assert msg["content"] == '{"action": "allow"}'
    assert msg["reasoning_content"] == "Let me reason carefully."


@pytest.mark.asyncio
async def test_gemini_stream_thought_parts_and_signed_function_call() -> None:
    """Streamed thought parts emit reasoning events and signed tool calls survive accumulation."""
    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "Step 1: check weather.", "thought": True},
                            {"text": "", "thought": True},
                        ],
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "Checking now."},
                            {
                                "functionCall": {
                                    "name": "get_weather",
                                    "args": {"city": "Tokyo"},
                                },
                                "thoughtSignature": "sig_stream_xyz",
                            },
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        },
    ]

    async def _chunks():
        for frame in frames:
            for chunk in _gemini_stream_chunk_to_chat(frame):
                yield chunk

    events = [
        e async for e in chat_stream_to_response_events(_chunks(), model="gemini-3-pro-preview")
    ]
    reasoning_started = [e for e in events if isinstance(e, ResponseReasoningStartedEvent)]
    reasoning_deltas = [e for e in events if isinstance(e, ResponseReasoningTextDeltaEvent)]
    text_deltas = [e for e in events if isinstance(e, ResponseTextDeltaEvent)]

    assert len(reasoning_started) == 1
    assert [e.delta for e in reasoning_deltas] == ["Step 1: check weather."]
    assert [e.delta for e in text_deltas] == ["Checking now."]

    response = events[-1].response
    messages = [o for o in response.output if isinstance(o, MessageOutput)]
    calls = [o for o in response.output if isinstance(o, FunctionCallOutput)]
    assert len(messages) == 1
    assert messages[0].content[0].text == "Checking now."
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].thought_signature == "sig_stream_xyz"

    # Full round-trip back into a Gemini turn-2 request payload.
    turn2_items = [
        {"role": "user", "content": "Weather in Tokyo?"},
        {
            "type": "function_call",
            "call_id": calls[0].call_id,
            "name": calls[0].name,
            "arguments": calls[0].arguments,
            "thought_signature": calls[0].thought_signature,
        },
        {
            "type": "function_call_output",
            "call_id": calls[0].call_id,
            "output": "Sunny, 24C",
        },
    ]
    chat_messages = responses_input_to_chat_messages(turn2_items, None)
    payload = _chat_to_gemini(chat_messages, None, {}, model="gemini-3-pro-preview")
    assert payload["contents"][1]["parts"][0]["thoughtSignature"] == "sig_stream_xyz"
    assert payload["contents"][2]["parts"][0]["functionResponse"]["name"] == "get_weather"


def test_stream_request_multi_frame_parallel_tool_calls_keep_distinct_indices(
    serve_streamed_response,
) -> None:
    """Parallel ``functionCall`` parts arriving in separate SSE frames get distinct indices."""
    frame1 = json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {"name": "get_weather", "args": {"city": "Paris"}},
                                "thoughtSignature": "sig_first",
                            }
                        ],
                    }
                }
            ]
        }
    )
    frame2 = json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {"name": "get_time", "args": {"tz": "CET"}},
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    sse_body = f"data: {frame1}\n\ndata: {frame2}\n\n".encode()
    serve_streamed_response(200, sse_body)

    adapter = GeminiAdapter()

    async def _collect():
        gen = adapter._stream_request(
            "https://fake-host/v1beta/models/gemini-3-pro-preview:streamGenerateContent?alt=sse",
            {},
            {"contents": []},
        )
        return [e async for e in chat_stream_to_response_events(gen, model="gemini-3-pro-preview")]

    events = asyncio.run(_collect())
    calls = [o for o in events[-1].response.output if isinstance(o, FunctionCallOutput)]
    assert len(calls) == 2
    assert calls[0].name == "get_weather"
    assert calls[0].thought_signature == "sig_first"
    assert calls[1].name == "get_time"
    assert calls[1].thought_signature is None


@pytest.mark.parametrize(
    ("model", "effort", "expected_config"),
    [
        (
            "gemini-3-pro-preview",
            "high",
            {"thinkingLevel": "HIGH", "includeThoughts": True},
        ),
        (
            "gemini-3.5-flash",
            "low",
            {"thinkingLevel": "LOW", "includeThoughts": True},
        ),
        (
            "gemini-4-pro",
            "medium",
            {"thinkingLevel": "MEDIUM", "includeThoughts": True},
        ),
        (
            "gemini-2.5-pro",
            "high",
            {"thinkingBudget": 24576, "includeThoughts": True},
        ),
        (
            "gemini-2.5-flash",
            "low",
            {"thinkingBudget": 1024, "includeThoughts": True},
        ),
        (
            "gemini-2.5-flash-lite",
            "medium",
            {"thinkingBudget": 8192, "includeThoughts": True},
        ),
    ],
)
def test_reasoning_effort_translates_to_thinking_config(
    model: str,
    effort: str,
    expected_config: dict[str, object],
) -> None:
    """``reasoning_effort`` maps to ``thinkingLevel`` on Gemini 3+ and budget on 2.5."""
    payload = _chat_to_gemini(
        [{"role": "user", "content": "Solve this"}],
        None,
        {"reasoning_effort": effort},
        model=model,
    )
    assert payload["generationConfig"]["thinkingConfig"] == expected_config


def test_reasoning_effort_none_omits_thinking_config() -> None:
    """A ``None`` or empty ``reasoning_effort`` leaves ``thinkingConfig`` unset."""
    payload = _chat_to_gemini(
        [{"role": "user", "content": "Hi"}],
        None,
        {"reasoning_effort": None},
        model="gemini-3-pro-preview",
    )
    assert "generationConfig" not in payload


@pytest.mark.parametrize("bad_effort", ["xhigh", "minimal", "bogus", 0, False])
def test_invalid_reasoning_effort_raises_permanent_llm_error(bad_effort: object) -> None:
    """Unsupported effort levels fail fast with ``PermanentLLMError``."""
    with pytest.raises(PermanentLLMError) as exc_info:
        _chat_to_gemini(
            [{"role": "user", "content": "Hi"}],
            None,
            {"reasoning_effort": bad_effort},
            model="gemini-3-pro-preview",
        )
    assert exc_info.value.code == "unsupported_reasoning_effort"


@pytest.mark.parametrize("legacy_model", ["gemini-1.5-pro", "gemini-2.0-flash"])
def test_non_thinking_legacy_model_rejects_reasoning_effort(legacy_model: str) -> None:
    """Gemini <2.5 non-thinking models fail fast when ``reasoning_effort`` is requested."""
    with pytest.raises(PermanentLLMError) as exc_info:
        _chat_to_gemini(
            [{"role": "user", "content": "Hi"}],
            None,
            {"reasoning_effort": "high"},
            model=legacy_model,
        )
    assert exc_info.value.code == "unsupported_reasoning_effort"


def test_gemini_25_thinking_budget_clamps_to_max_tokens() -> None:
    """Gemini 2.5 ``thinkingBudget`` does not exceed caller's ``max_tokens``."""
    payload = _chat_to_gemini(
        [{"role": "user", "content": "Hi"}],
        None,
        {"reasoning_effort": "high", "max_tokens": 2048},
        model="gemini-2.5-pro",
    )
    assert payload["generationConfig"]["maxOutputTokens"] == 2048
    assert payload["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 2048,
        "includeThoughts": True,
    }


@pytest.mark.parametrize(
    ("endpoint_base", "model", "expected"),
    [
        (
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-3-pro-preview",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview",
        ),
        (
            "https://aiplatform.googleapis.com/v1/projects/p/locations/global/publishers/google/models",
            "gemini-3-pro-preview",
            "https://aiplatform.googleapis.com/v1/projects/p/locations/global/publishers/google/models/gemini-3-pro-preview",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/",
            "models/gemini-2.5-pro",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/",
            "/models/gemini-3-pro-preview",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview",
        ),
    ],
)
def test_build_model_path_avoids_duplicate_models_segment(
    endpoint_base: str,
    model: str,
    expected: str,
) -> None:
    """``_build_model_path`` handles both AI Studio and Vertex ``/models`` base URLs."""
    assert _build_model_path(endpoint_base, model) == expected
