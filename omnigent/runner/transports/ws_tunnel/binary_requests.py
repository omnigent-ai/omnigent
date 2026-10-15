"""Capability-gated, single-message raw request bodies for the runner tunnel."""

from __future__ import annotations

import json
from dataclasses import replace

from omnigent.runner.transports.ws_tunnel.frames import RequestFrame, decode_frame, encode_frame
from omnigent.util.tunnel_limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES

BINARY_REQUEST_BODY_CAPABILITY = "binary_request_body_v1"
# Leave room for the upload relay's longest deadline (finalize: 120 seconds).
BINARY_REQUEST_DISPATCH_TIMEOUT_S = 150.0
_MAGIC = b"OTB\x01"
_PREFIX_LEN = len(_MAGIC) + 4


class BinaryRequestError(ValueError):
    """Malformed envelope, with a correlation id when its header is recoverable."""

    def __init__(self, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


def encode_binary_request(frame: RequestFrame, body: bytes) -> bytes:
    """Encode metadata without a body field, then append raw bytes within the WS limit."""
    header = json.loads(encode_frame(replace(frame, body=None)))
    del header["body"]
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if _PREFIX_LEN + len(header_bytes) + len(body) > RUNNER_TUNNEL_MAX_MESSAGE_BYTES:
        raise ValueError("binary request exceeds tunnel message limit")
    return _MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes + body


def decode_binary_request(data: bytes) -> tuple[RequestFrame, bytes]:
    """Validate an envelope and reuse the text protocol's RequestFrame validation."""
    if len(data) < _PREFIX_LEN:
        raise BinaryRequestError("truncated binary request prefix")
    header_len = int.from_bytes(data[len(_MAGIC) : _PREFIX_LEN], "big")
    if (
        header_len > RUNNER_TUNNEL_MAX_MESSAGE_BYTES - _PREFIX_LEN
        or header_len > len(data) - _PREFIX_LEN
    ):
        raise BinaryRequestError("binary request header length exceeds message")
    try:
        header_text = data[_PREFIX_LEN : _PREFIX_LEN + header_len].decode("utf-8")
        header = json.loads(header_text)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BinaryRequestError("invalid binary request JSON header") from exc
    request_id = header.get("id") if isinstance(header, dict) else None
    request_id = request_id if isinstance(request_id, str) and request_id else None
    if not data.startswith(_MAGIC):
        raise BinaryRequestError("bad binary request magic/version", request_id)
    if len(data) > RUNNER_TUNNEL_MAX_MESSAGE_BYTES:
        raise BinaryRequestError("binary request exceeds tunnel message limit", request_id)
    if not isinstance(header, dict) or "body" in header:
        raise BinaryRequestError("binary request header must omit body", request_id)
    try:
        frame = decode_frame(header_text)
        if not isinstance(frame, RequestFrame):
            raise ValueError("binary envelope header must be a request")
    except ValueError as exc:
        raise BinaryRequestError(str(exc), request_id) from exc
    return frame, data[_PREFIX_LEN + header_len :]
