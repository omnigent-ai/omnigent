"""
Google Gemini adapter.

Translates Chat Completions format to/from Gemini's generateContent
API. Ported from MLflow AI Gateway's GeminiAdapter.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms._responses_to_chat import extract_thought_signature
from omnigent.llms.adapters._content import parse_data_uri
from omnigent.llms.adapters.base import BaseAdapter
from omnigent.llms.errors import PermanentLLMError
from omnigent.util.reasoning_effort import GEMINI_EFFORTS, validate_effort_or_llm_error

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

# Gemini 2.5 token budgets per reasoning effort (within 2.5 Flash/Pro/Lite limits).
_GEMINI_25_THINKING_BUDGETS: dict[str, int] = {
    "low": 1024,
    "medium": 8192,
    "high": 24576,
}

# Matches major and optional minor version of a Gemini model id (e.g. ``gemini-2.5-pro``).
_GEMINI_VERSION_RE = re.compile(
    r"(?:^|/)gemini-(\d+)(?:[.\-](\d+))?(?:[.\-]|$)",
    re.IGNORECASE,
)


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
        payload = _chat_to_gemini(messages, tools, extra, model=model)
        headers = await self._get_headers(
            api_key_override=params.get("api_key"),
        )
        override_base = params.get("base_url")
        effective_base = override_base.rstrip("/") if override_base else self._get_base_url()
        model_path = _build_model_path(effective_base, model)

        if stream:
            effective_to = timeout if timeout is not None else _STREAM_TIMEOUT
            url = f"{model_path}:streamGenerateContent?alt=sse"
            return self._stream_request(
                url,
                headers,
                payload,
                effective_to,
            )

        effective_to = timeout if timeout is not None else _REQUEST_TIMEOUT
        url = f"{model_path}:generateContent"
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
            next_tool_call_index = 0
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[len("data: ") :])
                for chunk in _gemini_stream_chunk_to_chat(
                    data,
                    tool_call_index_start=next_tool_call_index,
                ):
                    delta_calls = (
                        (chunk.get("choices") or [{}])[0].get("delta", {}).get("tool_calls")
                    )
                    if delta_calls:
                        next_tool_call_index += len(delta_calls)
                    yield chunk


# ── Request translation ───────────────────────────────────


def _build_model_path(base_url: str, model: str) -> str:
    """Build ``<base>/models/<model>`` without duplicating a trailing ``/models`` segment."""
    trimmed = base_url.rstrip("/")
    normalized_model = model.lstrip("/").removeprefix("models/")
    if trimmed.endswith("/models"):
        return f"{trimmed}/{normalized_model}"
    return f"{trimmed}/models/{normalized_model}"


def _gemini_version(model: str) -> tuple[int, int] | None:
    """Parse ``(major, minor)`` from a Gemini model id, or ``None`` if unversioned."""
    match = _GEMINI_VERSION_RE.search(model)
    if match is None:
        return None
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else 0
    return (major, minor)


def _build_thinking_config(
    effort: str,
    model: str,
    *,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Build Gemini ``generationConfig.thinkingConfig`` for a validated reasoning effort."""
    version = _gemini_version(model)
    if version is not None and version < (2, 5) and "thinking" not in model.lower():
        raise PermanentLLMError(
            f"Model {model!r} does not support reasoning_effort",
            code="unsupported_reasoning_effort",
        )
    if version is not None and version < (3, 0):
        budget = _GEMINI_25_THINKING_BUDGETS[effort]
        if isinstance(max_output_tokens, int) and max_output_tokens > 0:
            budget = min(budget, max_output_tokens)
        return {
            "thinkingBudget": budget,
            "includeThoughts": True,
        }
    return {
        "thinkingLevel": effort.upper(),
        "includeThoughts": True,
    }


def _chat_to_gemini(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    extra: dict[str, Any],
    model: str = "",
) -> dict[str, Any]:
    """
    Convert Chat Completions messages to Gemini generateContent payload.

    :param messages: Chat Completions messages.
    :param tools: OpenAI-format tool schemas or ``None``.
    :param extra: Additional kwargs.
    :param model: Bare Gemini model name, e.g. ``"gemini-3-pro-preview"``
        or ``"gemini-2.5-pro"``. Used to select ``thinkingLevel`` vs
        ``thinkingBudget`` when ``reasoning_effort`` is provided.
    :returns: Gemini API request payload.
    """
    payload: dict[str, Any] = {}

    # System instruction
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    if system_parts:
        payload["system_instruction"] = {"parts": [{"text": text} for text in system_parts]}

    # Convert messages
    contents: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    prev_was_tool_response = False
    for m in messages:
        role = m["role"]
        if role == "system":
            continue

        gemini_role = "model" if role == "assistant" else "user"

        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tc_id = tc.get("id")
                func_name = (tc.get("function") or {}).get("name")
                if isinstance(tc_id, str) and tc_id and isinstance(func_name, str) and func_name:
                    tool_names_by_id[tc_id] = func_name
            parts = _assistant_tool_calls_to_parts(m)
            contents.append({"role": gemini_role, "parts": parts})
            prev_was_tool_response = False
        elif role == "tool":
            # Resolve functionResponse.name from explicit _tool_name or the matching
            # preceding tool_call; fall back to "function" for legacy standalone messages.
            tool_call_id = m.get("tool_call_id")
            resolved_name = (
                m.get("_tool_name")
                or (tool_names_by_id.get(tool_call_id) if isinstance(tool_call_id, str) else None)
                or "function"
            )
            response_part = {
                "functionResponse": {
                    "name": resolved_name,
                    "response": {"result": m["content"]},
                }
            }
            # Gemini requires parallel functionResponse parts to be grouped in one user turn.
            if prev_was_tool_response and contents:
                contents[-1]["parts"].append(response_part)
            else:
                contents.append({"role": "user", "parts": [response_part]})
                prev_was_tool_response = True
        else:
            parts = _content_to_gemini_parts(m.get("content"))
            contents.append({"role": gemini_role, "parts": parts})
            prev_was_tool_response = False

    payload["contents"] = contents

    # Generation config
    gen_config: dict[str, Any] = {}
    remaining = dict(extra)
    for openai_key, gemini_key in _GENERATION_CONFIG_KEYS.items():
        if openai_key in remaining:
            gen_config[gemini_key] = remaining.pop(openai_key)
    if "temperature" in remaining:
        gen_config["temperature"] = remaining.pop("temperature")
    reasoning_effort = remaining.pop("reasoning_effort", None)
    if reasoning_effort is not None and reasoning_effort != "":
        effort = validate_effort_or_llm_error(reasoning_effort, "Gemini", GEMINI_EFFORTS)
        if effort is not None:
            raw_max_output = gen_config.get("maxOutputTokens")
            gen_config["thinkingConfig"] = _build_thinking_config(
                effort,
                model,
                max_output_tokens=raw_max_output if isinstance(raw_max_output, int) else None,
            )

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
        raw_args = func.get("arguments")
        args = json.loads(raw_args) if raw_args else {}
        part: dict[str, Any] = {
            "functionCall": {
                "name": func["name"],
                "args": args,
            }
        }
        if thought_signature := extract_thought_signature(tc):
            part["thoughtSignature"] = thought_signature
        parts.append(part)
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


def _gemini_function_call_to_tool_call(
    part: dict[str, Any],
    *,
    index: int | None = None,
) -> dict[str, Any]:
    """Convert a Gemini ``functionCall`` part to a Chat Completions tool call dict."""
    fc = part["functionCall"]
    raw_id = fc.get("id")
    if isinstance(raw_id, str) and raw_id:
        call_id = raw_id
    else:
        # Fresh UUID avoids call_id collisions for identical parallel or repeated calls.
        call_id = f"call_{uuid.uuid4().hex[:12]}"

    tool_call: dict[str, Any] = {
        "id": call_id,
        "type": "function",
        "function": {
            "name": fc["name"],
            "arguments": json.dumps(fc.get("args") or {}),
        },
    }
    if index is not None:
        tool_call["index"] = index
    if thought_signature := extract_thought_signature(part):
        tool_call["thoughtSignature"] = thought_signature
    return tool_call


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
    thought_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for part in parts:
        if "text" in part:
            text = part["text"]
            if isinstance(text, str) and text:
                if part.get("thought") is True:
                    thought_parts.append(text)
                else:
                    text_parts.append(text)
        if "functionCall" in part:
            tool_calls.append(_gemini_function_call_to_tool_call(part))

    finish_reason = _normalize_finish_reason(candidate.get("finishReason"))

    content = "\n".join(text_parts) if text_parts else None
    usage = _extract_usage(resp.get("usageMetadata", {}))

    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or None,
    }
    if thought_parts:
        message["reasoning_content"] = "\n".join(thought_parts)

    return {
        "id": f"gemini-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


def _gemini_stream_chunk_to_chat(
    data: dict[str, Any],
    *,
    tool_call_index_start: int = 0,
) -> Iterator[dict[str, Any]]:
    """
    Convert a single Gemini streaming chunk to Chat Completions chunks.

    :param data: A parsed Gemini SSE data dict.
    :param tool_call_index_start: Starting tool-call index for this frame so
        multi-frame parallel calls keep distinct indices.
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
    tool_call_index = tool_call_index_start
    for part in parts:
        if "text" in part:
            text = part["text"]
            if isinstance(text, str) and text:
                delta: dict[str, Any] = (
                    {"reasoning_content": text}
                    if part.get("thought") is True
                    else {"content": text}
                )
                yield {
                    "id": f"gemini-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": None,
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": None,
                        }
                    ],
                }
        if "functionCall" in part:
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
                                _gemini_function_call_to_tool_call(part, index=tool_call_index)
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
