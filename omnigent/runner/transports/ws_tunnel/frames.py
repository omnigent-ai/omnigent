"""WebSocket tunnel frame schema (Phase 4).

Eight frame kinds, all JSON, per ``designs/RUNNER.md`` §3 "Frame
wire format". Frames carrying request/response correlation use an
``id`` field; ``hello`` / ``ping`` / ``pong`` don't.

Body-bearing frames (``request``, ``response.body``) carry an
explicit ``encoding`` field — ``"utf-8"`` (default; body is the
literal string) or ``"base64"`` (body is base64). Adapters pick
``utf-8`` for content-types like ``application/json`` /
``text/event-stream``; otherwise ``base64`` for binary payloads.

This module exports lightweight dataclasses for each kind plus
``encode_frame`` / ``decode_frame`` helpers. The dataclasses are
intentionally not pydantic — fewer dependencies, simpler
introspection, and the contract is small enough that hand-rolled
validation in decode_frame is fine.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FrameKind(str, Enum):
    """All frame kinds; the value is the JSON wire string."""

    HELLO = "hello"
    REQUEST = "request"
    RESPONSE_HEAD = "response.head"
    RESPONSE_BODY = "response.body"
    RESPONSE_END = "response.end"
    REQUEST_CANCEL = "request.cancel"
    PING = "ping"
    PONG = "pong"
    # WebSocket-channel frames: carry tunneled WS attach to the runner
    # (e.g. browser xterm.js → runner tmux). Three frames, all JSON;
    # binary WS payloads ride in ws.frame with encoding="base64".
    WS_OPEN = "ws.open"
    WS_FRAME = "ws.frame"
    WS_CLOSE = "ws.close"


# ── Frame dataclasses ────────────────────────────────────


@dataclass
class HelloFrame:
    """Runner's first frame on a fresh tunnel.

    :param runner_version: Runner's semver string, e.g. ``"0.1.2"``.
    :param frame_protocol_version: Wire-protocol major. Server refuses
        on major mismatch (RUNNER.md §2 "Version skew").
    :param harnesses: Names of harness kinds the runner can spawn.
    :param envs: Names of OS env types the runner supports.
    :param telemetry_opt_out: ``True`` when the runner's host has
        opted out of telemetry (``OMNIGENT_TELEMETRY=0``,
        ``DISABLE_TELEMETRY=true``, or ``telemetry: false`` in
        config.yaml).  The server honours this on a best-effort basis
        by skipping telemetry events for sessions on this runner.
    """

    runner_version: str
    frame_protocol_version: int
    harnesses: list[str] = field(default_factory=list)
    envs: list[str] = field(default_factory=list)
    telemetry_opt_out: bool = False


@dataclass
class RequestFrame:
    """Server → runner: execute this HTTP request locally."""

    id: str
    method: str
    path: str
    headers: list[list[str]] = field(default_factory=list)
    query_string: str = ""
    body: str | None = None
    encoding: str = "utf-8"  # "utf-8" or "base64"
    stream: bool = False


@dataclass
class ResponseHeadFrame:
    """Runner → server: status code + response headers."""

    id: str
    status: int
    headers: list[list[str]] = field(default_factory=list)


@dataclass
class ResponseBodyFrame:
    """Runner → server: a body chunk; repeated for streaming responses."""

    id: str
    body: str
    encoding: str = "utf-8"


@dataclass
class ResponseEndFrame:
    """Runner → server: end of response."""

    id: str


@dataclass
class RequestCancelFrame:
    """Server → runner: abort an in-flight request."""

    id: str
    reason: str = "client_disconnected"


@dataclass
class PingFrame:
    """Either direction: tunnel-level keepalive (request half)."""

    ts: int


@dataclass
class PongFrame:
    """Either direction: keepalive response — echoes the ping's ts."""

    ts: int


@dataclass
class WSOpenFrame:
    """Server → runner: open a tunneled WebSocket channel.

    The runner dispatches its local ASGI app at ``path`` with
    ``query_string`` and pumps frames between that endpoint and the
    server using ``ch_id`` for correlation.

    :param ch_id: Per-channel id, e.g. ``"a1b2c3d4"``. Unique within
        one runner session.
    :param path: ASGI path on the runner, e.g.
        ``"/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach"``.
    :param query_string: URL-encoded query string sans ``?``.
    """

    ch_id: str
    path: str
    query_string: str = ""


@dataclass
class WSFrame:
    """Either direction: one WebSocket frame on a channel.

    ``encoding="utf-8"`` carries the literal string payload (xterm.js
    resize JSON). ``encoding="base64"`` carries a base64 binary
    payload (PTY bytes).
    """

    ch_id: str
    data: str
    encoding: str = "utf-8"  # "utf-8" or "base64"


@dataclass
class WSCloseFrame:
    """Either direction: close a tunneled WebSocket channel."""

    ch_id: str
    code: int = 1000
    reason: str = ""


Frame = (
    HelloFrame
    | RequestFrame
    | ResponseHeadFrame
    | ResponseBodyFrame
    | ResponseEndFrame
    | RequestCancelFrame
    | PingFrame
    | PongFrame
    | WSOpenFrame
    | WSFrame
    | WSCloseFrame
)


# ── Encode / decode ──────────────────────────────────────


def encode_frame(frame: Frame) -> str:
    """Serialize a frame to its JSON wire form.

    The output is what goes onto the WebSocket as a text message.
    """
    if isinstance(frame, HelloFrame):
        return json.dumps(
            {
                "kind": FrameKind.HELLO.value,
                "runner_version": frame.runner_version,
                "frame_protocol_version": frame.frame_protocol_version,
                "harnesses": list(frame.harnesses),
                "envs": list(frame.envs),
                "telemetry_opt_out": frame.telemetry_opt_out,
            }
        )
    if isinstance(frame, RequestFrame):
        return json.dumps(
            {
                "kind": FrameKind.REQUEST.value,
                "id": frame.id,
                "method": frame.method,
                "path": frame.path,
                "query_string": frame.query_string,
                "headers": [list(h) for h in frame.headers],
                "body": frame.body,
                "encoding": frame.encoding,
                "stream": frame.stream,
            }
        )
    if isinstance(frame, ResponseHeadFrame):
        return json.dumps(
            {
                "kind": FrameKind.RESPONSE_HEAD.value,
                "id": frame.id,
                "status": frame.status,
                "headers": [list(h) for h in frame.headers],
            }
        )
    if isinstance(frame, ResponseBodyFrame):
        return json.dumps(
            {
                "kind": FrameKind.RESPONSE_BODY.value,
                "id": frame.id,
                "body": frame.body,
                "encoding": frame.encoding,
            }
        )
    if isinstance(frame, ResponseEndFrame):
        return json.dumps({"kind": FrameKind.RESPONSE_END.value, "id": frame.id})
    if isinstance(frame, RequestCancelFrame):
        return json.dumps(
            {
                "kind": FrameKind.REQUEST_CANCEL.value,
                "id": frame.id,
                "reason": frame.reason,
            }
        )
    if isinstance(frame, PingFrame):
        return json.dumps({"kind": FrameKind.PING.value, "ts": frame.ts})
    if isinstance(frame, PongFrame):
        return json.dumps({"kind": FrameKind.PONG.value, "ts": frame.ts})
    if isinstance(frame, WSOpenFrame):
        return json.dumps(
            {
                "kind": FrameKind.WS_OPEN.value,
                "ch_id": frame.ch_id,
                "path": frame.path,
                "query_string": frame.query_string,
            }
        )
    if isinstance(frame, WSFrame):
        return json.dumps(
            {
                "kind": FrameKind.WS_FRAME.value,
                "ch_id": frame.ch_id,
                "data": frame.data,
                "encoding": frame.encoding,
            }
        )
    if isinstance(frame, WSCloseFrame):
        return json.dumps(
            {
                "kind": FrameKind.WS_CLOSE.value,
                "ch_id": frame.ch_id,
                "code": frame.code,
                "reason": frame.reason,
            }
        )
    raise TypeError(f"unknown frame type: {type(frame).__name__}")


def decode_frame(text: str) -> Frame:
    """Parse a JSON wire frame back into its dataclass.

    :raises ValueError: On malformed JSON, missing ``kind``, unknown
        kind, or missing required fields for the kind.
    """
    msg = _parse_frame_object(text)
    kind = _parse_frame_kind(msg)
    return _decode_known_frame(kind, msg)


def _parse_frame_object(text: str) -> dict[str, Any]:
    """Parse a JSON frame object.

    :param text: Raw JSON frame text.
    :returns: Decoded frame object.
    :raises ValueError: If the payload is not a JSON object.
    """
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ValueError(f"frame must be a JSON object, got {type(msg).__name__}")
    return msg


def _parse_frame_kind(msg: dict[str, Any]) -> FrameKind:
    """Parse the frame kind discriminator.

    :param msg: Decoded frame object.
    :returns: Frame kind enum.
    :raises ValueError: If ``kind`` is missing or unknown.
    """
    kind = msg.get("kind")
    if not isinstance(kind, str):
        raise ValueError("frame missing 'kind' field")
    try:
        return FrameKind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown frame kind: {kind!r}") from exc


def _decode_known_frame(kind: FrameKind, msg: dict[str, Any]) -> Frame:
    """Decode a frame with a validated kind.

    :param kind: Parsed frame kind.
    :param msg: Decoded frame object.
    :returns: The typed frame dataclass.
    :raises ValueError: If the kind is unexpectedly unhandled.
    """
    match kind:
        case FrameKind.HELLO:
            return _decode_hello(msg)
        case FrameKind.REQUEST:
            return _decode_request(msg)
        case FrameKind.RESPONSE_HEAD:
            return _decode_response_head(msg)
        case FrameKind.RESPONSE_BODY:
            return _decode_response_body(msg)
        case FrameKind.RESPONSE_END:
            return ResponseEndFrame(id=_required_str(msg, "id"))
        case FrameKind.REQUEST_CANCEL:
            return _decode_request_cancel(msg)
        case FrameKind.PING:
            return PingFrame(ts=_required_int(msg, "ts"))
        case FrameKind.PONG:
            return PongFrame(ts=_required_int(msg, "ts"))
        case FrameKind.WS_OPEN:
            return _decode_ws_open(msg)
        case FrameKind.WS_FRAME:
            return _decode_ws_frame(msg)
        case FrameKind.WS_CLOSE:
            return _decode_ws_close(msg)
    # Unreachable — all enum members handled above.
    raise ValueError(f"unhandled frame kind: {kind.value!r}")  # pragma: no cover


def _decode_hello(msg: dict[str, Any]) -> HelloFrame:
    """Decode a hello frame.

    :param msg: Decoded frame object.
    :returns: Typed hello frame.
    """
    return HelloFrame(
        runner_version=_required_str(msg, "runner_version"),
        frame_protocol_version=_required_int(msg, "frame_protocol_version"),
        harnesses=_optional_str_list(msg, "harnesses"),
        envs=_optional_str_list(msg, "envs"),
        telemetry_opt_out=_optional_bool(msg, "telemetry_opt_out", False),
    )


def _decode_request(msg: dict[str, Any]) -> RequestFrame:
    """Decode a request frame.

    :param msg: Decoded frame object.
    :returns: Typed request frame.
    """
    return RequestFrame(
        id=_required_str(msg, "id"),
        method=_required_str(msg, "method"),
        path=_required_str(msg, "path"),
        query_string=_optional_str(msg, "query_string", ""),
        headers=_optional_headers(msg),
        body=_optional_body(msg),
        encoding=_optional_str(msg, "encoding", "utf-8"),
        stream=_optional_bool(msg, "stream", False),
    )


def _decode_response_head(msg: dict[str, Any]) -> ResponseHeadFrame:
    """Decode a response-head frame.

    :param msg: Decoded frame object.
    :returns: Typed response-head frame.
    """
    return ResponseHeadFrame(
        id=_required_str(msg, "id"),
        status=_required_int(msg, "status"),
        headers=_optional_headers(msg),
    )


def _decode_response_body(msg: dict[str, Any]) -> ResponseBodyFrame:
    """Decode a response-body frame.

    :param msg: Decoded frame object.
    :returns: Typed response-body frame.
    """
    return ResponseBodyFrame(
        id=_required_str(msg, "id"),
        body=_required_str(msg, "body"),
        encoding=_optional_str(msg, "encoding", "utf-8"),
    )


def _decode_request_cancel(msg: dict[str, Any]) -> RequestCancelFrame:
    """Decode a request-cancel frame.

    :param msg: Decoded frame object.
    :returns: Typed request-cancel frame.
    """
    return RequestCancelFrame(
        id=_required_str(msg, "id"),
        reason=_optional_str(msg, "reason", "client_disconnected"),
    )


def _decode_ws_open(msg: dict[str, Any]) -> WSOpenFrame:
    """Decode a WebSocket-open frame.

    :param msg: Decoded frame object.
    :returns: Typed WebSocket-open frame.
    """
    return WSOpenFrame(
        ch_id=_required_str(msg, "ch_id"),
        path=_required_str(msg, "path"),
        query_string=_optional_str(msg, "query_string", ""),
    )


def _decode_ws_frame(msg: dict[str, Any]) -> WSFrame:
    """Decode a WebSocket data frame.

    :param msg: Decoded frame object.
    :returns: Typed WebSocket data frame.
    """
    return WSFrame(
        ch_id=_required_str(msg, "ch_id"),
        data=_required_str(msg, "data"),
        encoding=_optional_str(msg, "encoding", "utf-8"),
    )


def _decode_ws_close(msg: dict[str, Any]) -> WSCloseFrame:
    """Decode a WebSocket-close frame.

    :param msg: Decoded frame object.
    :returns: Typed WebSocket-close frame.
    """
    return WSCloseFrame(
        ch_id=_required_str(msg, "ch_id"),
        code=_optional_int(msg, "code", 1000),
        reason=_optional_str(msg, "reason", ""),
    )


def _required_str(msg: dict[str, Any], key: str) -> str:
    val = msg.get(key)
    if not isinstance(val, str):
        raise ValueError(f"frame missing required string field: {key!r}")
    return val


def _required_int(msg: dict[str, Any], key: str) -> int:
    val = msg.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError(f"frame missing required int field: {key!r}")
    return val


def _optional_str(msg: dict[str, Any], key: str, default: str) -> str:
    """Return an optional string field.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"encoding"``.
    :param default: Protocol default used when the field is absent.
    :returns: The string value.
    :raises ValueError: If the field is present but not a string.
    """
    val = msg.get(key, default)
    if not isinstance(val, str):
        raise ValueError(f"frame field must be a string: {key!r}")
    return val


def _optional_bool(msg: dict[str, Any], key: str, default: bool) -> bool:
    """Return an optional boolean field.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"stream"``.
    :param default: Protocol default used when the field is absent.
    :returns: The boolean value.
    :raises ValueError: If the field is present but not a boolean.
    """
    val = msg.get(key, default)
    if not isinstance(val, bool):
        raise ValueError(f"frame field must be a boolean: {key!r}")
    return val


def _optional_int(msg: dict[str, Any], key: str, default: int) -> int:
    """Return an optional integer field.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"code"``.
    :param default: Protocol default used when the field is absent.
    :returns: The integer value.
    :raises ValueError: If the field is present but not an integer.
    """
    val = msg.get(key, default)
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError(f"frame field must be an integer: {key!r}")
    return val


def _optional_body(msg: dict[str, Any]) -> str | None:
    """Return an optional request body field.

    :param msg: Decoded request frame object.
    :returns: The body string, or ``None`` when absent.
    :raises ValueError: If ``body`` is present but not a string.
    """
    val = msg.get("body")
    if val is not None and not isinstance(val, str):
        raise ValueError("frame field must be a string or null: 'body'")
    return val


def _optional_str_list(msg: dict[str, Any], key: str) -> list[str]:
    """Return an optional list of strings.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"harnesses"``.
    :returns: A list of strings, empty when absent.
    :raises ValueError: If the field is not a string list.
    """
    val = msg.get(key, [])
    if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
        raise ValueError(f"frame field must be a list of strings: {key!r}")
    return list(val)


def _optional_headers(msg: dict[str, Any]) -> list[list[str]]:
    """Return optional HTTP headers.

    :param msg: Decoded request or response-head frame object.
    :returns: Header pairs as ``[[name, value], ...]``.
    :raises ValueError: If ``headers`` is not a list of string pairs.
    """
    val = msg.get("headers", [])
    if not isinstance(val, list):
        raise ValueError("frame field must be a list of header pairs: 'headers'")
    headers: list[list[str]] = []
    for item in val:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise ValueError("frame field must be a list of header pairs: 'headers'")
        headers.append([item[0], item[1]])
    return headers


# ── Body encoding helpers ────────────────────────────────


_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/jsonl",
    "application/x-ndjson",
    "text/event-stream",
)


def is_text_content_type(content_type: str) -> bool:
    """Decide whether a body of this content-type can be utf-8-encoded.

    True for the standard text-shaped types per RUNNER.md §3 "Frame
    wire format". False otherwise — those bodies must be base64-encoded.
    """
    ct = content_type.lower()
    return any(ct.startswith(prefix) for prefix in _TEXT_CONTENT_TYPES)


def encode_body(body: bytes, content_type: str) -> tuple[str, str]:
    """Return ``(encoded_body, encoding)`` for a body+content-type pair.

    Picks utf-8 inline for text-shaped content, base64 otherwise.
    """
    if is_text_content_type(content_type):
        return body.decode("utf-8", errors="replace"), "utf-8"
    return base64.b64encode(body).decode("ascii"), "base64"


def decode_body(body: str, encoding: str) -> bytes:
    """Decode a body string back to bytes per its declared encoding."""
    if encoding == "utf-8":
        return body.encode("utf-8")
    if encoding == "base64":
        return base64.b64decode(body)
    raise ValueError(f"unknown body encoding: {encoding!r}")
