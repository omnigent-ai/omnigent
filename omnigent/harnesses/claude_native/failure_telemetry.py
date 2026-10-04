"""Extract error metadata only from Claude's explicit failure records."""

from __future__ import annotations

import json
import re

from omnigent.models.model_metadata import concrete_reported_model
from omnigent.native.failure_telemetry import FailureContext, normalize_failure_context

_API_ERROR = re.compile(r"^\s*API Error:\s*([1-5][0-9]{2})\b")
_API_ERROR_BODY_MAX_CHARS = 16384


def claude_failure_context(
    entry: dict[str, object], *, error_text: str | None = None
) -> FailureContext:
    """Keep native IDs distinct from request IDs belonging to the event transport."""
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    context: dict[str, object] = {
        "native_harness": "claude-native",
        "native_session_id": entry.get("sessionId", entry.get("session_id")),
        "native_agent_id": entry.get("agentId", entry.get("agent_id")),
        "native_record_id": entry.get("uuid"),
        "native_request_id": entry.get("requestId"),
        "native_cli_version": entry.get("version"),
        "native_model": concrete_reported_model(message.get("model")),
        "native_error_category": entry.get("error"),
    }
    native_agent_id = context["native_agent_id"]
    if (isinstance(native_agent_id, str) and native_agent_id) or entry.get("isSidechain") is True:
        context["native_agent_role"] = "subagent"
    elif entry.get("isSidechain") is False:
        context["native_agent_role"] = "session_agent"
    else:
        context["native_agent_role"] = "unknown"
    markers = [container.get("isApiErrorMessage") for container in (message, entry)]
    if any(isinstance(marker, bool) for marker in markers):
        context["native_api_error_message"] = any(marker is True for marker in markers)
    if context.get("native_api_error_message") is True and error_text:
        context["native_error_message"] = error_text

    error = entry.get("error")
    if error is None:
        error = message.get("error")
    envelope = entry
    if isinstance(error, dict):
        context["inference_detail_source"] = "structured_error"
    elif (
        error_text is not None
        and (match := _API_ERROR.match(error_text[:_API_ERROR_BODY_MAX_CHARS])) is not None
    ):
        context["http_status"] = int(match[1])
        context["inference_detail_source"] = "api_error_text"
        context["native_error_message"] = error_text
        if len(error_text) <= _API_ERROR_BODY_MAX_CHARS:
            body = error_text[match.end() :].strip()
            try:
                decoded = json.loads(body) if body.startswith("{") else None
            except (ValueError, RecursionError):
                decoded = None
            if isinstance(decoded, dict):
                envelope = decoded
                error = decoded.get("error", decoded)
    if isinstance(error, dict):
        context.update(
            provider_error_type=error.get("type"),
            provider_error_code=(
                error["code"] if error.get("code") is not None else error.get("error_code")
            ),
            provider_error_param=error.get("param"),
        )
        error_message = error.get("message")
        if isinstance(error_message, str) and error_message.strip():
            context["native_error_message"] = error_message
    # An unqualified request_id in an error body has no proven provider ownership.
    if envelope is not entry:
        context["native_error_request_id"] = envelope.get("request_id")
    context["gateway_request_id"] = envelope.get(
        "gateway_request_id", entry.get("gateway_request_id")
    )
    context["provider_request_id"] = envelope.get(
        "provider_request_id", entry.get("provider_request_id")
    )
    if "http_status" not in context:
        context["http_status"] = envelope.get("status_code", envelope.get("status"))
    return normalize_failure_context(context)
