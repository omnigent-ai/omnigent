"""Tests for the WS tunnel frame protocol.

Each frame type round-trips through encode → decode unchanged.
Decode rejects malformed input cleanly. Body encoding picks utf-8
for text content, base64 for binary.
"""

from __future__ import annotations

import json

import pytest

from omnigent.runner.transports.ws_tunnel.frames import (
    FrameKind,
    HelloFrame,
    PingFrame,
    PongFrame,
    RequestCancelFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_body,
    decode_frame,
    encode_body,
    encode_frame,
    is_text_content_type,
)

# ── Round-trip per frame kind ────────────────────────────


def test_hello_round_trip() -> None:
    f = HelloFrame(
        runner_version="0.1.2",
        frame_protocol_version=1,
        harnesses=["claude-sdk", "codex"],
        envs=["os_sandbox"],
    )
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, HelloFrame)
    assert decoded.runner_version == "0.1.2"
    assert decoded.frame_protocol_version == 1
    assert decoded.harnesses == ["claude-sdk", "codex"]
    assert decoded.envs == ["os_sandbox"]


def test_request_round_trip_with_body_and_query_string() -> None:
    f = RequestFrame(
        id="req_abc",
        method="POST",
        path="/v1/responses",
        query_string="background=true",
        headers=[["content-type", "application/json"]],
        body='{"agent_id": "agent_x"}',
        encoding="utf-8",
        stream=True,
    )
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, RequestFrame)
    assert decoded.id == "req_abc"
    assert decoded.method == "POST"
    assert decoded.path == "/v1/responses"
    assert decoded.query_string == "background=true"
    assert decoded.headers == [["content-type", "application/json"]]
    assert decoded.body == '{"agent_id": "agent_x"}'
    assert decoded.encoding == "utf-8"
    assert decoded.stream is True


def test_request_round_trip_with_null_body() -> None:
    """GET requests have no body — encoded as null, decoded as None."""
    f = RequestFrame(id="req_g", method="GET", path="/health", body=None)
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, RequestFrame)
    assert decoded.body is None


def test_response_head_round_trip() -> None:
    f = ResponseHeadFrame(
        id="req_abc",
        status=200,
        headers=[["content-type", "text/event-stream"]],
    )
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, ResponseHeadFrame)
    assert decoded.status == 200
    assert decoded.headers == [["content-type", "text/event-stream"]]


def test_response_body_round_trip_utf8() -> None:
    f = ResponseBodyFrame(id="req_abc", body="data: {...}\n\n", encoding="utf-8")
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, ResponseBodyFrame)
    assert decoded.body == "data: {...}\n\n"
    assert decoded.encoding == "utf-8"


def test_response_body_round_trip_base64() -> None:
    """Binary bodies (file downloads) ride as base64 — preserve encoding marker."""
    f = ResponseBodyFrame(id="req_x", body="iVBORw0KGgoAAAA", encoding="base64")
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, ResponseBodyFrame)
    assert decoded.encoding == "base64"


def test_response_end_round_trip() -> None:
    f = ResponseEndFrame(id="req_abc")
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, ResponseEndFrame)
    assert decoded.id == "req_abc"


def test_request_cancel_round_trip() -> None:
    f = RequestCancelFrame(id="req_abc", reason="client_disconnected")
    decoded = decode_frame(encode_frame(f))
    assert isinstance(decoded, RequestCancelFrame)
    assert decoded.reason == "client_disconnected"


def test_ping_pong_round_trip() -> None:
    p = PingFrame(ts=1709654400000)
    decoded_p = decode_frame(encode_frame(p))
    assert isinstance(decoded_p, PingFrame)
    assert decoded_p.ts == 1709654400000

    o = PongFrame(ts=1709654400000)
    decoded_o = decode_frame(encode_frame(o))
    assert isinstance(decoded_o, PongFrame)
    assert decoded_o.ts == 1709654400000


# ── Decode failure modes ─────────────────────────────────


def test_decode_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        decode_frame("{not json")


def test_decode_rejects_non_object_root() -> None:
    """A JSON array or scalar isn't a frame; reject loudly."""
    with pytest.raises(ValueError, match="must be a JSON object"):
        decode_frame("[1, 2, 3]")


def test_decode_rejects_missing_kind() -> None:
    with pytest.raises(ValueError, match="missing 'kind'"):
        decode_frame(json.dumps({"id": "x"}))


def test_decode_rejects_unknown_kind() -> None:
    """An unknown kind string surfaces an explicit "unknown frame kind"
    error rather than silently treating it as an empty frame."""
    with pytest.raises(ValueError, match="unknown frame kind"):
        decode_frame(json.dumps({"kind": "fake.unknown_kind"}))


def test_decode_rejects_request_missing_required_fields() -> None:
    """A request frame with no method/path is structurally invalid."""
    payload = json.dumps({"kind": "request", "id": "x"})
    with pytest.raises(ValueError):
        decode_frame(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "kind": "hello",
                "runner_version": "1",
                "frame_protocol_version": 1,
                "harnesses": 123,
            },
            id="hello-harnesses-not-list",
        ),
        pytest.param(
            {"kind": "request", "id": "r", "method": "GET", "path": "/", "headers": 123},
            id="request-headers-not-list",
        ),
        pytest.param(
            {"kind": "request", "id": "r", "method": "GET", "path": "/", "stream": "yes"},
            id="request-stream-not-bool",
        ),
        pytest.param(
            {"kind": "request", "id": "r", "method": "GET", "path": "/", "body": 123},
            id="request-body-not-string",
        ),
        pytest.param(
            {"kind": "response.head", "id": "r", "status": 200, "headers": [["ok"]]},
            id="response-header-not-pair",
        ),
        pytest.param(
            {"kind": "ws.frame", "ch_id": "ch_1", "data": 123},
            id="ws-frame-data-not-string",
        ),
        pytest.param(
            {"kind": "ws.close", "ch_id": "ch_1", "code": "1000"},
            id="ws-close-code-not-int",
        ),
    ],
)
def test_decode_rejects_malformed_optional_fields(payload: dict[str, object]) -> None:
    """Bad optional fields raise ``ValueError``, not incidental exceptions.

    :param payload: Malformed frame payload.
    :returns: None.
    """
    with pytest.raises(ValueError):
        decode_frame(json.dumps(payload))


# ── Body encoding helpers ────────────────────────────────


def test_is_text_content_type_recognizes_standard_types() -> None:
    assert is_text_content_type("application/json")
    assert is_text_content_type("application/json; charset=utf-8")
    assert is_text_content_type("text/event-stream")
    assert is_text_content_type("text/plain")
    assert is_text_content_type("application/jsonl")


def test_is_text_content_type_rejects_binary() -> None:
    assert not is_text_content_type("image/png")
    assert not is_text_content_type("application/octet-stream")
    assert not is_text_content_type("application/pdf")


def test_encode_body_picks_utf8_for_text() -> None:
    body, encoding = encode_body(b'{"x": 1}', "application/json")
    assert encoding == "utf-8"
    assert body == '{"x": 1}'


def test_encode_body_picks_base64_for_binary() -> None:
    body, encoding = encode_body(b"\x89PNG", "image/png")
    assert encoding == "base64"
    # decode_body round-trips back to the same bytes.
    assert decode_body(body, encoding) == b"\x89PNG"


def test_decode_body_round_trips_both_encodings() -> None:
    assert decode_body("hello", "utf-8") == b"hello"
    assert decode_body("aGVsbG8=", "base64") == b"hello"


def test_decode_body_rejects_unknown_encoding() -> None:
    with pytest.raises(ValueError, match="unknown body encoding"):
        decode_body("x", "utf-7")


# ── Wire compatibility ──────────────────────────────────


def test_encoded_kind_uses_canonical_string() -> None:
    """Frame kinds on the wire match the documented values."""
    assert json.loads(encode_frame(PingFrame(ts=1)))["kind"] == "ping"
    assert json.loads(encode_frame(PongFrame(ts=1)))["kind"] == "pong"
    assert json.loads(encode_frame(ResponseEndFrame(id="x")))["kind"] == "response.end"
    # The dot-separated kinds are the load-bearing assertion — a
    # refactor that flipped them to "responseEnd" or "RESPONSE_END"
    # would break wire compat with any client outside the test suite.


def test_frame_kind_enum_values_match_design() -> None:
    """The FrameKind enum's wire values are the design-spec strings."""
    assert FrameKind.HELLO.value == "hello"
    assert FrameKind.REQUEST.value == "request"
    assert FrameKind.RESPONSE_HEAD.value == "response.head"
    assert FrameKind.RESPONSE_BODY.value == "response.body"
    assert FrameKind.RESPONSE_END.value == "response.end"
    assert FrameKind.REQUEST_CANCEL.value == "request.cancel"
    assert FrameKind.PING.value == "ping"
    assert FrameKind.PONG.value == "pong"
