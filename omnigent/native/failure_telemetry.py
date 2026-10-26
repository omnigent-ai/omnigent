"""Bounded native failure evidence, independent of session status and display text."""

from __future__ import annotations

import json
import math
import uuid

from omnigent.process_logging import redact_log_text

FailureContext = dict[str, str | int | float | bool]
_PROVIDER_ERROR_TEXT_LIMIT = 16384

_TEXT_FIELDS = frozenset(
    {
        "failure_id",
        "failure_source",
        "forwarder_source_id",
        "forwarder_rejection_reason",
        "detail_source",
        "failure_decision",
        "failure_context_error_type",
        "failure_context_error_phase",
        "suppression_reason",
        "native_error_category",
        "transcript_error_context_state",
        "native_error_message",
        "native_harness",
        "native_session_id",
        "native_agent_id",
        "native_agent_role",
        "native_parent_session_id",
        "native_record_id",
        "native_hook_event",
        "runner_version",
        "native_cli_version",
        "native_model",
        "native_request_id",
        "native_error_request_id",
        "gateway_request_id",
        "provider_request_id",
        "provider_error_type",
        "provider_error_code",
        "provider_error_param",
        "inference_detail_source",
        "diagnostic_capture_state",
        "diagnostic_launch_id",
        "diagnostic_read_error_kind",
        "exception_type",
        "exception_cause_type",
        "exception_context_type",
        "native_launch_os_errno_name",
    }
)
_BOOL_FIELDS = frozenset(
    {
        "native_api_error_message",
        "diagnostic_capture_enabled",
        "diagnostic_marker_present",
        "diagnostic_file_present",
        "diagnostic_truncated",
    }
)
_INT_FIELDS = frozenset(
    {
        "native_hook_cursor",
        "native_hook_offset",
        "http_status",
        "diagnostic_read_offset",
        "diagnostic_lines_omitted",
        "diagnostic_bytes_omitted",
        "native_launch_os_errno",
        "native_launch_winerror",
    }
)
_TIME_FIELDS = frozenset({"native_hook_recorded_at", "diagnostic_last_read_at"})
_AVAILABILITY_FIELDS = (
    "native_error_category",
    "native_api_error_message",
    "native_session_id",
    "native_agent_id",
    "native_cli_version",
    "native_model",
    "http_status",
    "provider_error_type",
    "provider_error_code",
    "provider_error_param",
    "native_request_id",
    "native_error_request_id",
    "gateway_request_id",
    "provider_request_id",
)


def normalize_failure_context(value: object) -> FailureContext:
    """Ignore malformed/unknown metadata; never let it override canonical log fields."""
    if not isinstance(value, dict):
        return {}
    result: FailureContext = {}
    for key in _TEXT_FIELDS:
        raw = value.get(key)
        if key == "provider_error_code" and type(raw) is int and abs(raw) <= 2**63 - 1:
            raw = str(raw)
        if isinstance(raw, str) and raw.strip():
            limit = 1024 if key == "native_error_message" else 256
            # Clipping first can turn a quoted credential into an unrecognized fragment.
            redacted = redact_log_text(raw, include_whitespace_credentials=True).strip()[:limit]
            result[key] = redacted.encode("utf-8", errors="replace").decode("utf-8")
    for key in _BOOL_FIELDS:
        raw = value.get(key)
        if isinstance(raw, bool):
            result[key] = raw
    for key in _INT_FIELDS:
        raw = value.get(key)
        if type(raw) is int and 0 <= raw <= 2**63 - 1:
            if key != "http_status" or 100 <= raw <= 599:
                result[key] = raw
    for key in _TIME_FIELDS:
        raw = value.get(key)
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and 0 <= raw <= 1e12
            and math.isfinite(raw)
        ):
            result[key] = raw
    return result


def failure_log_attributes(value: object) -> dict[str, str]:
    """Flatten evidence for the log sink and make unavailable source fields explicit."""
    result = normalize_failure_context(value)
    if result:
        result["failure_context_version"] = 1
        result["failure_context_missing_fields"] = ",".join(
            field for field in _AVAILABILITY_FIELDS if field not in result
        )
    return {key: str(field_value) for key, field_value in result.items()}


def provider_error_fields(error: object) -> FailureContext:
    """Read typed provider fields without retaining its message or unrelated keys."""
    if not isinstance(error, dict):
        return {}
    return normalize_failure_context(
        {
            "provider_error_type": error.get("type"),
            "provider_error_code": error.get("code")
            if error.get("code") is not None
            else error.get("error_code"),
            "provider_error_param": error.get("param"),
        }
    )


def provider_error_context_from_text(text: object) -> FailureContext:
    """Read at most three explicit provider/gateway error envelopes from bounded JSON."""
    if not isinstance(text, str) or len(text) > _PROVIDER_ERROR_TEXT_LIMIT:
        return {}
    pending = text.strip()
    context: FailureContext = {}
    for _ in range(3):
        if not pending.startswith("{"):
            break
        try:
            envelope = json.loads(pending)
        except (ValueError, RecursionError):
            break
        if not isinstance(envelope, dict):
            break
        error = envelope.get("error")
        if not isinstance(error, dict):
            if "error_code" not in envelope:
                break
            error = envelope
        context.update(provider_error_fields(error))
        context.update(
            normalize_failure_context(
                {
                    "http_status": envelope.get("status_code", envelope.get("status")),
                    "native_error_request_id": envelope.get("request_id"),
                    "gateway_request_id": envelope.get("gateway_request_id"),
                    "provider_request_id": envelope.get("provider_request_id"),
                }
            )
        )
        nested = error.get("message")
        if not isinstance(nested, str) or len(nested) > _PROVIDER_ERROR_TEXT_LIMIT:
            break
        pending = nested.strip()
    if context:
        context["inference_detail_source"] = "provider_error_json"
    return context


def native_failure_id(source: str, *identity: str | int | float | None) -> str:
    """Derive an observation ID from durable source identity, never a retry timestamp."""
    key = json.dumps([source, *identity], separators=(",", ":"), ensure_ascii=True)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "omnigent:native-failure:" + key))
