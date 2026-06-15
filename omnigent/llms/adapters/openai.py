"""
OpenAI and OpenAI-compatible provider adapter.

Handles OpenAI, Groq, DeepSeek, xAI, OpenRouter, and Ollama — any
provider that speaks the OpenAI Chat Completions API format.
"""

from __future__ import annotations

import codecs
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.base import BaseAdapter
from omnigent.llms.types import (
    NATIVE_TOOL_OUTPUT_TYPES,
    FunctionCallOutput,
    MessageOutput,
    NativeToolOutput,
    NativeToolOutputAddedEvent,
    OutputText,
    Response,
    ResponseCompletedEvent,
    ResponseReasoningStartedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
    Usage,
)

_logger = logging.getLogger(__name__)

# Timeout for non-streaming requests (seconds)
_REQUEST_TIMEOUT = 120

# Timeout for streaming connection (seconds)
_STREAM_TIMEOUT = 300


class OpenAICompatibleAdapter(BaseAdapter):
    """
    Adapter for providers using the OpenAI Chat Completions format.

    API keys and base URL overrides come from ``connection_params``
    at call time (from the ``connection:`` block in agent spec).

    :param base_url: The provider's default API base URL, e.g.
        ``"https://api.openai.com/v1"``. ``None`` for providers
        that always require ``connection_params["base_url"]``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        # Kept for backward compat with tests; no longer used at runtime.
        api_key_env: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None

    def _build_headers(
        self,
        api_key_override: str | None = None,
    ) -> dict[str, str]:
        """
        Build HTTP headers for the request.

        :param api_key_override: API key from ``connection_params``.
            ``None`` means no auth header is added.
        :returns: Headers dict with Authorization if an API key is
            provided.
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key_override:
            headers["Authorization"] = f"Bearer {api_key_override}"
        return headers

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Build the Chat Completions request payload.

        :param messages: Chat Completions messages.
        :param model: Model name without provider prefix.
        :param tools: Tool schemas or ``None``.
        :param stream: Whether to enable streaming.
        :param extra: Additional kwargs (temperature, etc.).
        :returns: The request payload dict.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            **extra,
        }
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
            payload.setdefault("stream_options", {"include_usage": True})
        return payload

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
        Send a Chat Completions request to the provider.

        :param messages: Chat Completions messages.
        :param model: Model name, e.g. ``"gpt-5.4"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Enable streaming.
        :param extra: Additional kwargs.
        :param connection_params: Per-call overrides. Supported keys:
            ``"api_key"``, ``"base_url"``.
        :param timeout: Request timeout in seconds. ``None`` uses
            the module default.
        :returns: Response dict or async iterator of chunk dicts.
        """
        params = connection_params or {}
        payload = self._build_payload(
            messages,
            model,
            tools,
            stream,
            extra,
        )
        effective_base = _resolve_base_url(
            params.get("base_url"),
            self._base_url,
        )
        url = f"{effective_base}/chat/completions"
        headers = self._build_headers(
            api_key_override=params.get("api_key"),
        )

        if stream:
            effective_timeout = timeout if timeout is not None else _STREAM_TIMEOUT
            return self._stream_request(
                url,
                headers,
                payload,
                effective_timeout,
            )
        effective_timeout = timeout if timeout is not None else _REQUEST_TIMEOUT
        return await self._send_request(
            url,
            headers,
            payload,
            effective_timeout,
        )

    async def _send_request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int = _REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """
        Send a non-streaming HTTP POST and return the JSON response.

        :param url: The full endpoint URL.
        :param headers: HTTP headers.
        :param payload: JSON payload.
        :param timeout: Request timeout in seconds, e.g. ``120``.
        :returns: Parsed JSON response dict.
        :raises httpx.HTTPStatusError: On non-2xx status.
        """
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def _stream_request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int = _STREAM_TIMEOUT,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Send a streaming HTTP POST and yield parsed SSE data chunks.

        :param url: The full endpoint URL.
        :param headers: HTTP headers.
        :param payload: JSON payload with ``stream: true``.
        :param timeout: Request timeout in seconds, e.g. ``300``.
        :returns: Async iterator of parsed Chat Completions chunk
            dicts.
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
            if resp.status_code >= 400:
                await resp.aread()
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                parsed = _parse_sse_line(line)
                if parsed is not None:
                    yield parsed


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """
    Parse a single SSE line into a data dict.

    Ignores non-data lines (event:, id:, comments) and the
    ``[DONE]`` sentinel.

    :param line: A raw SSE line, e.g. ``"data: {\"id\": ...}"``.
    :returns: Parsed JSON dict, or ``None`` if the line should
        be skipped.
    """
    if not line.startswith("data: "):
        return None
    data = line[len("data: ") :]
    if data.strip() == "[DONE]":
        return None
    result: dict[str, Any] = json.loads(data)
    return result


def _resolve_base_url(
    override: str | None,
    default: str | None,
) -> str:
    """
    Resolve the effective base URL from override or default.

    :param override: Per-call base URL from ``connection_params``.
    :param default: Adapter's default base URL (``None`` for providers
        that always require ``connection_params``).
    :returns: The resolved base URL, stripped of trailing slashes.
    :raises OmnigentError: If neither override nor default is available.
    """
    if override:
        return override.rstrip("/")
    if default:
        return default
    raise OmnigentError(
        "No base_url available — provide 'base_url' in"
        " connection_params (from llm.connection config)",
        code=ErrorCode.INVALID_INPUT,
    )


def _to_responses_tools(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert Chat Completions tool schemas to Responses API format.

    Chat Completions uses ``{"type": "function", "function": {"name":
    ..., "description": ..., "parameters": ...}}``. The Responses API
    expects ``{"type": "function", "name": ..., "description": ...,
    "parameters": ...}`` (top-level, no nesting).

    If a tool is already in Responses API format (has ``"name"`` at
    the top level), it is passed through unchanged.

    :param tools: Tool schemas in Chat Completions format.
    :returns: Tool schemas in Responses API format.
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if "function" in tool and "name" not in tool:
            # Chat Completions format — flatten
            fn = tool["function"]
            entry: dict[str, Any] = {
                # Chat Completions tools always have type "function"
                # per the OpenAI spec; default is spec-mandated.
                "type": tool.get("type", "function"),
                "name": fn["name"],
                # Empty dict is valid JSON Schema for "no parameters".
                "parameters": fn.get("parameters", {}),
            }
            if desc := fn.get("description"):
                entry["description"] = desc
            converted.append(entry)
        else:
            # Already in Responses format or unknown — pass through
            converted.append(tool)
    return converted


def _parse_responses_output(
    output_items: list[dict[str, Any]],
) -> list[MessageOutput | FunctionCallOutput | NativeToolOutput]:
    """
    Convert Responses API output items to ``llms.types`` output objects.

    ``message`` and ``function_call`` items are parsed into typed
    objects. Provider-native tool items (e.g. ``web_search_call``)
    and ``reasoning`` items are wrapped in :class:`NativeToolOutput`
    and passed through as raw dicts. Reasoning items must be
    preserved because OpenAI requires them when replaying
    ``web_search_call`` items as input.

    :param output_items: List of output item dicts from the Responses
        API response, e.g. the ``response.output`` list.
    :returns: List of :class:`MessageOutput`, :class:`FunctionCallOutput`,
        and/or :class:`NativeToolOutput` instances.
    """
    output: list[MessageOutput | FunctionCallOutput | NativeToolOutput] = []
    for item in output_items:
        item_type = item.get("type")
        if item_type == "message":
            parts = [
                OutputText(text=p["text"])
                for p in item.get("content", [])
                if p.get("type") == "output_text" and p.get("text")
            ]
            if parts:
                output.append(MessageOutput(content=parts))
        elif item_type == "function_call":
            output.append(
                FunctionCallOutput(
                    call_id=item["call_id"],
                    name=item["name"],
                    arguments=item["arguments"],
                )
            )
        elif item_type in NATIVE_TOOL_OUTPUT_TYPES or item_type == "reasoning":
            output.append(NativeToolOutput(data=item))
    return output


def _parse_responses_response(data: dict[str, Any]) -> Response:
    """
    Convert a Responses API response dict to a :class:`Response`.

    :param data: The full Responses API response JSON dict.
    :returns: A :class:`Response` with parsed output and usage.
    """
    output = _parse_responses_output(data.get("output", []))
    usage_data: dict[str, Any] = data.get("usage") or {}
    usage = (
        Usage(
            input_tokens=usage_data.get("input_tokens"),
            output_tokens=usage_data.get("output_tokens"),
            total_tokens=usage_data.get("total_tokens"),
        )
        if usage_data
        else None
    )
    model = data.get("model")
    if model is None:
        raise OmnigentError(
            "Response missing required 'model' field",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return Response(output=output, model=model, usage=usage)


def _parse_responses_event(
    event_type: str,
    data: dict[str, Any],
) -> ResponseStreamEvent | None:
    """
    Convert a single Responses API SSE event to a
    :class:`ResponseStreamEvent`, or ``None`` if the event type
    is not handled.

    :param event_type: The SSE event name, e.g.
        ``"response.output_text.delta"``.
    :param data: The parsed JSON payload from the ``data:`` line.
    :returns: A streaming event dataclass, or ``None``.
    """
    if event_type == "response.output_text.delta":
        return ResponseTextDeltaEvent(delta=data["delta"])
    if event_type == "response.reasoning_summary_text.delta":
        return ResponseReasoningSummaryTextDeltaEvent(delta=data["delta"])
    if event_type == "response.reasoning_text.delta":
        return ResponseReasoningTextDeltaEvent(delta=data["delta"])
    if event_type == "response.output_item.added":
        item = data.get("item", {})
        item_type = item.get("type")
        if item_type == "reasoning":
            return ResponseReasoningStartedEvent()
    if event_type == "response.output_item.done":
        item = data.get("item", {})
        # Include reasoning items alongside native tool items — OpenAI
        # requires them when replaying web_search_call as input.
        if item.get("type") in NATIVE_TOOL_OUTPUT_TYPES or item.get("type") == "reasoning":
            return NativeToolOutputAddedEvent(item=item)
    if event_type == "response.completed":
        return ResponseCompletedEvent(response=_parse_responses_response(data["response"]))
    return None


class OpenAIAdapter(OpenAICompatibleAdapter):
    """
    OpenAI-specific adapter that calls ``/v1/responses`` natively.

    Extends :class:`OpenAICompatibleAdapter` (which uses Chat
    Completions) by adding :meth:`responses_create` — a direct
    Responses API path that preserves reasoning token streaming events
    that Chat Completions does not expose.

    :param base_url: The OpenAI API base URL.
    :param api_key_env: Environment variable name for the API key.
    """

    async def responses_create(
        self,
        *,
        input: list[dict[str, Any]],
        instructions: str | None,
        model: str,
        tools: list[dict[str, Any]] | None,
        reasoning: dict[str, str] | None,
        stream: bool,
        connection_params: dict[str, str] | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> Response | AsyncIterator[ResponseStreamEvent]:
        """
        Call the OpenAI Responses API (``/v1/responses``) directly.

        Used instead of Chat Completions so that reasoning token
        streaming events (``response.reasoning_summary_text.delta``,
        ``response.reasoning_text.delta``) flow through unmodified.

        :param input: Responses API input items.
        :param instructions: System instructions string, or ``None``.
        :param model: Model name without provider prefix, e.g.
            ``"o4-mini"``.
        :param tools: OpenAI-format tool schemas, or ``None``.
        :param reasoning: Reasoning config dict, e.g.
            ``{"effort": "high", "summary": "detailed"}``,
            or ``None``.
        :param stream: If ``True``, return an async iterator of
            :class:`ResponseStreamEvent`. If ``False``, return a
            :class:`Response`.
        :param connection_params: Per-call overrides. Supported keys:
            ``"api_key"``, ``"base_url"``.
        :param timeout: Request timeout in seconds. ``None`` uses
            the module default.
        :param kwargs: Additional API kwargs (temperature, etc.).
        :returns: A :class:`Response` or an async iterator of
            :class:`ResponseStreamEvent`.
        """
        params = connection_params or {}
        payload: dict[str, Any] = {
            "model": model,
            "input": input,
            **kwargs,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            # Convert Chat Completions tool format to Responses
            # API format: flatten {"type", "function": {...}} to
            # {"type", "name", "description", "parameters", ...}
            payload["tools"] = _to_responses_tools(tools)
        if reasoning:
            payload["reasoning"] = reasoning
        if stream:
            payload["stream"] = True

        effective_base = _resolve_base_url(
            params.get("base_url"),
            self._base_url,
        )
        url = f"{effective_base}/responses"
        headers = self._build_headers(
            api_key_override=params.get("api_key"),
        )

        if stream:
            effective_to = timeout if timeout is not None else _STREAM_TIMEOUT
            return self._stream_responses(
                url,
                headers,
                payload,
                effective_to,
            )
        effective_to = timeout if timeout is not None else _REQUEST_TIMEOUT
        resp_data = await self._send_request(
            url,
            headers,
            payload,
            effective_to,
        )
        return _parse_responses_response(resp_data)

    async def _stream_responses(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int = _STREAM_TIMEOUT,
    ) -> AsyncIterator[ResponseStreamEvent]:
        """
        Stream the Responses API and yield typed
        :class:`ResponseStreamEvent` instances.

        Parses SSE ``event:`` + ``data:`` pairs, mapping each to the
        appropriate event dataclass. Unknown event types are skipped.

        :param url: The ``/v1/responses`` endpoint URL.
        :param headers: HTTP headers including Authorization.
        :param payload: The request payload with ``stream: true``.
        :param timeout: Request timeout in seconds, e.g. ``300``.
        :yields: :class:`ResponseStreamEvent` instances.
        """
        current_event: str | None = None
        buf = ""
        # Decode incrementally: httpx yields arbitrary byte chunks, so a
        # multi-byte UTF-8 character can be split across a chunk boundary.
        # An incremental decoder buffers the partial sequence until the rest
        # arrives, instead of emitting U+FFFD replacement chars per chunk.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
            ) as resp,
        ):
            if resp.status_code >= 400:
                body = await resp.aread()
                _logger.error(
                    "OpenAI Responses API %s: %s",
                    resp.status_code,
                    body.decode("utf-8", errors="replace")[:2000],
                )
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                buf += decoder.decode(chunk)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.startswith("event: "):
                        current_event = line[7:]
                    elif line.startswith("data: ") and current_event:
                        data_str = line[6:]
                        if data_str.strip() != "[DONE]":
                            event = _parse_responses_event(
                                current_event,
                                json.loads(data_str),
                            )
                            if event is not None:
                                yield event
                        current_event = None
