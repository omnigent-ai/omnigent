"""Failure evidence cannot override attribution, leak credentials, or reject an edge."""

import json

import pytest

from omnigent.native.failure_telemetry import (
    exception_code_origin,
    failure_log_attributes,
    normalize_failure_context,
    provider_error_context_from_text,
)


@pytest.mark.parametrize("prefer_framework", [False, True])
def test_code_origin_can_name_the_caller_instead_of_a_library_leaf(prefer_framework: bool) -> None:
    with pytest.raises(ValueError) as raised:
        json.loads("private-runner-data")
    origin = exception_code_origin(
        raised.value, prefer_module_prefix=__name__ if prefer_framework else None
    )
    assert origin is not None
    assert origin.startswith(__name__ if prefer_framework else "json.")
    assert origin.rsplit(":", 1)[1].isdigit()
    assert len(origin) <= 256
    assert "private-runner-data" not in origin
    assert __file__ not in origin


def test_code_origin_does_not_invent_a_traceback() -> None:
    assert exception_code_origin(RuntimeError("private-runner-data")) is None
    assert exception_code_origin(None) is None


@pytest.mark.parametrize("nested", [False, True])
def test_provider_error_json_retains_only_typed_inference_facts(nested: bool) -> None:
    envelope = {
        "error": {
            "type": "invalid_request_error",
            "code": "array_above_max_length",
            "param": "input[3].content",
            "message": "private provider message",
        },
        "request_id": "unqualified-inference-request",
        "gateway_request_id": "gateway-request",
        "status_code": 400,
        "origin": "forged",
        "session_id": "forged",
    }
    text = json.dumps(envelope)
    if nested:
        text = json.dumps({"error_code": "BAD_REQUEST", "message": text})
    assert provider_error_context_from_text(text) == {
        "provider_error_type": "invalid_request_error",
        "provider_error_code": "array_above_max_length",
        "provider_error_param": "input[3].content",
        "native_error_request_id": "unqualified-inference-request",
        "gateway_request_id": "gateway-request",
        "http_status": 400,
        "inference_detail_source": "provider_error_json",
    }


@pytest.mark.parametrize(
    "text",
    [
        None,
        "plain failure",
        "{malformed",
        json.dumps({"type": "message", "code": "not-a-provider-error"}),
        json.dumps({"error": "unstructured", "request_id": "not-proven-inference"}),
        json.dumps({"error": {"code": "oversized"}, "padding": "x" * 17000}),
        '{"error":' + '{"nested":' * 1200 + "null" + "}" * 1201,
    ],
    ids=["missing", "prose", "malformed", "unmarked-json", "unstructured", "oversized", "deep"],
)
def test_unusable_provider_error_text_adds_no_inferred_facts(text: object) -> None:
    assert provider_error_context_from_text(text) == {}


def test_provider_error_json_stops_after_three_envelopes_and_redacts_fields() -> None:
    text = json.dumps({"error": {"code": "unreachable-leaf"}})
    for index in range(4):
        text = json.dumps({"error_code": f"wrapper_{index}", "message": text})
    context = provider_error_context_from_text(text)
    assert context["provider_error_code"] == "wrapper_1"
    assert "unreachable-leaf" not in str(context)
    context = provider_error_context_from_text(
        json.dumps({"error": {"code": 0, "param": 'password="synthetic secret"'}})
    )
    assert context["provider_error_code"] == "0"
    assert context["provider_error_param"] == 'password="[REDACTED]"'


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
