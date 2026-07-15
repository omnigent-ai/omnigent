"""
Google Gemini adapter.

Translates Chat Completions format to/from Gemini's generateContent
API. Ported from MLflow AI Gateway's GeminiAdapter.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters._content import parse_data_uri
from omnigent.llms.adapters.base import BaseAdapter

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_REQUEST_TIMEOUT = 120
_STREAM_TIMEOUT = 300

# Gemini generation config key mapping from OpenAI names
_GENERATION_CONFIG_KEYS = {
    "stop": "stopSequences",
    "n": "candidateCount",
    "max_tokens": "maxOutputTokens",
    "max_completion_tokens": "maxOutputTokens",
    "top_k": "topK",
    "top_p": "topP",
    "frequency_penalty": "frequencyPenalty",
    "presence_penalty": "presencePenalty",
}

_GENERATION_CONFIG_NAMES = {
    "temperature",
    "stopSequences",
    "candidateCount",
    "maxOutputTokens",
    "topK",
    "topP",
    "frequencyPenalty",
    "presencePenalty",
}


class GeminiAdapter(BaseAdapter):
    """
    Adapter for the Google Gemini API.

    API key must be provided via ``connection_params["api_key"]``
    at call time (from the ``connection:`` block in agent spec).
    """

    def _get_base_url(self) -> str:
        """
        Return the API base URL. Overridden by VertexAdapter.

        :returns: The Gemini API base URL.
        """
        return _BASE_URL

    async def _get_headers(
        self,
        api_key_override: str | None = None,
    ) -> dict[str, str]:
        """
        Build HTTP headers. Overridden by VertexAdapter for OAuth.

        Async so VertexAdapter can offload blocking auth refresh
        to a thread. No I/O in this base implementation.

        :param api_key_override: API key from ``connection_params``.
        :returns: Headers dict with API key.
        :raises OmnigentError: If no API key is provided.
        """
        if not api_key_override:
            raise OmnigentError(
                "Gemini adapter requires 'api_key' in"
                " connection_params (from llm.connection config)",
                code=ErrorCode.INVALID_INPUT,
            )
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key_override,
        }

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
        Send a request to the Gemini API.

        :param messages: Chat Completions format messages.
        :param model: Model name, e.g. ``"gemini-2.5-pro"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Enable streaming.
        :param extra: Additional kwargs.
        :param connection_params: Per-call overrides. Supported keys:
            ``"api_key"``, ``"base_url"``.
        :param timeout: Request timeout in seconds. ``None`` uses
            the module default.
        :returns: Chat Completions response dict or async chunk
            iterator.
        """
        params = connection_params or {}
        payload = _chat_to_gemini(messages, tools, extra)
        headers = await self._get_headers(
            api_key_override=params.get("api_key"),
        )
        override_base = params.get("base_url")
        effective_base = override_base.rstrip("/") if override_base else self._get_base_url()

        if stream:
            effective_to = timeout if timeout is not None else _STREAM_TIMEOUT
            url = f"{effective_base}/models/{model}:streamGenerateContent?alt=sse"
            return self._stream_request(
                url,
                headers,
                payload,
                effective_to,
            )

        effective_to = timeout if timeout is not None else _REQUEST_TIMEOUT
        url = f"{effective_base}/models/{model}:generateContent"
        return await self._send_request(
            url,
            headers,
            payload,
            model,
            effective_to,
        )

    async def _send_request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        model: str,
        timeout: int = _REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """
        Send a non-streaming Gemini request.

        :param url: The full endpoint URL.
        :param headers: HTTP headers.
        :param payload: Gemini API payload.
        :param model: Model name for the response.
        :param timeout: Request timeout in seconds, e.g. ``120``.
        :returns: Chat Completions response dict.
        """
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            return _gemini_to_chat(resp.json(), model)

    async def _stream_request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int = _STREAM_TIMEOUT,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Send a streaming Gemini request.

        :param url: The streaming endpoint URL.
        :param headers: HTTP headers.
        :param payload: Gemini API payload.
        :param timeout: Request timeout in seconds, e.g. ``300``.
        :returns: Async iterator of Chat Completions chunk dicts.
        """
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
            ) as resp,
        ):
            # Buffer error bodies before raising: a streamed response
            # is unread, so exc.response.text would raise
            # ResponseNotRead and error classification (e.g.
            # context-overflow detection) would never see the
            # provider's message.
            if resp.status_code >= 400:
                await resp.aread()
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[len("data: ") :])
                for chunk in _gemini_stream_chunk_to_chat(
                    data,
                ):
                    yield chunk


# ── Request translation ───────────────────────────────────


def _chat_to_gemini(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert Chat Completions messages to Gemini generateContent payload.

    :param messages: Chat Completions messages.
    :param tools: OpenAI-format tool schemas or ``None``.
    :param extra: Additional kwargs.
    :returns: Gemini API request payload.
    """
    payload: dict[str, Any] = {}

    # System instruction
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    if system_parts:
        payload["system_instruction"] = {"parts": [{"text": text} for text in system_parts]}

    # Convert messages
    contents: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue

        gemini_role = "model" if role == "assistant" else "user"

        if role == "assistant" and m.get("tool_calls"):
            parts = _assistant_tool_calls_to_parts(m)
            contents.append({"role": gemini_role, "parts": parts})
        elif role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                # Gemini requires the function name in the response;
                                # _tool_name is injected by the caller. Falls back to
                                # "function" for legacy tool messages that lack it.
                                "name": m.get("_tool_name", "function"),
                                "response": {"result": m["content"]},
                            }
                        }
                    ],
                }
            )
        else:
            parts = _content_to_gemini_parts(m.get("content"))
            contents.append({"role": gemini_role, "parts": parts})

    payload["contents"] = contents

    # Generation config
    gen_config: dict[str, Any] = {}
    remaining = dict(extra)
    for openai_key, gemini_key in _GENERATION_CONFIG_KEYS.items():
        if openai_key in remaining:
            gen_config[gemini_key] = remaining.pop(openai_key)
    if "temperature" in remaining:
        gen_config["temperature"] = remaining.pop("temperature")

    if gen_config:
        payload["generationConfig"] = gen_config

    # Tools
    if tools:
        payload["tools"] = [{"functionDeclarations": _convert_tools(tools)}]

    return payload


def _assistant_tool_calls_to_parts(
    m: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Convert assistant tool calls to Gemini functionCall parts.

    :param m: Assistant message with ``tool_calls``.
    :returns: List of Gemini parts.
    """
    parts: list[dict[str, Any]] = []
    if content := m.get("content"):
        parts.append({"text": content})
    for tc in m["tool_calls"]:
        func = tc["function"]
        parts.append(
            {
                "functionCall": {
                    "name": func["name"],
                    "args": json.loads(func["arguments"]),
                }
            }
        )
    return parts


def _content_to_gemini_parts(
    content: list[dict[str, Any]] | str | None,
) -> list[dict[str, Any]]:
    """
    Convert Chat Completions content to Gemini ``parts`` array.

    Handles string content (text-only), list content (multimodal),
    and ``None`` (empty parts).

    :param content: Chat Completions content — string, list of
        content part dicts, or ``None``.
    :returns: Gemini parts list.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}]
    return [_translate_part_to_gemini(part) for part in content]


def _translate_part_to_gemini(part: dict[str, Any]) -> dict[str, Any]:
    """
    Translate a single Chat Completions content part to Gemini format.

    - ``text`` → ``{"text": "..."}``
    - ``image_url`` with data URI → ``{"inlineData":
      {"mimeType": "...", "data": "..."}}``
    - ``image_url`` with external URL → ``{"text": "[image: <url>]"}``
      (Gemini does not support URL references in content parts)
    - ``input_file`` with file_data → ``{"inlineData":
      {"mimeType": "...", "data": "..."}}``
    - Unrecognized → passed through as-is.

    :param part: A Chat Completions content part dict.
    :returns: A Gemini part dict.
    """
    part_type = part.get("type")

    if part_type == "text":
        return {"text": part["text"]}

    if part_type == "image_url":
        image_url = part["image_url"]
        url = image_url["url"]
        parsed = parse_data_uri(url)
        if parsed is not None:
            return {
                "inlineData": {
                    "mimeType": parsed.media_type,
                    "data": parsed.data,
                },
            }
        # Gemini does not support URL references in content parts.
        # Pass URL as text so the model at least sees the reference.
        return {"text": f"[image: {url}]"}

    if part_type == "input_file":
        # file_data is a data: URI (e.g. "data:application/pdf;base64,...").
        # content_resolver guarantees this format; fail loud if violated.
        file_uri = parse_data_uri(part["file_data"])
        if file_uri is None:
            raise ValueError(
                f"input_file file_data must be a data: URI, got: {part['file_data'][:80]!r}"
            )
        return {
            "inlineData": {
                "mimeType": file_uri.media_type,
                "data": file_uri.data,
            },
        }

    # Unrecognized part type — pass through for forward compat.
    return part


def _convert_tools(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert OpenAI tool schemas to Gemini functionDeclarations.

    :param tools: OpenAI-format tool definitions.
    :returns: Gemini functionDeclaration list.
    """
    declarations = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool["function"]
        decl: dict[str, Any] = {
            "name": func["name"],
        }
        if desc := func.get("description"):
            decl["description"] = desc
        if params := func.get("parameters"):
            decl["parameters"] = params
        declarations.append(decl)
    return declarations


# ── Response translation ──────────────────────────────────


def _gemini_to_chat(
    resp: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """
    Convert a Gemini generateContent response to Chat Completions format.

    :param resp: Gemini response dict.
    :param model: Model name for the response.
    :returns: Chat Completions response dict.
    """
    candidates = resp.get("candidates", [])
    if not candidates:
        return _empty_chat_response(model)

    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts", [])

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for part in parts:
        if "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            # Gemini doesn't provide call IDs — generate deterministic ones
            call_id = hashlib.md5(json.dumps(fc, sort_keys=True).encode()).hexdigest()[:12]
            tool_calls.append(
                {
                    "id": f"call_{call_id}",
                    "type": "function",
                    "function": {
                        "name": fc["name"],
                        "arguments": json.dumps(fc.get("args", {})),
                    },
                }
            )

    finish_reason = _normalize_finish_reason(candidate.get("finishReason"))

    content = "\n".join(text_parts) if text_parts else None
    usage = _extract_usage(resp.get("usageMetadata", {}))

    return {
        "id": f"gemini-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
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
        "usage": usage,
    }


def _gemini_stream_chunk_to_chat(
    data: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """
    Convert a single Gemini streaming chunk to Chat Completions chunks.

    :param data: A parsed Gemini SSE data dict.
    :returns: Iterator of Chat Completions chunk dicts.
    """
    candidates = data.get("candidates", [])
    if not candidates:
        # Usage-only chunk
        if usage_meta := data.get("usageMetadata"):
            yield {
                "id": f"gemini-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": None,
                "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                "usage": _extract_usage(usage_meta),
            }
        return

    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts", [])
    finish_reason = _normalize_finish_reason(candidate.get("finishReason"))

    # Each parallel function call needs a distinct ``tool_calls`` index. The
    # downstream accumulator (``chat_stream_to_response_events``) keys tool
    # calls by this index, overwriting name/id and *appending* arguments for a
    # repeated index. Gemini returns parallel calls as multiple ``functionCall``
    # parts in one chunk, so a fixed ``0`` would collapse them into a single
    # corrupted call. Count tool calls so each gets its own index.
    tool_call_index = 0
    for part in parts:
        if "text" in part:
            yield {
                "id": f"gemini-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": None,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": part["text"]},
                        "finish_reason": None,
                    }
                ],
            }
        elif "functionCall" in part:
            fc = part["functionCall"]
            call_id = hashlib.md5(json.dumps(fc, sort_keys=True).encode()).hexdigest()[:12]
            yield {
                "id": f"gemini-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": None,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tool_call_index,
                                    "id": f"call_{call_id}",
                                    "type": "function",
                                    "function": {
                                        "name": fc["name"],
                                        "arguments": json.dumps(fc.get("args", {})),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            tool_call_index += 1

    if finish_reason:
        yield {
            "id": f"gemini-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": None,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }
            ],
        }


def _normalize_finish_reason(reason: str | None) -> str | None:
    """
    Normalize Gemini finish reason to OpenAI format.

    :param reason: Gemini finish reason string or ``None``.
    :returns: OpenAI-style finish reason.
    """
    if not reason:
        return None
    if reason == "MAX_TOKENS":
        return "length"
    if reason == "STOP":
        return "stop"
    return reason.lower()


def _extract_usage(meta: dict[str, Any]) -> dict[str, Any]:
    """
    Extract usage from Gemini usageMetadata.

    :param meta: Gemini usageMetadata dict.
    :returns: Chat Completions usage dict.
    """
    prompt = meta.get("promptTokenCount")
    completion = meta.get("candidatesTokenCount")
    total = meta.get("totalTokenCount")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _empty_chat_response(model: str) -> dict[str, Any]:
    """
    Return an empty Chat Completions response.

    :param model: Model name.
    :returns: Chat Completions response with no content.
    """
    return {
        "id": f"gemini-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        },
    }
