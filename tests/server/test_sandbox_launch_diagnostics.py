from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from omnigent.server.sandbox_launch_diagnostics import sandbox_launch_failure_attributes


@pytest.mark.parametrize(
    ("error", "category", "reason"),
    [
        (None, "unknown", "missing_detail"),
        ("opaque provider failure", "unknown", "unclassified"),
        (
            "managed host did not come online within 720s — "
            "your Arclet never received your dbcert",
            "authentication",
            "client_certificate_missing",
        ),
        (
            "managed host did not come online within 720s — if sign-in expired, sign in again",
            "connectivity",
            "host_connect_timeout",
        ),
        ("runner tunnel did not connect", "connectivity", "runner_connect_timeout"),
        ("session was deleted while provisioning", "lifecycle", "launch_cancelled"),
        ("managed sandbox launch failed: quota exceeded", "quota", "quota_exceeded"),
        ("insufficient capacity in provider", "capacity", "capacity_unavailable"),
        (
            "sandbox provider 'example' is not configured",
            "configuration",
            "provider_configuration_invalid",
        ),
        ("authentication required", "authentication", "sign_in_required"),
        ("provider returned HTTP 429", "transient_provisioning", "rate_limited"),
        ("provider returned HTTP 503", "transient_provisioning", "upstream_unavailable"),
        ("Connection refused (HTTP 404)", "connectivity", "endpoint_unavailable"),
        ("provider operation timed out", "timeout", "operation_timed_out"),
        ("internal error during managed host wake", "internal", "unexpected_exception"),
        ("x" * 4096 + " quota exceeded", "unknown", "unclassified"),
    ],
)
def test_launch_failure_categories(error: str | None, category: str, reason: str) -> None:
    assert sandbox_launch_failure_attributes(error) == {
        "failure_category": category,
        "failure_reason": reason,
    }


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (RuntimeError("private-synthetic-token"), {"exception_type": "RuntimeError"}),
        (
            HTTPException(status_code=503, detail="private-synthetic-token"),
            {"exception_type": "HTTPException", "http_status": "503"},
        ),
        (
            httpx.HTTPStatusError(
                "private-synthetic-token",
                request=httpx.Request("GET", "https://example.test/private"),
                response=httpx.Response(429),
            ),
            {"exception_type": "HTTPStatusError", "http_status": "429"},
        ),
        (type("invalid-private-type", (Exception,), {})(), {"exception_type": "other"}),
    ],
)
def test_launch_exception_diagnostics_exclude_private_detail(
    exception: Exception, expected: dict[str, str]
) -> None:
    assert sandbox_launch_failure_attributes("opaque failure", exception=exception) == {
        "failure_category": "unknown",
        "failure_reason": "unclassified",
        **expected,
    }
