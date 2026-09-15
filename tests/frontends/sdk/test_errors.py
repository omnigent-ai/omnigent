"""Unit tests for SDK HTTP error decoding helpers."""

from __future__ import annotations

import httpx
import pytest
from omnigent_client._errors import OmnigentError, require_json_object, response_body


def test_response_body_returns_text_for_non_json_error() -> None:
    """Non-JSON error bodies are preserved as text for status handling."""
    resp = httpx.Response(
        502,
        headers={"content-type": "text/html"},
        text="<html><title>Bad gateway</title></html>",
    )

    assert response_body(resp) == "<html><title>Bad gateway</title></html>"


def test_response_body_returns_json_error_object() -> None:
    """Structured server error envelopes stay structured."""
    body = {"error": {"code": "invalid_input", "message": "bad request"}}
    resp = httpx.Response(400, json=body)

    assert response_body(resp) == body


def test_require_json_object_rejects_html_success_response() -> None:
    """Successful HTML fallback pages raise ``OmnigentError`` with context."""
    resp = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<!doctype html><title>Omnigent</title>",
    )

    with pytest.raises(OmnigentError) as exc_info:
        require_json_object(resp, "GET /v1/conversations")

    message = str(exc_info.value)
    assert "GET /v1/conversations returned non-JSON response" in message
    assert "status=200" in message
    assert "text/html" in message
    assert "Omnigent" in message
    assert exc_info.value.status_code == 200


def test_require_json_object_rejects_json_array_success_response() -> None:
    """Success endpoints expecting objects reject other JSON types."""
    resp = httpx.Response(200, json=[])

    with pytest.raises(OmnigentError) as exc_info:
        require_json_object(resp, "GET /v1/conversations")

    assert str(exc_info.value) == "GET /v1/conversations returned JSON list, expected object"
    assert exc_info.value.status_code == 200


def test_require_json_object_reports_empty_body_without_content_type() -> None:
    """Empty non-JSON bodies report the missing response details explicitly."""
    resp = httpx.Response(200, content=b"")

    with pytest.raises(OmnigentError) as exc_info:
        require_json_object(resp, "GET /v1/conversations")

    message = str(exc_info.value)
    assert "status=200" in message
    assert "no content-type header" in message
    assert "<empty body>" in message


def test_raise_for_status_429_raises_rate_limited_error() -> None:
    """HTTP 429 maps to the typed transient error, not the base class.

    Callers that absorb momentary rate-limits (e.g. the resume
    picker's bounded session-list retry) need to catch exactly the
    transient case without also swallowing 4xx client bugs.
    """
    from omnigent_client._errors import RateLimitedError, raise_for_status

    body = {"error": {"code": "rate_limited", "message": "rate limited"}}
    with pytest.raises(RateLimitedError) as exc_info:
        raise_for_status(429, body)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "rate_limited"
    # Still an OmnigentError, so existing broad handlers keep working.
    assert isinstance(exc_info.value, OmnigentError)
