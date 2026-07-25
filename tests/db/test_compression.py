"""Tests for the opaque-column compression codec (``omnigent.db.compression``).

Covers the frame format, the raw/zstd threshold, and — critically — that
values written before a column was migrated (unframed ``TEXT``) still decode
unchanged, so the ``TEXT`` → ``BLOB`` migration needs no backfill.
"""

from __future__ import annotations

import json

import pytest

from omnigent.db.compression import _MIN_COMPRESS_BYTES, decode, encode


def test_none_round_trips() -> None:
    """``None`` encodes to ``None`` and decodes back to ``None``."""
    assert encode(None) is None
    assert decode(None) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x",
        json.dumps(["--dangerously-skip-permissions"]),  # small -> stored raw
        json.dumps({"input_tokens": 128345, "output_tokens": 6789, "total_tokens": 135134}),
        "café — naïve — 日本語 — 🎉 " * 5,  # multibyte utf-8
        json.dumps({"history": [{"turn": i, "note": f"entry {i}"} for i in range(300)]}),
    ],
    ids=["empty", "single-char", "small-raw", "usage-json", "unicode", "large-json"],
)
def test_round_trip(value: str) -> None:
    """Every payload decodes back to exactly what was encoded."""
    assert decode(encode(value)) == value


def test_small_values_stored_raw_large_values_compressed() -> None:
    """Sub-threshold payloads use the raw codec; larger ones use zstd."""
    small = "a" * (_MIN_COMPRESS_BYTES - 1)
    large = json.dumps({f"k{i}": f"value padding {i}" for i in range(200)})
    small_blob, large_blob = encode(small), encode(large)
    assert small_blob is not None and large_blob is not None
    assert small_blob[1] == 0x00, "small payload should be stored uncompressed"
    assert large_blob[1] == 0x01, "large payload should be zstd-compressed"
    # The whole point: the compressed blob is meaningfully smaller than raw.
    assert len(large_blob) < len(large.encode("utf-8"))


def test_framed_values_start_with_nul_sentinel() -> None:
    """New values carry the NUL sentinel that distinguishes them from legacy text."""
    blob = encode(json.dumps({"a": 1}))
    assert blob is not None and blob[0] == 0x00


def test_legacy_unframed_bytes_decode_unchanged() -> None:
    """Pre-migration UTF-8 bytes (no sentinel) pass through untouched."""
    legacy = b'{"input_tokens":5,"note":"written before migration"}'
    assert decode(legacy) == legacy.decode("utf-8")


def test_legacy_str_decodes_unchanged() -> None:
    """SQLite dynamic typing hands back legacy rows as ``str``; pass them through."""
    assert decode('{"legacy":"sqlite"}') == '{"legacy":"sqlite"}'


def test_memoryview_is_accepted() -> None:
    """Some drivers return binary columns as ``memoryview``."""
    blob = encode("x" * 200)
    assert blob is not None
    assert decode(memoryview(blob)) == "x" * 200
    assert decode(memoryview(b'{"legacy":1}')) == '{"legacy":1}'


def test_empty_bytes_decode_to_empty_string() -> None:
    """A legacy empty ``TEXT`` value (empty bytes) decodes to ``''``."""
    assert decode(b"") == ""
