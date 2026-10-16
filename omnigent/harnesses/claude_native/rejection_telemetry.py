"""Bounded evidence for HTTP rejections of native transcript events."""

from __future__ import annotations

import json
import re
from typing import get_args

import httpx
from pydantic_core import ErrorType

from omnigent.errors import ErrorCode
from omnigent.native.failure_telemetry import FailureContext

_BODY_LIMIT = 16384
# Must stay below the 1024-char clip applied to forwarder_validation_errors in
# omnigent.native.failure_telemetry.normalize_failure_context, or that clip can
# truncate the JSON summary mid-token, producing an unparseable value.
_SUMMARY_LIMIT = 1000
_ERROR_LIMIT = 8
_PATH_LIMIT = 8
_REQUEST_ID = re.compile(
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)
_ERROR_TYPES = frozenset(get_args(ErrorType))
_PATH_FIELDS = frozenset(
    {
        "body",
        "SessionEventInput",
        "list[SessionEventInput]",
        "type",
        "data",
        "model_override",
        "tools",
        "created_by",
        "item_type",
        "item_data",
        "source_id",
        "response_id",
        "failure_context",
        "status",
        "output",
        "message",
        "role",
        "content",
        "text",
        "call_id",
        "name",
        "arguments",
    }
)
_REJECTION_REASONS = frozenset(
    {
        ErrorCode.FORBIDDEN,
        ErrorCode.INVALID_INPUT,
        ErrorCode.NOT_FOUND,
        "event batch exceeds count or size limit",
        "event type is not allowed on runner tunnel",
        "source_id required for tunnel replay",
        "invalid session event",
    }
)


def _validation_summary(detail: list[object]) -> FailureContext:
    samples: list[dict[str, object]] = []
    truncated = len(detail) > _ERROR_LIMIT
    for error in detail[:_ERROR_LIMIT]:
        if not isinstance(error, dict) or not isinstance(error.get("loc"), list):
            truncated = True
            continue
        raw_loc = error["loc"]
        loc: list[str | int] = []
        truncated |= len(raw_loc) > _PATH_LIMIT
        for part in raw_loc[:_PATH_LIMIT]:
            if (isinstance(part, str) and part in _PATH_FIELDS) or (
                type(part) is int and 0 <= part <= 2**31 - 1
            ):
                loc.append(part)
            else:
                loc.append("*")
                truncated = True
        raw_type = error.get("type")
        kind = raw_type if isinstance(raw_type, str) and raw_type in _ERROR_TYPES else "other"
        candidate: list[dict[str, object]] = [*samples, {"loc": loc, "type": kind}]
        if len(json.dumps(candidate, separators=(",", ":"))) > _SUMMARY_LIMIT:
            truncated = True
            break
        samples = candidate
    return {
        "forwarder_validation_error_count": len(detail),
        "forwarder_validation_errors": json.dumps(samples, separators=(",", ":")),
        "forwarder_validation_truncated": truncated,
    }


def forwarder_rejection_context(error: httpx.HTTPError) -> FailureContext:
    """Retain schema metadata and transport identity without echoed input or messages."""
    if not isinstance(error, httpx.HTTPStatusError):
        return {}
    response = error.response
    context: FailureContext = {"http_status": response.status_code}
    request_id = response.headers.get("x-request-id", "")
    if _REQUEST_ID.fullmatch(request_id):
        context["forwarder_http_request_id"] = request_id
    if len(response.content) > _BODY_LIMIT:
        context["forwarder_rejection_diagnostic_state"] = "body_too_large"
        return context
    try:
        body = response.json()
    except (ValueError, RecursionError):
        context["forwarder_rejection_diagnostic_state"] = "invalid_json"
        return context
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail in _REJECTION_REASONS:
        context["forwarder_rejection_reason"] = detail
        context["forwarder_rejection_diagnostic_state"] = "known_reason"
    elif isinstance(detail, list):
        context.update(_validation_summary(detail))
        context["forwarder_rejection_diagnostic_state"] = "validation_errors"
    else:
        context["forwarder_rejection_diagnostic_state"] = "unsupported_detail"
    return context
