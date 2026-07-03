"""Tests for llms.adapters.bedrock — translation logic."""

import json

import pytest

from omnigent.llms.adapters.bedrock import (
    _build_converse_kwargs,
    _converse_to_chat,
    _convert_tools,
    _messages_to_converse,
    _translate_part_to_converse,
)

# ── Request translation ──────────────────────────────────


def test_system_messages_extracted_as_system_prompts() -> None:
    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "user", "content": "Hi"},
    ]
    converse_msgs, system_prompts = _messages_to_converse(messages)
    assert system_prompts == [{"text": "Be helpful."}]
    assert len(converse_msgs) == 1
    assert converse_msgs[0]["role"] == "user"


def test_user_message_converted_to_text_block() -> None:
    messages = [{"role": "user", "content": "Hello"}]
    converse_msgs, _ = _messages_to_converse(messages)
    assert converse_msgs[0] == {
        "role": "user",
        "content": [{"text": "Hello"}],
    }


def test_assistant_message_with_text() -> None:
    messages = [{"role": "assistant", "content": "Hi there"}]
    converse_msgs, _ = _messages_to_converse(messages)
    assert converse_msgs[0] == {
        "role": "assistant",
        "content": [{"text": "Hi there"}],
    }


def test_assistant_tool_calls_converted_to_tool_use() -> None:
    messages = [
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
        }
    ]
    converse_msgs, _ = _messages_to_converse(messages)
    msg = converse_msgs[0]
    assert msg["role"] == "assistant"
    tu = msg["content"][0]["toolUse"]
    assert tu["toolUseId"] == "call_1"
    assert tu["name"] == "get_weather"
    assert tu["input"] == {"city": "London"}


def test_tool_messages_converted_to_tool_result() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Sunny, 22C",
        }
    ]
    converse_msgs, _ = _messages_to_converse(messages)
    msg = converse_msgs[0]
    assert msg["role"] == "user"
    tr = msg["content"][0]["toolResult"]
    assert tr["toolUseId"] == "call_1"
    assert tr["content"] == [{"text": "Sunny, 22C"}]


def test_inference_config_mapped() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    extra = {"temperature": 0.7, "top_p": 0.9, "max_tokens": 100}
    kwargs = _build_converse_kwargs(messages, "model-id", None, extra)
    config = kwargs["inferenceConfig"]
    assert config["temperature"] == 0.7
    assert config["topP"] == 0.9
    assert config["maxTokens"] == 100


def test_stop_sequences_mapped() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    extra = {"stop": ["END", "STOP"]}
    kwargs = _build_converse_kwargs(messages, "model-id", None, extra)
    assert kwargs["inferenceConfig"]["stopSequences"] == ["END", "STOP"]


def test_single_stop_string_wrapped_in_list() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    extra = {"stop": "END"}
    kwargs = _build_converse_kwargs(messages, "model-id", None, extra)
    assert kwargs["inferenceConfig"]["stopSequences"] == ["END"]


def test_tools_converted_to_tool_spec() -> None:
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
    spec = result[0]["toolSpec"]
    assert spec["name"] == "get_weather"
    assert spec["description"] == "Get weather"
    assert spec["inputSchema"] == {"json": {"type": "object", "properties": {}}}


def test_tool_config_added_to_kwargs() -> None:
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
    kwargs = _build_converse_kwargs(messages, "model-id", tools, {})
    assert "toolConfig" in kwargs
    assert "tools" in kwargs["toolConfig"]


# ── Response translation ─────────────────────────────────


def test_converse_text_response_to_chat() -> None:
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Hello!"}],
            }
        },
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 10,
            "outputTokens": 5,
            "totalTokens": 15,
        },
    }
    chat = _converse_to_chat(response, "bedrock-model")
    assert chat["model"] == "bedrock-model"
    assert chat["choices"][0]["message"]["content"] == "Hello!"
    assert chat["choices"][0]["finish_reason"] == "stop"
    assert chat["usage"]["prompt_tokens"] == 10
    assert chat["usage"]["completion_tokens"] == 5


def test_converse_tool_use_response_to_chat() -> None:
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tu_1",
                            "name": "get_weather",
                            "input": {"city": "London"},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {},
    }
    chat = _converse_to_chat(response, "bedrock-model")
    tool_calls = chat["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "tu_1"
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"city": "London"}
    assert chat["choices"][0]["finish_reason"] == "tool_calls"


def test_converse_mixed_text_and_tool_use() -> None:
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "Let me check."},
                    {
                        "toolUse": {
                            "toolUseId": "tu_1",
                            "name": "search",
                            "input": {"q": "test"},
                        }
                    },
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {},
    }
    chat = _converse_to_chat(response, "bedrock-model")
    assert chat["choices"][0]["message"]["content"] == "Let me check."
    assert len(chat["choices"][0]["message"]["tool_calls"]) == 1


# ── Multimodal content translation ──────────────────────


def test_user_message_with_image_data_uri() -> None:
    """
    User message with image_url data URI translates to Bedrock
    image block with format and bytes.
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
    converse_msgs, _ = _messages_to_converse(messages)
    blocks = converse_msgs[0]["content"]
    # Two blocks: text + image.
    assert len(blocks) == 2
    assert blocks[0] == {"text": "Describe this"}
    assert blocks[1] == {
        "image": {
            "format": "png",
            "source": {"bytes": "abc123"},
        },
    }


def test_user_message_with_external_url_becomes_text() -> None:
    """
    External URL falls back to text placeholder since Bedrock
    does not support URL references in image blocks.
    """
    part = {
        "type": "image_url",
        "image_url": {"url": "https://example.com/photo.png"},
    }
    result = _translate_part_to_converse(part)
    assert result == {"text": "[image: https://example.com/photo.png]"}


def test_user_message_with_file_data() -> None:
    """
    input_file with file_data translates to Bedrock document block.
    """
    part = {
        "type": "input_file",
        "file_data": "data:application/pdf;base64,JVBERi0xLjQK",
        "filename": "report.pdf",
    }
    result = _translate_part_to_converse(part)
    assert result == {
        "document": {
            "format": "pdf",
            "name": "report.pdf",
            "source": {"bytes": "JVBERi0xLjQK"},
        },
    }


@pytest.mark.parametrize(
    ("mime", "expected_format"),
    [
        # Documented Bedrock formats and their canonical MIMEs.
        # https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_DocumentBlock.html
        ("application/pdf", "pdf"),
        ("text/csv", "csv"),
        ("application/msword", "doc"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
        ("application/vnd.ms-excel", "xls"),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
        ),
        ("text/html", "html"),
        ("text/plain", "txt"),
        ("text/markdown", "md"),
        # Other text/* MIMEs collapse to "txt" — Bedrock's only
        # generic-text bucket. The block's filename still tells the
        # model the original extension.
        ("text/yaml", "txt"),
        ("text/x-rust", "txt"),
        ("text/typescript", "txt"),
    ],
)
def test_bedrock_document_format_maps_known_mimes(mime: str, expected_format: str) -> None:
    """Known + text/* MIMEs map to a Bedrock-accepted format value.

    Regression check for ``text/plain`` and ``text/markdown`` — pre-fix
    these went through ``media_type.split("/")[-1]`` and produced
    ``"plain"`` / ``"markdown"``, both rejected by the Converse API.
    """
    from omnigent.llms.adapters.bedrock import _bedrock_document_format

    assert _bedrock_document_format(mime) == expected_format


def test_bedrock_document_format_unknown_non_text_keeps_legacy_split() -> None:
    """Non-text MIMEs we don't recognise keep the prior split-by-``/``
    behaviour, so any application/* traffic that already worked is
    unaffected. The Converse API will reject formats it doesn't
    accept (e.g. ``"json"``) — that's the same outcome as before this
    fix, not a regression introduced here.
    """
    from omnigent.llms.adapters.bedrock import _bedrock_document_format

    assert _bedrock_document_format("application/json") == "json"
    assert _bedrock_document_format("application/zip") == "zip"


def test_user_message_with_text_plain_file_data_uses_txt_format() -> None:
    """End-to-end: a ``text/plain`` file_data block becomes a Bedrock
    document with ``format: "txt"`` (not ``"plain"``).

    Pairs with the content_resolver change that coerces unsupported
    file_data MIMEs to text/plain: once the resolver hands Bedrock a text/plain
    data URI, the adapter must translate it into a format Bedrock
    actually accepts.
    """
    part = {
        "type": "input_file",
        "file_data": "data:text/plain;base64,SGVsbG8sIFdvcmxkIQ==",
        "filename": "config.yaml",
    }
    result = _translate_part_to_converse(part)
    assert result == {
        "document": {
            "format": "txt",
            "name": "config.yaml",
            "source": {"bytes": "SGVsbG8sIFdvcmxkIQ=="},
        },
    }


def test_user_message_with_markdown_file_data_uses_md_format() -> None:
    """End-to-end: ``text/markdown`` becomes ``format: "md"``.

    Bedrock's enum spells the markdown format ``md``; the prior split-
    by-``/`` produced ``"markdown"`` which the Converse API rejects.
    """
    part = {
        "type": "input_file",
        "file_data": "data:text/markdown;base64,IyBIZWxsbwo=",
        "filename": "README.md",
    }
    result = _translate_part_to_converse(part)
    assert result == {
        "document": {
            "format": "md",
            "name": "README.md",
            "source": {"bytes": "IyBIZWxsbwo="},
        },
    }


def test_string_user_content_becomes_text_block() -> None:
    """
    String user content becomes a single text block —
    backward compatibility with text-only messages.
    """
    messages = [{"role": "user", "content": "Hello"}]
    converse_msgs, _ = _messages_to_converse(messages)
    assert converse_msgs[0]["content"] == [{"text": "Hello"}]


# ── Streaming chunk helpers ──────────────────────────────


def test_stream_text_chunk_structure() -> None:
    """_stream_text_chunk builds a valid Chat Completions text delta chunk."""
    from omnigent.llms.adapters.bedrock import _stream_text_chunk

    chunk = _stream_text_chunk("bedrock-model", "Hello")
    assert chunk["model"] == "bedrock-model"
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"][0]["delta"]["content"] == "Hello"
    assert chunk["choices"][0]["finish_reason"] is None


def test_stream_stop_chunk_structure() -> None:
    """_stream_stop_chunk builds a valid stop chunk."""
    from omnigent.llms.adapters.bedrock import _stream_stop_chunk

    chunk = _stream_stop_chunk("bedrock-model", "stop")
    assert chunk["choices"][0]["finish_reason"] == "stop"
    assert chunk["choices"][0]["delta"] == {}


def test_stream_stop_chunk_tool_calls() -> None:
    """_stream_stop_chunk for tool_use produces tool_calls finish reason."""
    from omnigent.llms.adapters.bedrock import _stream_stop_chunk

    chunk = _stream_stop_chunk("bedrock-model", "tool_calls")
    assert chunk["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_usage_chunk_structure() -> None:
    """_stream_usage_chunk builds a valid usage chunk."""
    from omnigent.llms.adapters.bedrock import _stream_usage_chunk

    usage = {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
    chunk = _stream_usage_chunk("bedrock-model", usage)
    assert chunk["usage"]["prompt_tokens"] == 10
    assert chunk["usage"]["completion_tokens"] == 5
    assert chunk["usage"]["total_tokens"] == 15


# ── None content becomes empty blocks ────────────────────


def test_none_content_becomes_empty_blocks() -> None:
    """None content yields empty content block list."""
    from omnigent.llms.adapters.bedrock import _content_to_converse_blocks

    assert _content_to_converse_blocks(None) == []


# ── Unrecognized part becomes text placeholder ───────────


def test_unrecognized_part_becomes_text_placeholder() -> None:
    """Unrecognized content part types render as text placeholder."""
    result = _translate_part_to_converse({"type": "input_audio", "data": "base64"})
    assert result == {"text": "[unsupported content: input_audio]"}


# ── file_data without data URI raises ────────────────────


def test_file_data_without_data_uri_raises() -> None:
    """input_file without a data: URI prefix raises ValueError."""
    part = {
        "type": "input_file",
        "file_data": "https://example.com/file.pdf",
    }
    with pytest.raises(ValueError, match="data: URI"):
        _translate_part_to_converse(part)


# ── file_data without filename ───────────────────────────


def test_file_data_without_filename() -> None:
    """input_file without filename omits name from document block."""
    part = {
        "type": "input_file",
        "file_data": "data:application/pdf;base64,JVBERi0xLjQK",
    }
    result = _translate_part_to_converse(part)
    assert "name" not in result["document"]


# ── Non-function tools skipped ───────────────────────────


def test_non_function_tools_skipped() -> None:
    """Non-function tool types are filtered out."""
    tools = [
        {"type": "not_function", "whatever": {}},
        {"type": "function", "function": {"name": "fn", "parameters": {}}},
    ]
    result = _convert_tools(tools)
    assert len(result) == 1
    assert result[0]["toolSpec"]["name"] == "fn"


# ── Tool without description ─────────────────────────────


def test_tool_without_description_omits_description() -> None:
    """Tool specs without description omit the field."""
    tools = [
        {
            "type": "function",
            "function": {"name": "fn", "parameters": {}},
        }
    ]
    result = _convert_tools(tools)
    assert "description" not in result[0]["toolSpec"]


# ── System prompts None when absent ──────────────────────


def test_no_system_messages_returns_none_system() -> None:
    """No system messages -> system_prompts is None."""
    messages = [{"role": "user", "content": "Hi"}]
    _, system_prompts = _messages_to_converse(messages)
    assert system_prompts is None


# ── Converse response with empty content ─────────────────


def test_converse_response_no_content() -> None:
    """Response with no text or tool content yields None content and empty tool_calls."""
    response = {
        "output": {"message": {"role": "assistant", "content": []}},
        "stopReason": "end_turn",
        "usage": {},
    }
    chat = _converse_to_chat(response, "bedrock-model")
    assert chat["choices"][0]["message"]["content"] is None
    assert chat["choices"][0]["message"]["tool_calls"] is None


# ── max_completion_tokens alias ──────────────────────────


def test_max_completion_tokens_alias() -> None:
    """max_completion_tokens is an alias for max_tokens in inference config."""
    messages = [{"role": "user", "content": "Hi"}]
    extra = {"max_completion_tokens": 2048}
    kwargs = _build_converse_kwargs(messages, "model-id", None, extra)
    assert kwargs["inferenceConfig"]["maxTokens"] == 2048
