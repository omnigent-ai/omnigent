"""Codex failure evidence retains typed causes without copying free-form details."""

import json

import pytest

from omnigent.harnesses.codex_native.failure_telemetry import (
    codex_error_context,
    codex_error_reason,
)


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ("unauthorized", {"native_error_category": "unauthorized"}),
        (
            {"type": "httpConnectionFailed", "httpStatusCode": 503},
            {"native_error_category": "httpConnectionFailed", "http_status": 503},
        ),
        (
            {"responseStreamDisconnected": {"httpStatusCode": 502}},
            {"native_error_category": "responseStreamDisconnected", "http_status": 502},
        ),
        ({"httpStatusCode": 401}, {"http_status": 401}),
        ({"kind": "other", "httpStatusCode": True}, {"native_error_category": "other"}),
        ({"type": ["malformed"], "httpStatusCode": "503"}, {}),
        (
            {"responseStreamDisconnected": {"httpStatusCode": None}},
            {"native_error_category": "responseStreamDisconnected"},
        ),
        (None, {}),
    ],
)
def test_error_info_shapes_preserve_only_typed_fields(info: object, expected: dict) -> None:
    context = codex_error_context(
        {
            "codexErrorInfo": info,
            "message": "private provider message",
            "additionalDetails": "private provider command output",
            "request_id": "unproven request",
            "headers": {"Authorization": "secret"},
        },
        thread_id="native-thread",
    )
    assert context == {**expected, "native_session_id": "native-thread"}


def test_structured_provider_details_are_redacted_and_bounded() -> None:
    context = codex_error_context(
        {
            "codexErrorInfo": "x" * 1000,
            "additionalDetails": json.dumps(
                {
                    "error": {
                        "code": "BAD_REQUEST",
                        "param": 'password="synthetic secret"',
                        "message": "private diagnostic body",
                    },
                    "request_id": "native-request",
                    "status_code": 400,
                }
            ),
        }
    )
    assert context["native_error_category"] == "x" * 256
    assert context["provider_error_code"] == "BAD_REQUEST"
    assert context["provider_error_param"] == 'password="[REDACTED]"'
    assert context["http_status"] == 400
    assert context["native_error_request_id"] == "native-request"
    assert "private" not in str(context)
    assert "synthetic secret" not in str(context)


@pytest.mark.parametrize("error", [None, [], 1, "private error"])
def test_malformed_payload_produces_no_context(error: object) -> None:
    assert codex_error_context(error) == {}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Fatal error: application network permission was revoked", "network_permission_revoked"),
        ("Fatal error: application network policy is unavailable", "network_policy_unavailable"),
        ("We're currently experiencing high demand.", "provider_high_demand"),
        ("provider auth command `helper` produced an empty token", "provider_auth_empty_token"),
        ("provider auth command `helper` timed out after 20000 ms", "provider_auth_timeout"),
        (
            "provider auth command `sh` exited: helper: not found",
            "provider_auth_executable_missing",
        ),
        ("provider auth command `helper` exited with status 1", "provider_auth_failed"),
        (
            "BAD_REQUEST: This endpoint retired-test-model is deprecated.",
            "provider_model_deprecated",
        ),
        ("unrelated error", "unclassified"),
        ("x" * 5000 + "application network permission was revoked", "unclassified"),
    ],
)
def test_error_reason_is_a_fixed_bounded_label(message: str, expected: str) -> None:
    assert codex_error_reason(message) == expected
