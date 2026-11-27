"""Connect streaming frames, for tests that call a streamed RPC over raw HTTP."""

from __future__ import annotations

import json

# The flags bit that marks a stream's last frame, which carries its error, if any.
END_OF_STREAM = 0x02


def connect_frame(payload: bytes, flags: int = 0) -> bytes:
    """One Connect streaming frame: a flags byte, a big-endian length and the payload."""
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def end_of_stream_code(body: bytes) -> str | None:
    """The error code in a Connect stream's end-of-stream frame, which a refusal carries."""
    offset = 0
    while offset + 5 <= len(body):
        flags, size = body[offset], int.from_bytes(body[offset + 1 : offset + 5], "big")
        payload = body[offset + 5 : offset + 5 + size]
        offset += 5 + size
        if flags & END_OF_STREAM:
            return json.loads(payload).get("error", {}).get("code")
    return None
