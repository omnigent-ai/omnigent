"""
Response and streaming event types for the LLM client.

These dataclasses mirror the OpenAI Responses API types so that
``workflow.py``'s ``_response_to_dict()`` and ``_accumulate_stream()``
work unchanged — they access ``.type``, ``.output``, ``.delta``,
``.response``, ``.content``, ``.text``, ``.call_id``, ``.name``,
and ``.arguments`` attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.llms.prompt_cache import PromptCacheObservation


@dataclass
class OutputText:
    """
    A text content part within a message output.

    :param text: The text content, e.g. ``"Hello! How can I help?"``.
    :param type: Always ``"output_text"``.
    :param annotations: Optional list of annotations (e.g.
        ``file_citation``) referencing files the agent produced.
        ``None`` when no annotations are present.
    """

    text: str
    type: str = "output_text"
    annotations: list[dict[str, Any]] | None = None


@dataclass
class MessageOutput:
    """
    An assistant message in the response output.

    :param content: List of content parts, e.g.
        ``[OutputText(text="Hello")]``.
    :param type: Always ``"message"``.
    """

    content: list[OutputText]
    type: str = "message"


@dataclass
class FunctionCallOutput:
    """
    A tool/function call in the response output.

    :param call_id: Unique identifier for the tool call, e.g.
        ``"call_abc123"``.
    :param name: The function name, e.g. ``"get_weather"``.
    :param arguments: JSON-encoded arguments string, e.g.
        ``'{"city": "London"}'``.
    :param type: Always ``"function_call"``.
    """

    call_id: str
    name: str
    arguments: str
    type: str = "function_call"


# OpenAI-native tool types that are executed server-side and
# passed through to the client without local dispatch.
NATIVE_TOOL_OUTPUT_TYPES: frozenset[str] = frozenset(
    {
        "web_search_call",
        "file_search_call",
        "code_interpreter_call",
        "computer_call",
        "image_generation_call",
        "mcp_call",
        "mcp_list_tools",
    }
)


@dataclass
class NativeToolOutput:
    """
    A provider-native tool output item passed through as a raw dict.

    Native tools (e.g. ``web_search_call``, ``file_search_call``)
    are executed server-side by the LLM provider. Agent-plane does
    not dispatch them locally — they flow through to the client.

    :param data: The full raw dict from the Responses API,
        e.g. ``{"type": "web_search_call", "id": "ws_abc",
        "status": "completed"}``.
    """

    # Any: native tool output dicts are heterogeneous and
    # provider-defined — we pass them through without parsing.
    data: dict[str, Any]


@dataclass
class Usage:
    """
    Token usage information.

    :param input_tokens: Number of input/prompt tokens.
    :param output_tokens: Number of output/completion tokens.
    :param total_tokens: Total tokens (input + output).
    :param cache_read_input_tokens: Prompt tokens served from the provider's
        prompt cache, or ``None`` when the provider did not report it.
    :param cache_creation_input_tokens: Prompt tokens written to the
        provider's prompt cache, or ``None`` when not reported.
    :param input_tokens_include_cache: ``True`` when ``input_tokens`` already
        counts cached reads (OpenAI); ``False`` when cache counts are additive
        (Anthropic).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    input_tokens_include_cache: bool = False

    def to_cost_usage(self) -> dict[str, int]:
        """
        Usage in the additive shape :func:`compute_llm_cost` prices.

        ``input_tokens`` is the uncached portion; cache reads and writes are
        separate so they are billed at their own rates.

        :returns: e.g. ``{"input_tokens": 200, "output_tokens": 50,
            "cache_read_input_tokens": 1800}``.
        """
        read = self.cache_read_input_tokens or 0
        write = self.cache_creation_input_tokens or 0
        input_tokens = self.input_tokens or 0
        if self.input_tokens_include_cache:
            input_tokens = max(input_tokens - read, 0)
        usage = {"input_tokens": input_tokens, "output_tokens": self.output_tokens or 0}
        if read:
            usage["cache_read_input_tokens"] = read
        if write:
            usage["cache_creation_input_tokens"] = write
        return usage


@dataclass
class Response:
    """
    A completed LLM response.

    :param output: List of output items — ``MessageOutput``,
        ``FunctionCallOutput``, or ``NativeToolOutput`` instances.
        Native tool outputs are provider-executed items (e.g.
        ``web_search_call``) passed through as raw dicts.
    :param model: The model identifier that produced the response,
        e.g. ``"claude-sonnet-4-20250514"``.
    :param usage: Token usage information, or ``None`` if unavailable.
    :param prompt_cache: Normalized prompt-cache observation set by the
        client, or ``None`` when the response did not pass through it.
    """

    output: list[MessageOutput | FunctionCallOutput | NativeToolOutput]
    model: str
    usage: Usage | None = None
    prompt_cache: PromptCacheObservation | None = None


# ── Streaming event types ─────────────────────────────────


@dataclass
class ResponseTextDeltaEvent:
    """
    Incremental text token from the assistant.

    :param delta: The text fragment, e.g. ``"Hello"``.
    :param type: Always ``"response.output_text.delta"``.
    """

    delta: str
    type: str = "response.output_text.delta"


@dataclass
class ResponseReasoningTextDeltaEvent:
    """
    Incremental reasoning token (full chain-of-thought).
    Only emitted by providers that support reasoning (e.g. OpenAI).

    :param delta: The reasoning text fragment.
    :param type: Always ``"response.reasoning_text.delta"``.
    """

    delta: str
    type: str = "response.reasoning_text.delta"


@dataclass
class ResponseReasoningSummaryTextDeltaEvent:
    """
    Incremental reasoning summary token.
    Only emitted when ``reasoning.summary`` is configured.

    :param delta: The summary text fragment.
    :param type: Always ``"response.reasoning_summary_text.delta"``.
    """

    delta: str
    type: str = "response.reasoning_summary_text.delta"


@dataclass
class ResponseReasoningStartedEvent:
    """
    Emitted once when a reasoning block begins.

    Fired when the model starts reasoning, even when reasoning content
    is encrypted and no delta events will follow. Allows clients to
    show a ``[thinking...]`` indicator regardless of org verification
    status.

    :param type: Always ``"response.reasoning.started"``.
    """

    type: str = "response.reasoning.started"


@dataclass
class NativeToolOutputAddedEvent:
    """
    Emitted when a provider-native tool output item completes
    during streaming (e.g. ``web_search_call``).

    :param item: The full raw dict from the Responses API,
        e.g. ``{"type": "web_search_call", "id": "ws_abc",
        "status": "completed"}``.
    :param type: Always ``"response.output_item.done"``.
    """

    # Any: native tool output dicts are heterogeneous and
    # provider-defined — we pass them through without parsing.
    item: dict[str, Any]
    type: str = "response.output_item.done"


@dataclass
class ResponseCompletedEvent:
    """
    Emitted when the full response is complete.

    :param response: The assembled ``Response`` object.
    :param type: Always ``"response.completed"``.
    """

    response: Response
    type: str = "response.completed"


# Union type for all streaming events
ResponseStreamEvent = (
    ResponseTextDeltaEvent
    | ResponseReasoningTextDeltaEvent
    | ResponseReasoningSummaryTextDeltaEvent
    | ResponseReasoningStartedEvent
    | NativeToolOutputAddedEvent
    | ResponseCompletedEvent
)
