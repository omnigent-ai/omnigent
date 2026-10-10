"""Failure evidence cannot override attribution, leak credentials, or reject an edge."""

import pytest

from omnigent.native.failure_telemetry import failure_log_attributes, normalize_failure_context


@pytest.mark.parametrize("value", [None, [], "bad", 17, {"http_status": True}])
def test_malformed_metadata_is_ignored(value: object) -> None:
    assert normalize_failure_context(value) == {}


def test_context_is_bounded_redacted_and_cannot_override_canonical_fields() -> None:
    context = failure_log_attributes(
        {
            "native_error_category": "server_error",
            "native_error_message": "token=synthetic-secret " + "x" * 9000,
            "provider_error_param": "p" * 2000,
            "native_hook_cursor": -1,
            "native_hook_recorded_at": float("nan"),
            "diagnostic_last_read_at": float("inf"),
            "origin": "forged",
            "code": "forged",
            "session_id": "forged",
            "response_id": "forged",
            "request_id": "event-post-request",
            "turn_id": "forged",
            "user_id": "forged",
            "event_name": "forged",
            "failure_context_missing_fields": "forged",
            "nested": {"token": "synthetic-secret"},
        }
    )
    assert context["native_error_category"] == "server_error"
    assert "synthetic-secret" not in str(context)
    assert "[REDACTED]" in context["native_error_message"]
    assert len(context["native_error_message"]) <= 1024
    assert len(context["provider_error_param"]) == 256
    assert not (
        {
            "origin",
            "code",
            "session_id",
            "response_id",
            "turn_id",
            "user_id",
            "event_name",
            "request_id",
            "nested",
        }
        & context.keys()
    )
    assert "native_hook_cursor" not in context
    assert "native_hook_recorded_at" not in context
    assert "diagnostic_last_read_at" not in context
    missing = context["failure_context_missing_fields"].split(",")
    assert "native_error_category" not in missing
    assert "provider_request_id" in missing
    assert "native_api_error_message" in missing
    assert "launch_id" not in missing


def test_explicit_false_marker_is_distinct_from_unavailable() -> None:
    attrs = failure_log_attributes({"native_api_error_message": False})
    assert attrs["native_api_error_message"] == "False"
    assert "native_api_error_message" not in attrs["failure_context_missing_fields"].split(",")


@pytest.mark.parametrize("field", ["native_error_message", "provider_error_param"])
def test_quoted_credentials_are_redacted_before_any_clipping(field: str) -> None:
    value = 'password="first ' + "synthetic-credential-fragment " * 200 + '"'
    context = normalize_failure_context({field: value})
    assert context[field] == 'password="[REDACTED]"'
