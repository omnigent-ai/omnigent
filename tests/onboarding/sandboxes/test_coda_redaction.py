"""Tests for fixed CoDA upstream-error classification."""

from __future__ import annotations

import json

from omnigent.onboarding.sandboxes.coda import _safe_control_error_detail


def test_safe_control_error_detail_never_exposes_sensitive_json() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"
    sensitive_values = (
        "header.payload.signature",
        "launch-secret",
        "api-secret",
        "private-secret",
        "cookie-secret",
        "client-secret",
        jwt,
    )
    raw = json.dumps(
        {
            "error": "upstream rejected",
            "jwt": sensitive_values[0],
            "host_token": sensitive_values[1],
            "x-api-key": sensitive_values[2],
            "private_key": sensitive_values[3],
            "cookie": f"session={sensitive_values[4]}",
            "client_secret": sensitive_values[5],
            "nested": {"message": f'embedded {{"host_token":"{sensitive_values[1]}"}} {jwt}'},
        }
    )

    detail = _safe_control_error_detail(raw)

    assert detail == "upstream control request failed"
    assert len(detail) <= 1024
    for value in sensitive_values:
        assert value not in detail


def test_safe_control_error_detail_rejects_invalid_inputs() -> None:
    for raw in ("not-json", "", b"\xff", None, object()):
        assert _safe_control_error_detail(raw) == "upstream control request failed"


def test_safe_control_error_detail_rejects_deeply_nested_json() -> None:
    deeply_nested_json = "{}"
    for _ in range(2000):
        deeply_nested_json = '{"nested":' + deeply_nested_json + "}"

    assert _safe_control_error_detail(deeply_nested_json) == "upstream control request failed"
