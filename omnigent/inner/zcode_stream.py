"""Parse ``zcode -p --output-format stream-json`` stdout."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

JsonObject = dict[str, Any]  # type: ignore[explicit-any]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ToolUpdate:
    phase: str
    name: str
    call_id: str | None
    args: JsonObject = field(default_factory=dict)
    result: Any = None  # type: ignore[explicit-any]
    error: str | None = None


@dataclass(frozen=True)
class PermissionNotice:
    tool_name: str
    request_id: str | None
    tool_call_id: str | None


@dataclass(frozen=True)
class StreamError:
    message: str


@dataclass(frozen=True)
class ResultSummary:
    response: str
    session_id: str | None
    usage: JsonObject


StreamEvent = (
    TextDelta | ReasoningDelta | ToolUpdate | PermissionNotice | StreamError | ResultSummary
)


def _object(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _error_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("message", "detail", "underlyingErrorMessage", "error"):
            nested = value.get(key)
            if nested is not None:
                return _error_text(nested)
    return str(value or "ZCode failed")


def _usage(raw: object) -> JsonObject:
    source = _object(raw)
    mapped: JsonObject = {}
    for original, normalized in (
        ("inputTokens", "input_tokens"),
        ("outputTokens", "output_tokens"),
        ("totalTokens", "total_tokens"),
        ("cacheReadTokens", "cache_read_input_tokens"),
        ("cacheWriteTokens", "cache_creation_input_tokens"),
        ("reasoningTokens", "reasoning_tokens"),
    ):
        value = source.get(original)
        if isinstance(value, int) and not isinstance(value, bool):
            mapped[normalized] = value
    return mapped


def _tool_update(payload: JsonObject) -> ToolUpdate | None:
    phase = payload.get("kind")
    call_id = payload.get("toolCallId")
    call = call_id if isinstance(call_id, str) and call_id else None
    name = payload.get("toolName")
    tool_name = name if isinstance(name, str) and name else "zcode_tool"
    raw_args = payload.get("input", payload.get("args"))
    args = raw_args if isinstance(raw_args, dict) else {}
    if phase in {"scheduled", "started"}:
        return ToolUpdate(phase=phase, name=tool_name, call_id=call, args=args)
    if phase == "result":
        result = payload.get("result", payload.get("output"))
        nested = _object(result)
        if nested.get("success") is False or nested.get("error") is not None:
            error = nested.get("error", nested.get("content"))
            return ToolUpdate(
                phase="error",
                name=tool_name,
                call_id=call,
                error=_error_text(error),
            )
        content = nested.get("content", result) if nested else result
        return ToolUpdate(phase="result", name=tool_name, call_id=call, args=args, result=content)
    if phase == "error":
        return ToolUpdate(
            phase="error",
            name=tool_name,
            call_id=call,
            args=args,
            error=_error_text(payload.get("error", payload.get("message"))),
        )
    return None


def parse_stream_line(line: str) -> StreamEvent | None:
    """Parse one top-level print-stream event, ignoring non-JSON noise."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    payload = _object(event.get("payload"))
    if kind == "result":
        projection = _object(event.get("projection"))
        usage = _usage(event.get("usage"))
        context_used = projection.get("contextUsed")
        if isinstance(context_used, int) and not isinstance(context_used, bool):
            usage["context_tokens"] = context_used
        response = event.get("response")
        session_id = event.get("sessionId")
        return ResultSummary(
            response=response if isinstance(response, str) else "",
            session_id=session_id if isinstance(session_id, str) and session_id else None,
            usage=usage,
        )
    if kind == "model.streaming":
        delta = payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return None
        if payload.get("kind") == "text_delta":
            return TextDelta(delta)
        if payload.get("kind") == "reasoning_delta":
            return ReasoningDelta(delta)
    if kind == "tool.updated":
        return _tool_update(payload)
    if kind == "permission.requested":
        tool_name = payload.get("toolName")
        request_id = payload.get("requestId")
        tool_call_id = payload.get("toolCallId")
        return PermissionNotice(
            tool_name=tool_name if isinstance(tool_name, str) else "zcode_tool",
            request_id=request_id if isinstance(request_id, str) else None,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
        )
    if kind in {"error", "turn.failed"}:
        return StreamError(_error_text(payload.get("error", payload.get("message"))))
    return None
