"""Bounded correlation for failed Claude session rotation requests."""

import httpx
import pytest

from omnigent.harnesses.claude_native.rotation_diagnostics import rotation_failure_attributes


@pytest.mark.parametrize(
    ("method", "path", "stage"),
    [
        ("GET", "/v1/sessions/private-session", "read_snapshot"),
        ("POST", "/v1/sessions", "create_session"),
        ("POST", "/v1/sessions/private/resources/terminals/private/transfer", "transfer_terminal"),
        ("PATCH", "/v1/sessions/private-session", "update_binding"),
        ("GET", "/private/path", "unknown"),
    ],
)
def test_rotation_http_evidence_omits_request_and_response_content(
    method: str, path: str, stage: str
) -> None:
    request = httpx.Request(
        method, "https://private.example" + path, headers={"Authorization": "private token"}
    )
    response = httpx.Response(
        403,
        request=request,
        text="private response",
        headers={"x-databricks-request-id": "private correlation"},
    )
    error = httpx.HTTPStatusError("private exception", request=request, response=response)

    fields = rotation_failure_attributes(error)

    assert fields == {
        "rotation_stage": stage,
        "authorization_present": True,
        "http_status": 403,
        "server_request_id_state": "unrecognized",
    }
    assert "private" not in repr(fields)


def test_rotation_transport_failure_without_a_request_cannot_replace_original_error() -> None:
    assert rotation_failure_attributes(httpx.ReadTimeout("private timeout")) == {}


def test_rotation_failure_omits_absent_correlation() -> None:
    request = httpx.Request("POST", "https://private.example/v1/sessions")
    response = httpx.Response(400, request=request)
    error = httpx.HTTPStatusError("private error", request=request, response=response)
    assert rotation_failure_attributes(error) == {
        "rotation_stage": "create_session",
        "authorization_present": False,
        "http_status": 400,
        "server_request_id_state": "missing",
    }
