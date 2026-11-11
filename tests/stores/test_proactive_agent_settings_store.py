"""Decoding of stored proactive-agent settings values."""

from __future__ import annotations

import pytest

from omnigent.db.compression import encode
from omnigent.stores.proactive_agent_settings_store import _decode_enabled


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        (encode("true"), True),
        (encode("false"), False),
        (b"false", False),
        ("false", False),
        (memoryview(encode("false") or b""), False),
        # Unreadable values read as on: not JSON, the wrong JSON type, a
        # truncated frame, a corrupt compressed frame, and an oversized value.
        (b"not json", True),
        (encode('"yes"'), True),
        (encode("null"), True),
        (b"\x00", True),
        (b"\x00\x01not a zstd frame", True),
        (encode("[" + "0," * 100 + "0]"), True),
    ],
    ids=[
        "missing",
        "true",
        "false",
        "legacy-false",
        "sqlite-str-false",
        "memoryview-false",
        "not-json",
        "string",
        "null",
        "truncated-frame",
        "corrupt-compressed-frame",
        "oversized",
    ],
)
def test_decode_enabled(raw: bytes | str | memoryview | None, expected: bool) -> None:
    assert _decode_enabled(raw) is expected
