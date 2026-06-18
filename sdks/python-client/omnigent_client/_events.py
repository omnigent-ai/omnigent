"""Typed event dataclasses for SSE stream events.

The client parses raw SSE frames into these types. Consumers
iterate over them via ``async for event in stream``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._types import ErrorInfo, Response

# ── Native tool type constants ───────────────────────────

NATIVE_TOOL_TYPES: frozenset[str] = frozenset(
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

# JSON-RPC method name MCP uses for elicitation requests. The
# server's ``response.elicitation_request`` SSE event carries this
# verbatim under ``method`` so MCP-aware consumers can route on
# the same name they already recognize.
MCP_ELICITATION_METHOD = "elicitation/create"


# ── Response lifecycle events ────────────────────────────


@dataclass
class ResponseCreated:
    """``response.created`` — always first (sequence 0)."""

    response: Response


@dataclass
class ResponseQueued:
    """``response.queued`` — only when ``background=True``."""

    response: Response


@dataclass
class ResponseInProgress:
    """``response.in_progress`` — execution started."""

    response: Response


@dataclass
class ResponseCompleted:
    """``response.completed`` — agent finished successfully."""

    response: Response


@dataclass
class ResponseFailed:
    """``response.failed`` — unrecoverable error."""

    response: Response


@dataclass
class ResponseIncomplete:
    """``response.incomplete`` — stopped early."""

    response: Response
    reason: str  # "max_iterations", "execution_timeout", etc.


@dataclass
class ResponseCancelled:
    """``response.cancelled`` — cancelled via POST /cancel."""

    response: Response


# ── Text streaming ───────────────────────────────────────


@dataclass
class TextDelta:
    """``response.output_text.delta`` — incremental text token."""

    delta: str


# ── Reasoning ────────────────────────────────────────────


@dataclass
class ReasoningStarted:
    """``response.reasoning.started`` — reasoning block opened."""


@dataclass
class ReasoningDelta:
    """``response.reasoning_text.delta`` — reasoning token."""

    delta: str


@dataclass
class ReasoningSummaryDelta:
    """``response.reasoning_summary_text.delta`` — summary token."""

    delta: str


@dataclass
class ReasoningSummaryPartDone:
    """``response.reasoning_summary_part.done`` — a summary paragraph ended.

    The Responses API delimits successive reasoning-summary paragraphs
    with this boundary; the ``ReasoningSummaryDelta`` fragments within a
    part carry no trailing newline. The reducer uses it to flush the
    held tail and insert a paragraph separator (issue #654).
    """


# ── Parsed output items ─────────────────────────────────


@dataclass
class ToolCall:
    """A tool call from ``output_item.done`` (type ``function_call``)."""

    name: str
    arguments: dict[str, object]
    call_id: str
    status: str  # "completed", "action_required", "incomplete"
    agent_name: str  # "coder" or "coder.researcher"


@dataclass
class ToolResult:
    """A tool result from ``output_item.done`` (type ``function_call_output``).

    ``arguments`` is optional because the OpenAI-compatible
    ``function_call_output`` item only requires ``call_id`` and ``output``.
    Omnigent producers may include it as a convenience copy of the
    originating ``function_call.arguments`` so result-only renderers can use
    call metadata (for example, rendering ``sys_os_edit`` as a diff).
    """

    call_id: str
    output: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass
class ElicitationRequest:
    """
    A server-initiated elicitation, MCP shape.

    Parsed out of the ``response.elicitation_request`` SSE event;
    the event's ``params`` block matches MCP's
    ``ElicitRequestFormParams`` field-for-field, plus extras
    (``phase``, ``policy_name``, ``content_preview``) under MCP's
    ``extra="allow"`` config that today's only producer (the
    policy ASK flow) populates for renderer use.

    Consumers respond via
    ``POST /v1/sessions/{session_id}/events`` with
    ``type == "approval"`` and MCP-shape ``ElicitResult`` fields
    (``action`` + optional ``content``) in ``data``. The client
    handles the POST automatically when an
    ``on_elicitation_request`` hook is registered on
    :class:`StreamHooks`.

    :param elicitation_id: Server-assigned id. Used in the
        approval event payload, e.g. ``"elicit_abc123"``.
    :param message: Human-readable prompt the consumer should
        render to the user. For the policy ASK producer this is
        the combined reason string from deciding ASKing policies.
    :param requested_schema: A restricted subset of JSON Schema
        defining the structure of the expected response. Empty
        ``{}`` for binary approve/reject elicitations (the verdict
        is in the consumer's ``action``).
    :param mode: MCP elicitation mode — ``"form"`` (inline) or
        ``"url"`` (standalone approval page). e.g. ``"url"``.
    :param phase: Producer-supplied extra (policy ASK only):
        which enforcement point produced the ASK — one of
        ``"input"``, ``"tool_call"``, ``"tool_result"``,
        ``"output"``. Empty string when the elicitation came from
        a producer that doesn't populate this extra.
    :param policy_name: Producer-supplied extra (policy ASK
        only): name of the deciding (first-in-YAML-order) ASKing
        policy, e.g. ``"approve_web_search"``. Empty string when
        not applicable.
    :param content_preview: Producer-supplied extra (policy ASK
        only): truncated snapshot of the gated content. Safe to
        display verbatim. Empty string when not applicable.
    :param target_session_id: Session whose resolve endpoint owns
        this elicitation, e.g. ``"conv_child123"``. Set when a
        sub-agent's prompt was mirrored into an ancestor stream so the
        consumer can route the verdict back to the child that parked on
        it. ``None`` means resolve against the session the event arrived
        on.
    """

    elicitation_id: str
    message: str
    requested_schema: dict[str, object]
    mode: str
    phase: str
    policy_name: str
    content_preview: str
    url: str | None = None
    target_session_id: str | None = None


@dataclass
class NativeToolCall:
    """A provider-native tool output (web_search, mcp, etc.)."""

    tool_type: str  # e.g. "web_search_call"
    data: dict[str, object]


@dataclass
class MessageDone:
    """The final assistant message from ``output_item.done`` (type ``message``)."""

    content: list[dict[str, object]] = field(default_factory=list)


# ── File output ──────────────────────────────────────────


@dataclass
class OutputFileDone:
    """``response.output_file.done`` — file artifact produced."""

    file_id: str
    filename: str | None = None
    content_type: str | None = None


# ── Error and retry ──────────────────────────────────────


@dataclass
class RetryEvent:
    """``response.retry`` — a retryable failure, will retry."""

    source: str  # "llm" or "tool"
    tool_name: str | None
    attempt: int
    max_attempts: int
    delay_seconds: float
    error: ErrorInfo


@dataclass
class ErrorEvent:
    """``response.error`` — an error during execution."""

    source: str  # "llm" or "tool"
    tool_name: str | None
    error: ErrorInfo


# ── Compaction ───────────────────────────────────────────


@dataclass
class CompactionInProgress:
    """``response.compaction.in_progress`` — server started compacting."""


@dataclass
class CompactionCompleted:
    """``response.compaction.completed`` — compaction finished successfully."""


@dataclass
class CompactionFailed:
    """``response.compaction.failed`` — compaction failed; history unchanged."""


# ── Async client-tool cancel (Phase 5) ───────────────────


@dataclass
class ClientTaskCancel:
    """
    ``response.client_task.cancel`` — server-to-client cancel
    notification for async client-tool dispatches.

    Emitted when an async client tool task
    (``kind="client_tool"``) was cancelled mid-flight, either
    via direct ``sys_cancel_task`` or via parent-cancel
    propagation. The SDK should cancel the matching local
    ``asyncio.Task`` running the tool body. The body's
    eventual PATCH back is a no-op (G3 first-write-wins), so
    the cancel is best-effort from the SDK's perspective —
    the server has already decided the task is cancelled.

    :param task_id: The server-issued client-tool task id from
        the original ``function_call_output`` handle, e.g.
        ``"resp_async_xyz"``.
    :param call_id: The synthesized ``function_call.call_id``
        the SDK saw on the action_required event for this
        dispatch, e.g. ``"call_async_b2c4..."``. Populated by
        the server from ``pending_tool_calls`` so the SDK can
        look up the local ``asyncio.Task`` it spawned. ``None``
        on legacy emissions where the lookup wasn't done; the
        SDK falls back to no-op when both fields can't be
        matched to a tracked task.
    """

    task_id: str
    call_id: str | None = None


# ── Union type for all events ────────────────────────────

StreamEvent = (
    ResponseCreated
    | ResponseQueued
    | ResponseInProgress
    | ResponseCompleted
    | ResponseFailed
    | ResponseIncomplete
    | ResponseCancelled
    | TextDelta
    | ReasoningStarted
    | ReasoningDelta
    | ReasoningSummaryDelta
    | ReasoningSummaryPartDone
    | ToolCall
    | ToolResult
    | NativeToolCall
    | MessageDone
    | OutputFileDone
    | RetryEvent
    | ErrorEvent
    | CompactionInProgress
    | CompactionCompleted
    | CompactionFailed
    | ClientTaskCancel
    | ElicitationRequest
)
