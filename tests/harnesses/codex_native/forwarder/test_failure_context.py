"""Codex provider evidence survives the forwarder's status transport."""

import json

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["turn", "notification", "error_item"])
async def test_failed_status_preserves_provider_evidence(source: str) -> None:
    payload = {
        "message": "The upstream request failed.",
        "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 503}},
        "request_id": "opaque-error-request",
        "gateway_request_id": "gateway-request",
        "provider_request_id": "provider-request",
        "provider_error_code": "upstream_unavailable",
        "code": "unqualified-native-code",
        "failure_source": "forged",
        "runner_version": "forged",
    }
    if source == "notification":
        error = forwarder._terminal_error_from_notification({"error": payload})
    else:
        turn = (
            {"error": payload} if source == "turn" else {"items": [{"type": "error", **payload}]}
        )
        error = forwarder._terminal_error_from_turn({"turn": turn})
    assert error is not None
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    edge = forwarder._CodexTurnStatusEdge(
        status="failed", turn_id="native-turn", source="turn/completed:turn-error", error=error
    )
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        await forwarder._post_turn_status_edge(client, "omnigent-session", edge)
        await forwarder._post_turn_status_edge(client, "omnigent-session", edge)
    first, second = [r["data"] for r in requests]
    assert first == second
    assert first["status"] == "failed"
    assert first["output"] == payload["message"]
    assert first["response_id"] == "codex_native-turn"
    assert "reauth_required" not in first
    context = first["failure_context"]
    assert context["failure_source"] == "codex_terminal_turn"
    assert context["native_harness"] == "codex-native"
    assert context["native_error_category"] == "responseTooManyFailedAttempts"
    assert context["http_status"] == 503
    assert context["provider_error_code"] == "upstream_unavailable"
    assert context["native_error_request_id"] == "opaque-error-request"
    assert context["gateway_request_id"] == "gateway-request"
    assert context["provider_request_id"] == "provider-request"
    assert context["failure_id"]
    assert context["runner_version"] != "forged"


@pytest.mark.asyncio
async def test_evidence_is_bounded_and_does_not_override_lifecycle_fields() -> None:
    error = forwarder._terminal_error_from_turn(
        {
            "turn": {
                "error": {
                    "message": 'password="synthetic secret" ' + "x" * 3000,
                    "codexErrorInfo": {"type": "Other", "httpStatusCode": True},
                    "gateway_request_id": "g" * 1000,
                    "status": "idle",
                    "session_id": "another-session",
                    "request_id": {"secret": "synthetic secret"},
                }
            }
        }
    )
    assert error is not None
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        await forwarder._post_turn_status_edge(
            client,
            "omnigent-session",
            forwarder._CodexTurnStatusEdge(
                status="failed",
                turn_id="native-turn",
                source="turn/completed:turn-error",
                error=error,
            ),
        )
    data = requests[0]["data"]
    assert data["status"] == "failed"
    context = data["failure_context"]
    assert "synthetic secret" not in json.dumps(context)
    assert len(context["native_error_message"]) <= 1024
    assert len(context["gateway_request_id"]) == 256
    assert "http_status" not in context
    assert "session_id" not in context
    assert "native_error_request_id" not in context


_PROVIDER_KEYS = frozenset(
    {
        "provider_error_type",
        "provider_error_code",
        "provider_error_param",
        "native_error_request_id",
    }
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            'unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED","message":'
            '"Budget \\"test\\" has reached its limit","request_id":"req-gateway-1"}, '
            "url: https://host/ai-gateway/codex/v1/responses",
            {
                "http_status": 403,
                "provider_error_code": "PERMISSION_DENIED",
                "native_error_request_id": "req-gateway-1",
            },
        ),
        (
            'unexpected status 401 Unauthorized: {"error": {"type": "invalid_request_error", '
            '"code": "invalid_api_key", "param": null, "message": "Incorrect API key"}}, '
            "url: https://host/v1/responses",
            {
                "http_status": 401,
                "provider_error_type": "invalid_request_error",
                "provider_error_code": "invalid_api_key",
            },
        ),
        (
            "unexpected status 502 Bad Gateway: <html>upstream unavailable</html>, url: https://host/x",
            {"http_status": 502},
        ),
        # A rejection quoted inside another error is not this turn's rejection,
        # whether it sits mid-line or on its own line after other text.
        (
            'tool output contained "unexpected status 401 Unauthorized: {"error_code":"X"}" '
            "but the turn failed while writing the patch",
            {"http_status": None},
        ),
        (
            'command stderr:\nunexpected status 401 Unauthorized: {"error_code":"X"}, url: https://h/x',
            {"http_status": None},
        ),
    ],
)
def test_gateway_rejection_message_supplies_status_and_error_code(
    message: str, expected: dict[str, object]
) -> None:
    """The gateway envelope codex embeds in the message becomes bounded evidence."""
    error = forwarder._terminal_error_from_notification(
        {
            "error": {
                "message": message,
                "codexErrorInfo": {"responseStreamConnectionFailed": {"httpStatusCode": None}},
                "additionalDetails": "Connection failed: error sending request",
            }
        }
    )
    assert error is not None
    context = error.failure_context
    assert context is not None
    assert context["native_error_category"] == "responseStreamConnectionFailed"
    assert context.get("http_status") == expected["http_status"]
    assert {key: context[key] for key in _PROVIDER_KEYS & set(context)} == {
        key: expected[key] for key in _PROVIDER_KEYS & set(expected)
    }
    assert context["native_error_message"].endswith("Connection failed: error sending request")


def test_blank_direct_fields_fall_back_to_gateway_evidence() -> None:
    """A blank or malformed direct field does not shadow the value parsed from the gateway body."""
    error = forwarder._terminal_error_from_notification(
        {
            "error": {
                "message": (
                    'unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED",'
                    '"request_id":"req-gateway-1","param":"model"}, url: https://host/x'
                ),
                "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 403}},
                "request_id": "   ",
                "provider_error_code": "",
                "provider_error_param": {"nested": True},
                "gateway_request_id": "direct-gateway-id",
            }
        }
    )
    assert error is not None
    context = error.failure_context
    assert context is not None
    assert context["native_error_request_id"] == "req-gateway-1"
    assert context["provider_error_code"] == "PERMISSION_DENIED"
    assert context["provider_error_param"] == "model"
    assert context["gateway_request_id"] == "direct-gateway-id"
