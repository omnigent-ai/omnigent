"""Tests for the centralized error code / HTTP status mapping."""

from __future__ import annotations

import pytest

from omnigent.errors import _CODE_TO_HTTP_STATUS, ErrorCode, OmnigentError


def test_harness_protocol_violation_string_value() -> None:
    """The error code's string value is what appears in JSON responses.

    Clients dispatch on this string; renaming it is a wire-protocol
    change. If this assertion flips, every external consumer that
    branches on ``error.code == "harness_protocol_violation"`` breaks.
    """
    assert ErrorCode.HARNESS_PROTOCOL_VIOLATION == "harness_protocol_violation"


def test_harness_protocol_violation_maps_to_500() -> None:
    """Harness protocol violations are server-side bugs in the harness wrap.

    They surface as HTTP 500 (no client action can fix them — the harness
    implementation needs investigation). If this drifts to 4xx, callers
    might mistakenly retry or attempt user-side remediation.
    """
    assert _CODE_TO_HTTP_STATUS[ErrorCode.HARNESS_PROTOCOL_VIOLATION] == 500


def test_omnigent_error_with_harness_violation_code_returns_500() -> None:
    """End-to-end: OmnigentError(code=HARNESS_PROTOCOL_VIOLATION).http_status == 500.

    Exercises the public API path that FastAPI's exception handler uses
    to map an error to an HTTP status. If this fails, harness protocol
    violations would surface to clients as 500-with-default rather than
    500-with-the-right-code, masking the bug class.
    """
    err = OmnigentError(
        "harness emitted response.completed with outstanding elicitations",
        code=ErrorCode.HARNESS_PROTOCOL_VIOLATION,
    )
    assert err.http_status == 500
    assert err.code == ErrorCode.HARNESS_PROTOCOL_VIOLATION
    assert "outstanding elicitations" in err.message


@pytest.mark.parametrize(
    "code,expected_status",
    [
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.INVALID_INPUT, 400),
        (ErrorCode.ALREADY_EXISTS, 409),
        (ErrorCode.CONFLICT, 409),
        (ErrorCode.INTERNAL_ERROR, 500),
        (ErrorCode.HARNESS_PROTOCOL_VIOLATION, 500),
    ],
)
def test_all_error_codes_have_http_status_mapping(code: str, expected_status: int) -> None:
    """Every public ErrorCode value MUST appear in the mapping.

    A code without a mapping silently defaults to 500 in
    OmnigentError.http_status — not wrong, but it hides drift.
    This parametrized test makes adding a new ErrorCode without
    updating the mapping a noisy failure rather than a silent
    default.
    """
    assert _CODE_TO_HTTP_STATUS[code] == expected_status
