"""SSE frame parser — converts raw byte chunks into typed events.

Handles the ``event:`` / ``data:`` / ``[DONE]`` framing from the
server's ``text/event-stream`` responses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from omnigent.server import schemas as _srv_events

from ._events import (
    NATIVE_TOOL_TYPES,
    ClientTaskCancel,
    CompactionInProgress,
    ElicitationRequest,
    ErrorEvent,
    MessageDone,
    NativeToolCall,
    OutputFileDone,
    ReasoningDelta,
    ReasoningStarted,
    ReasoningSummaryDelta,
    ReasoningSummaryPartDone,
    ResponseCancelled,
    ResponseCompleted,
    ResponseCreated,
    ResponseFailed,
    ResponseIncomplete,
    ResponseInProgress,
    ResponseQueued,
    RetryEvent,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolResult,
)
from ._types import ErrorInfo, Response

_log = logging.getLogger("omnigent_client.sse")


def _wire_type(cls: type) -> str:
    """
    Extract the wire ``type`` literal from a server event class.

    Each :mod:`omnigent.server.schemas` event class pins its
    ``type`` field as ``Literal["..."]``; this helper unwraps that
    to a plain string so the SDK's ``str == str`` dispatch table
    stays a ``str`` comparison rather than introducing a class-side
    isinstance check.

    :param cls: A subclass of the server's ``_SSEEventBase``,
        e.g. :class:`omnigent.server.schemas.OutputTextDeltaEvent`.
    :returns: The wire ``type`` literal, e.g.
        ``"response.output_text.delta"``.
    """
    return cls.model_fields["type"].annotation.__args__[0]  # type: ignore[union-attr]


# Wire type literals — pulled from the server's typed source of
# truth so a rename there is a one-edit change here as well.
_T_RESPONSE_CREATED = _wire_type(_srv_events.CreatedEvent)
_T_RESPONSE_QUEUED = _wire_type(_srv_events.QueuedEvent)
_T_RESPONSE_IN_PROGRESS = _wire_type(_srv_events.InProgressEvent)
_T_RESPONSE_COMPLETED = _wire_type(_srv_events.CompletedEvent)
_T_RESPONSE_FAILED = _wire_type(_srv_events.FailedEvent)
_T_RESPONSE_INCOMPLETE = _wire_type(_srv_events.IncompleteEvent)
_T_RESPONSE_CANCELLED = _wire_type(_srv_events.CancelledEvent)
_T_RESPONSE_OUTPUT_TEXT_DELTA = _wire_type(_srv_events.OutputTextDeltaEvent)
_T_RESPONSE_REASONING_STARTED = _wire_type(_srv_events.ReasoningStartedEvent)
_T_RESPONSE_REASONING_TEXT_DELTA = _wire_type(_srv_events.ReasoningTextDeltaEvent)
_T_RESPONSE_REASONING_SUMMARY_TEXT_DELTA = _wire_type(_srv_events.ReasoningSummaryTextDeltaEvent)
_T_RESPONSE_REASONING_SUMMARY_PART_DONE = _wire_type(_srv_events.ReasoningSummaryPartDoneEvent)
_T_RESPONSE_OUTPUT_ITEM_DONE = _wire_type(_srv_events.OutputItemDoneEvent)
_T_RESPONSE_OUTPUT_FILE_DONE = _wire_type(_srv_events.OutputFileDoneEvent)
_T_RESPONSE_RETRY = _wire_type(_srv_events.RetryEvent)
_T_RESPONSE_ERROR = _wire_type(_srv_events.ErrorEvent)
_T_RESPONSE_COMPACTION_IN_PROGRESS = _wire_type(_srv_events.CompactionInProgressEvent)
_T_RESPONSE_CLIENT_TASK_CANCEL = _wire_type(_srv_events.ClientTaskCancelEvent)
_T_RESPONSE_ELICITATION_REQUEST = _wire_type(_srv_events.ElicitationRequestEvent)


async def parse_sse_stream(
    byte_stream: AsyncIterator[bytes],
) -> AsyncIterator[StreamEvent]:
    """Parse an SSE byte stream into typed events.

    :param byte_stream: Raw bytes from ``httpx.Response.aiter_bytes()``.
    :yields: Typed :class:`StreamEvent` instances.
    """
    buf = ""
    current_event: str | None = None

    async for chunk in byte_stream:
        buf += chunk.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip("\r")

            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    # The server's terminal sentinel is a bare ``data: [DONE]``
                    # with no preceding ``event:`` line, so detect it regardless
                    # of ``current_event``.
                    return
                # A ``data:`` line is only a parseable event when an ``event:``
                # line preceded it; a lone data line (other than [DONE]) is
                # ignored.
                if current_event is not None:
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        _log.warning("Failed to parse SSE data: %s", data_str[:200])
                        current_event = None
                        continue
                    event = _parse_event(current_event, data)
                    if event is not None:
                        yield event
                    current_event = None
            elif line == "":
                current_event = None


def _normalize_event_type(event_type: str) -> str:
    """Normalize event type to handle server enum rendering.

    The server builds terminal events as ``f"response.{task.status}"``
    where ``task.status`` may be a Python enum (rendering as
    ``response.TaskStatus.COMPLETED``) instead of the expected
    ``response.completed``. Normalize by extracting and lowercasing
    the enum value.
    """
    if ".TaskStatus." in event_type:
        # "response.TaskStatus.COMPLETED" → "response.completed"
        parts = event_type.split(".")
        status = parts[-1].lower()
        return f"response.{status}"
    return event_type


def _parse_event(event_type: str, data: dict[str, Any]) -> StreamEvent | None:
    """Convert a raw SSE event type + JSON data into a typed event.

    :param event_type: Wire name of the SSE ``event:`` field, e.g.
        ``"response.output_text.delta"``. Compared against the
        ``_T_RESPONSE_*`` wire-type constants (sourced from
        :mod:`omnigent.server.schemas`) to dispatch.
    :param data: Decoded JSON payload from the SSE ``data:`` field.
    :returns: A typed :class:`StreamEvent` for known event names, or
        ``None`` when the payload is missing required fields or the
        event type is unrecognized (forward-compatible skip).
    """
    event_type = _normalize_event_type(event_type)

    # Response lifecycle
    if event_type == _T_RESPONSE_CREATED:
        return ResponseCreated(response=_parse_response(data))
    if event_type == _T_RESPONSE_QUEUED:
        return ResponseQueued(response=_parse_response(data))
    if event_type == _T_RESPONSE_IN_PROGRESS:
        return ResponseInProgress(response=_parse_response(data))
    if event_type == _T_RESPONSE_COMPLETED:
        return ResponseCompleted(response=_parse_response(data))
    if event_type == _T_RESPONSE_FAILED:
        return ResponseFailed(response=_parse_response(data))
    if event_type == _T_RESPONSE_INCOMPLETE:
        resp = _parse_response(data)
        reason = ""
        if resp.incomplete_details is not None:
            reason = resp.incomplete_details.reason
        return ResponseIncomplete(response=resp, reason=reason)
    if event_type == _T_RESPONSE_CANCELLED:
        return ResponseCancelled(response=_parse_response(data))

    # Text streaming
    if event_type == _T_RESPONSE_OUTPUT_TEXT_DELTA:
        delta = data.get("delta")
        if isinstance(delta, str):
            return TextDelta(delta=delta)
        return None

    # Reasoning
    if event_type == _T_RESPONSE_REASONING_STARTED:
        return ReasoningStarted()
    if event_type == _T_RESPONSE_REASONING_TEXT_DELTA:
        delta = data.get("delta")
        if isinstance(delta, str):
            return ReasoningDelta(delta=delta)
        return None
    if event_type == _T_RESPONSE_REASONING_SUMMARY_TEXT_DELTA:
        delta = data.get("delta")
        if isinstance(delta, str):
            return ReasoningSummaryDelta(delta=delta)
        return None
    if event_type == _T_RESPONSE_REASONING_SUMMARY_PART_DONE:
        return ReasoningSummaryPartDone()

    # Output items
    if event_type == _T_RESPONSE_OUTPUT_ITEM_DONE:
        return _parse_output_item(data)

    # File output
    if event_type == _T_RESPONSE_OUTPUT_FILE_DONE:
        return OutputFileDone(
            file_id=str(data.get("file_id", "")),
            filename=str(data["filename"]) if data.get("filename") is not None else None,
            content_type=str(data["content_type"])
            if data.get("content_type") is not None
            else None,
        )

    # Retry
    if event_type == _T_RESPONSE_RETRY:
        return RetryEvent(
            source=str(data.get("source", "")),
            tool_name=str(data["tool_name"]) if data.get("tool_name") is not None else None,
            attempt=int(data.get("attempt", 0)),
            max_attempts=int(data.get("max_attempts", 0)),
            delay_seconds=float(data.get("delay_seconds", 0.0)),
            error=_parse_error_info(data.get("error", {})),
        )

    # Error
    if event_type == _T_RESPONSE_ERROR:
        return ErrorEvent(
            source=str(data.get("source", "")),
            tool_name=str(data["tool_name"]) if data.get("tool_name") is not None else None,
            error=_parse_error_info(data.get("error", {})),
        )

    # Compaction
    if event_type == _T_RESPONSE_COMPACTION_IN_PROGRESS:
        return CompactionInProgress()

    # Async client-tool cancel notification
    if event_type == _T_RESPONSE_CLIENT_TASK_CANCEL:
        task_id = data.get("task_id")
        if isinstance(task_id, str) and task_id:
            raw_call_id = data.get("call_id")
            call_id = raw_call_id if isinstance(raw_call_id, str) and raw_call_id else None
            return ClientTaskCancel(task_id=task_id, call_id=call_id)
        _log.warning("response.client_task.cancel missing task_id: %r", data)
        return None

    # MCP-shape elicitation request (POLICIES.md §7 + the
    # universal API additions in
    # designs/SERVER_HARNESS_CONTRACT.md). The ``params`` block
    # mirrors MCP's ``ElicitRequestFormParams`` field-for-field;
    # we surface it as a typed event so the stream consumer can
    # route it to the elicitation hook (not into a ToolHandler).
    if event_type == "response.elicitation_request":
        elicitation_id = data.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            _log.warning(
                "response.elicitation_request missing elicitation_id: %r",
                data,
            )
            return None
        params = data.get("params")
        if not isinstance(params, dict):
            _log.warning(
                "response.elicitation_request missing params dict: %r",
                data,
            )
            return None
        requested_schema = params.get("requestedSchema")
        return ElicitationRequest(
            elicitation_id=elicitation_id,
            message=str(params.get("message") or ""),
            # MCP spec restricts requestedSchema to a JSON-Schema
            # dict — we accept anything dict-shaped and pass it
            # through; non-dict inputs (defensive against
            # malformed producers) become an empty schema.
            requested_schema=requested_schema if isinstance(requested_schema, dict) else {},
            mode=str(params.get("mode") or "form"),
            phase=str(params.get("phase") or ""),
            policy_name=str(params.get("policy_name") or ""),
            content_preview=str(params.get("content_preview") or ""),
            url=str(params["url"]) if isinstance(params.get("url"), str) else None,
        )

    # Unknown event — skip gracefully for forward-compatibility
    _log.debug("Skipping unknown SSE event type: %s", event_type)
    return None


def _parse_output_item(data: dict[str, Any]) -> StreamEvent | None:
    """Parse a ``response.output_item.done`` event into a typed event."""
    item = data.get("item")
    if not isinstance(item, dict):
        return None

    item_type = item.get("type", "")

    if item_type == "function_call":
        args_str = str(item.get("arguments", "{}"))
        try:
            arguments = json.loads(args_str)
        except json.JSONDecodeError:
            arguments = {}
        name = str(item.get("name", ""))
        call_id = str(item.get("call_id", ""))
        return ToolCall(
            name=name,
            arguments=arguments,
            call_id=call_id,
            status=str(item.get("status", "")),
            agent_name=str(item.get("model", "")),
        )

    if item_type == "function_call_output":
        raw_arguments = item.get("arguments")
        arguments: dict[str, object] = {}
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif isinstance(raw_arguments, str) and raw_arguments:
            try:
                decoded_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                decoded_arguments = {}
            if isinstance(decoded_arguments, dict):
                arguments = decoded_arguments
        return ToolResult(
            call_id=str(item.get("call_id", "")),
            output=str(item.get("output", "")),
            arguments=arguments,
        )

    if item_type == "message":
        content = item.get("content", [])
        return MessageDone(
            content=content if isinstance(content, list) else [],
        )

    if item_type in NATIVE_TOOL_TYPES:
        return NativeToolCall(
            tool_type=item_type,
            data=item,
        )

    # Compaction items, reasoning items, etc. — skip
    _log.debug("Skipping output item type: %s", item_type)
    return None


def _parse_response(data: dict[str, Any]) -> Response:
    """Extract the Response object from an SSE event payload."""
    resp_data = data.get("response")
    if isinstance(resp_data, dict):
        return Response.from_dict(resp_data)
    # Some events put fields at the top level
    return Response.from_dict(data)


def _parse_error_info(raw: Any) -> ErrorInfo:
    """Parse an ErrorInfo from a nested dict."""
    if isinstance(raw, dict):
        return ErrorInfo(
            code=str(raw.get("code", "")),
            message=str(raw.get("message", "")),
        )
    return ErrorInfo(code="", message=str(raw))
