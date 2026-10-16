"""Schema rejections retain field names and types without echoed input."""

import json

import httpx
import pytest

from omnigent.harnesses.claude_native import forwarder
from omnigent.native.failure_telemetry import failure_log_attributes


def _rejection(response: httpx.Response) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("rejected", request=response.request, response=response)


def _response(body: object, **kwargs: object) -> httpx.Response:
    return httpx.Response(
        422,
        json=body,
        request=httpx.Request("POST", "http://synthetic/v1/sessions/conv_test/events"),
        **kwargs,
    )


def test_validation_rejection_retains_fields_types_and_http_request_id() -> None:
    response = _response(
        {
            "detail": [
                {
                    "loc": ["body", "SessionEventInput", "data"],
                    "type": "dict_type",
                    "msg": "private transcript text",
                    "input": "private transcript text",
                    "ctx": {"secret": "private context"},
                    "url": "https://synthetic/private-context",
                }
            ]
        },
        headers={"x-request-id": "abc123abc123abc123abc123abc123ab"},
    )
    context = forwarder._forwarder_rejection_context(_rejection(response))
    assert context["http_status"] == 422
    assert context["forwarder_http_request_id"] == response.headers["x-request-id"]
    assert context["forwarder_validation_error_count"] == 1
    assert json.loads(context["forwarder_validation_errors"]) == [
        {"loc": ["body", "SessionEventInput", "data"], "type": "dict_type"}
    ]
    assert context["forwarder_validation_truncated"] is False
    attrs = failure_log_attributes(context)
    assert attrs["forwarder_http_request_id"] == response.headers["x-request-id"]
    assert json.loads(attrs["forwarder_validation_errors"])[0]["type"] == "dict_type"
    assert "private" not in json.dumps(attrs)


def test_validation_rejection_bounds_and_redacts_dynamic_paths_and_error_types() -> None:
    body = {
        "detail": [
            {
                "loc": ["body", "data", "private-dynamic-key", i],
                "type": "private-custom-error",
                "input": "private transcript",
            }
            for i in range(30)
        ]
    }
    context = forwarder._forwarder_rejection_context(_rejection(_response(body)))
    assert context["forwarder_validation_error_count"] == 30
    assert context["forwarder_validation_truncated"] is True
    summary = json.loads(context["forwarder_validation_errors"])
    assert 1 <= len(summary) <= 8
    assert summary[0] == {"loc": ["body", "data", "*", 0], "type": "other"}
    assert len(context["forwarder_validation_errors"]) <= 1024
    assert "private" not in json.dumps(failure_log_attributes(context))


@pytest.mark.parametrize(
    ("content", "states"),
    [
        (b"{bad", {"invalid_json"}),
        (b"[" * 1500 + b"]" * 1500, {"invalid_json", "unsupported_detail"}),
    ],
    ids=["malformed-json", "excessive-nesting"],
)
def test_malformed_rejection_keeps_status_without_breaking_failure_forwarding(
    content: bytes, states: set[str]
) -> None:
    response = httpx.Response(
        422, content=content, request=httpx.Request("POST", "http://synthetic/events")
    )
    context = forwarder._forwarder_rejection_context(_rejection(response))
    assert context["http_status"] == 422
    assert context["forwarder_rejection_diagnostic_state"] in states


def test_json_decoder_recursion_error_does_not_break_failure_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _response({})

    def decode() -> object:
        raise RecursionError("synthetic decoder depth failure")

    monkeypatch.setattr(response, "json", decode)
    context = forwarder._forwarder_rejection_context(_rejection(response))
    assert context["http_status"] == 422
    assert context["forwarder_rejection_diagnostic_state"] == "invalid_json"


def test_large_rejection_is_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _response({"detail": "private transcript" * 2000})

    def unexpected_parse() -> object:
        raise AssertionError("oversized diagnostic response was parsed")

    monkeypatch.setattr(response, "json", unexpected_parse)
    context = forwarder._forwarder_rejection_context(_rejection(response))
    assert context["http_status"] == 422
    assert context["forwarder_rejection_diagnostic_state"] == "body_too_large"
    assert "private" not in json.dumps(context)


@pytest.mark.parametrize("detail", [None, {}, [None, {}, {"loc": 123}], "private transcript"])
def test_unrecognized_rejection_details_are_not_copied(detail: object) -> None:
    context = forwarder._forwarder_rejection_context(_rejection(_response({"detail": detail})))
    assert context["http_status"] == 422
    assert "private" not in json.dumps(context)
