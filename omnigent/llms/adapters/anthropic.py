"""
Anthropic Messages API adapter.

Translates Chat Completions format to/from Anthropic's native
Messages API. Ported from MLflow AI Gateway's AnthropicAdapter.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters._content import parse_data_uri
from omnigent.llms.adapters.base import BaseAdapter
from omnigent.reasoning_effort import ANTHROPIC_EFFORTS, validate_effort_or_llm_error

_BASE_URL = "https://api.anthropic.com/v1"
_API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 16384
_REQUEST_TIMEOUT = 120
_STREAM_TIMEOUT = 300


class AnthropicAdapter(BaseAdapter):
    """
    Adapter for the Anthropic Messages API.

    API key must be provided via ``connection_params["api_key"]``
    at call time (from the ``connection:`` block in agent spec).
    """

    async def chat_completions(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        extra: dict[str, Any],
        *,
        connection_params: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        """
        Send a request to the Anthropic Messages API.

        :param messages: Chat Completions format messages.
        :param model: Model name, e.g. ``"claude-sonnet-4-20250514"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Enable streaming.
        :param extra: Additional kwargs (temperature, etc.).
        :param connection_params: Per-call overrides. Supported keys:
            ``"api_key"``, ``"base_url"``.
        :param timeout: Request timeout in seconds. ``None`` uses
            the module default.
        :returns: Chat Completions response dict or async chunk
            iterator.
        """
        params = connection_params or {}
        payload = _chat_to_anthropic(messages, model, tools, extra)
        headers = _build_headers(
            api_key_override=params.get("api_key"),
        )
        override_base = params.get("base_url")
        effective_base = override_base.rstrip("/") if override_base else _BASE_URL

        if stream:
            payload["stream"] = True
            effective_to = timeout if timeout is not None else _STREAM_TIMEOUT
            return _stream_request(
                headers,
                payload,
                effective_base,
                effective_to,
            )

        effective_to = timeout if timeout is not None else _REQUEST_TIMEOUT
        return await _send_request(
            headers,
            payload,
            effective_base,
            effective_to,
        )


# ── Request translation ───────────────────────────────────


def _chat_to_anthropic(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert Chat Completions messages to Anthropic Messages API payload.

    :param messages: Chat Completions messages.
    :param model: Model name.
    :param tools: OpenAI-format tool schemas or ``None``.
    :param extra: Additional kwargs.
    :returns: Anthropic API request payload.
    """
    payload: dict[str, Any] = {"model": model}

    # Extract system messages
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    if system_parts:
        payload["system"] = "\n".join(system_parts)

    # Convert messages (skip system)
    converted: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "assistant":
            converted.append(_convert_assistant_message(m))
        elif role == "tool":
            converted.append(_convert_tool_message(m))
        elif role == "user":
            converted.append(_convert_user_message(m))

    payload["messages"] = converted

    # Max tokens
    max_tokens = (
        extra.pop("max_tokens", None)
        or extra.pop("max_completion_tokens", None)
        or _DEFAULT_MAX_TOKENS
    )
    payload["max_tokens"] = max_tokens

    # Temperature — Anthropic range is 0-1, OpenAI is 0-2
    if "temperature" in extra:
        payload["temperature"] = 0.5 * extra.pop("temperature")

    # Top P
    if "top_p" in extra:
        payload["top_p"] = extra.pop("top_p")

    # Stop sequences
    if stop := extra.pop("stop", None):
        payload["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    # Tools
    if tools:
        payload["tools"] = _convert_tools(tools)

    # Tool choice
    if tool_choice := extra.pop("tool_choice", None):
        payload["tool_choice"] = _convert_tool_choice(tool_choice)

    # Reasoning effort (for Claude extended thinking)
    if reasoning_effort := extra.pop("reasoning_effort", None):
        effort = validate_effort_or_llm_error(reasoning_effort, "Anthropic", ANTHROPIC_EFFORTS)
        if effort is not None:
            payload["thinking"] = {"type": "adaptive"}
            payload.setdefault("output_config", {})["effort"] = effort
            # Adaptive thinking on newer models (Opus 4.6+) does not support
            # temperature or top_p — remove them if they were set.
            payload.pop("temperature", None)
            payload.pop("top_p", None)

    return payload


def _convert_assistant_message(m: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Chat Completions assistant message to Anthropic format.

    :param m: Assistant message dict with optional ``tool_calls``.
    :returns: Anthropic-format assistant message.
    """
    if tool_calls := m.get("tool_calls"):
        content: list[dict[str, Any]] = []
        if text := m.get("content"):
            content.append({"type": "text", "text": text})
        for tc in tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"]),
                }
            )
        return {"role": "assistant", "content": content}
    return {"role": "assistant", "content": m.get("content")}


def _convert_tool_message(m: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Chat Completions tool message to Anthropic format.

    :param m: Tool message with ``tool_call_id`` and ``content``.
    :returns: Anthropic-format user message with ``tool_result``.
    """
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }
        ],
    }


def _convert_user_message(m: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Chat Completions user message to Anthropic format.

    When content is a string, passes through as-is (Anthropic
    accepts string content). When content is a list (multimodal),
    translates each part to Anthropic's native format.

    :param m: User message with string or list content.
    :returns: Anthropic-format user message.
    """
    content = m.get("content")
    if not isinstance(content, list):
        return m
    return {
        "role": "user",
        "content": [_translate_part_to_anthropic(part) for part in content],
    }


def _translate_part_to_anthropic(part: dict[str, Any]) -> dict[str, Any]:
    """
    Translate a single Chat Completions content part to Anthropic format.

    - ``text`` → ``{"type": "text", "text": "..."}``
    - ``image_url`` with data URI → ``{"type": "image", "source":
      {"type": "base64", "media_type": "...", "data": "..."}}``
    - ``image_url`` with external URL → ``{"type": "image", "source":
      {"type": "url", "url": "..."}}``
    - ``input_file`` with file_data → ``{"type": "document", "source":
      {"type": "base64", "media_type": "...", "data": "..."}}``
    - Unrecognized → passed through as-is.

    :param part: A Chat Completions content part dict.
    :returns: An Anthropic content block dict.
    """
    part_type = part.get("type")

    if part_type == "text":
        return {"type": "text", "text": part["text"]}

    if part_type == "image_url":
        image_url = part["image_url"]
        url = image_url["url"]
        parsed = parse_data_uri(url)
        if parsed is not None:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": parsed.media_type,
                    "data": parsed.data,
                },
            }
        # External URL — Anthropic supports URL source type.
        return {
            "type": "image",
            "source": {"type": "url", "url": url},
        }

    if part_type == "input_file":
        # file_data is a data: URI (e.g. "data:application/pdf;base64,...").
        # content_resolver guarantees this format; fail loud if violated.
        file_uri = parse_data_uri(part["file_data"])
        if file_uri is None:
            raise ValueError(
                f"input_file file_data must be a data: URI, got: {part['file_data'][:80]!r}"
            )
        if file_uri.media_type == "application/pdf":
            # Anthropic's base64 document source only accepts PDF.
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": file_uri.data,
                },
            }
        # All other file types (markdown, plain text, code, etc.):
        # use Anthropic's "text" source type, which accepts a decoded
        # string.  The base64 payload is decoded here; the filename
        # field on the surrounding block already tells the model the
        # original extension.
        text_content = base64.b64decode(file_uri.data).decode("utf-8", errors="replace")
        return {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": text_content,
            },
        }

    # Unrecognized part type — pass through for forward compat.
    return part


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Convert OpenAI-format tool schemas to Anthropic format.

    :param tools: OpenAI tool definitions.
    :returns: Anthropic tool definitions.
    """
    converted = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool["function"]
        entry: dict[str, Any] = {
            "name": func["name"],
            "input_schema": func.get("parameters", {}),
        }
        if desc := func.get("description"):
            entry["description"] = desc
        converted.append(entry)
    return converted


def _convert_tool_choice(
    tool_choice: str | dict[str, Any],
) -> dict[str, Any]:
    """
    Convert OpenAI tool_choice to Anthropic format.

    :param tool_choice: ``"none"``, ``"auto"``, ``"required"``,
        or ``{"type": "function", "function": {"name": "..."}}``.
    :returns: Anthropic tool_choice dict.
    """
    match tool_choice:
        case "none":
            return {"type": "none"}
        case "auto":
            return {"type": "auto"}
        case "required":
            return {"type": "any"}
        case {"type": "function", "function": {"name": name}}:
            return {"type": "tool", "name": name}
        case _:
            return {"type": "auto"}


def _effort_to_budget(effort: str, max_tokens: int) -> int:
    """
    Map a reasoning effort string to a thinking budget.

    :param effort: ``"low"``, ``"medium"``, or ``"high"``.
    :param max_tokens: The max_tokens setting for the request.
    :returns: Budget token count.
    """
    effort = validate_effort_or_llm_error(effort, "Anthropic", ANTHROPIC_EFFORTS)
    match effort:
        case "low":
            return min(1024, max_tokens)
        case "medium":
            return min(4096, max_tokens)
        case "high":
            return min(8192, max_tokens)
        case "xhigh" | "max":
            return max_tokens
        case _:
            raise ValueError(f"Unsupported Anthropic reasoning effort: {effort}")


# ── Response translation ──────────────────────────────────


def _anthropic_to_chat(resp: dict[str, Any]) -> dict[str, Any]:
    """
    Convert an Anthropic Messages API response to Chat Completions format.

    :param resp: Anthropic response dict.
    :returns: Chat Completions response dict.
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in resp.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    },
                }
            )

    # Anthropic API always returns stop_reason; fail loud if missing.
    stop_reason = resp["stop_reason"]
    finish_reason = "length" if stop_reason == "max_tokens" else "stop"
    if tool_calls:
        finish_reason = "tool_calls"

    content = "\n".join(text_parts) if text_parts else None

    usage = resp.get("usage", {})

    return {
        "id": resp["id"],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp["model"],
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens": (
                (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0) or None
            ),
        },
    }


# ── Streaming ─────────────────────────────────────────────


async def _stream_to_chat_chunks(
    lines: AsyncIterator[str],
) -> AsyncIterator[dict[str, Any]]:
    """
    Parse Anthropic SSE stream into Chat Completions chunk dicts.

    :param lines: Async iterator of raw SSE lines from the HTTP
        response.
    :returns: Async iterator of Chat Completions streaming chunk
        dicts.
    """
    metadata: dict[str, str] = {}
    usage_data: dict[str, int] = {}
    tool_call_index = -1

    async for line in lines:
        if not line.startswith("data: "):
            continue

        data = json.loads(line[len("data: ") :])
        event_type = data.get("type")

        if event_type == "message_start":
            msg = data.get("message", {})
            metadata["id"] = msg["id"]
            metadata["model"] = msg["model"]
            if msg_usage := msg.get("usage"):
                usage_data["input_tokens"] = msg_usage.get("input_tokens", 0)
            continue

        if event_type == "content_block_start":
            block = data.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_call_index += 1
                yield _make_chunk(
                    metadata,
                    delta={
                        "tool_calls": [
                            {
                                "index": tool_call_index,
                                "id": block["id"],
                                "type": "function",
                                "function": {"name": block["name"]},
                            }
                        ]
                    },
                )
            continue

        if event_type == "content_block_delta":
            delta_block = data.get("delta", {})
            if delta_block.get("type") == "text_delta":
                yield _make_chunk(
                    metadata,
                    delta={"content": delta_block["text"]},
                )
            elif delta_block.get("type") == "input_json_delta":
                yield _make_chunk(
                    metadata,
                    delta={
                        "tool_calls": [
                            {
                                "index": tool_call_index,
                                "function": {
                                    "arguments": delta_block["partial_json"],
                                },
                            }
                        ]
                    },
                )
            continue

        if event_type == "message_delta":
            if delta_usage := data.get("usage"):
                usage_data["output_tokens"] = delta_usage.get("output_tokens", 0)
            stop_reason = data.get("delta", {}).get("stop_reason")
            finish = "length" if stop_reason == "max_tokens" else "stop"
            yield _make_chunk(
                metadata,
                delta={},
                finish_reason=finish,
                usage={
                    "prompt_tokens": usage_data.get("input_tokens"),
                    "completion_tokens": usage_data.get("output_tokens"),
                    "total_tokens": (
                        usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0)
                    ),
                },
            )
            continue


def _make_chunk(
    metadata: dict[str, str],
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a Chat Completions streaming chunk dict.

    :param metadata: Response metadata (id, model).
    :param delta: The delta content for this chunk.
    :param finish_reason: Finish reason, or ``None``.
    :param usage: Usage dict, or ``None``.
    :returns: A Chat Completions chunk dict.
    """
    chunk: dict[str, Any] = {
        "id": metadata["id"],
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": metadata["model"],
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage:
        chunk["usage"] = usage
    return chunk


# ── HTTP helpers ──────────────────────────────────────────


def _build_headers(
    api_key_override: str | None = None,
) -> dict[str, str]:
    """
    Build Anthropic API headers.

    :param api_key_override: API key from ``connection_params``.
    :returns: Headers dict with API key and version.
    :raises OmnigentError: If no API key is provided.
    """
    if not api_key_override:
        raise OmnigentError(
            "Anthropic adapter requires 'api_key' in"
            " connection_params (from llm.connection config)",
            code=ErrorCode.INVALID_INPUT,
        )
    return {
        "Content-Type": "application/json",
        "x-api-key": api_key_override,
        "anthropic-version": _API_VERSION,
    }


async def _send_request(
    headers: dict[str, str],
    payload: dict[str, Any],
    base_url: str,
    timeout: int = _REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """
    Send a non-streaming request to Anthropic and return a Chat
    Completions format response.

    :param headers: HTTP headers.
    :param payload: Anthropic API payload.
    :param base_url: API base URL, e.g.
        ``"https://api.anthropic.com/v1"``.
    :param timeout: Request timeout in seconds, e.g. ``120``.
    :returns: Chat Completions response dict.
    """
    url = f"{base_url}/messages"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        return _anthropic_to_chat(resp.json())


async def _stream_request(
    headers: dict[str, str],
    payload: dict[str, Any],
    base_url: str,
    timeout: int = _STREAM_TIMEOUT,
) -> AsyncIterator[dict[str, Any]]:
    """
    Send a streaming request to Anthropic and yield Chat
    Completions chunk dicts.

    :param headers: HTTP headers.
    :param payload: Anthropic API payload with ``stream: true``.
    :param base_url: API base URL, e.g.
        ``"https://api.anthropic.com/v1"``.
    :param timeout: Request timeout in seconds, e.g. ``300``.
    :returns: Async iterator of Chat Completions chunk dicts.
    """
    url = f"{base_url}/messages"
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
        ) as resp,
    ):
        resp.raise_for_status()
        async for chunk in _stream_to_chat_chunks(
            resp.aiter_lines(),
        ):
            yield chunk
