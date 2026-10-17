"""Tests for the opaque-column compression codec (``omnigent.db.compression``).

Covers the frame format, the raw/zstd threshold, and — critically — that
values written before a column was migrated (unframed ``TEXT``) still decode
unchanged, so the ``TEXT`` → ``BLOB`` migration needs no backfill.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import StatementError

from omnigent.db.compression import _MIN_COMPRESS_BYTES, CompressedText, decode, encode


def test_statement_cache_keeps_independent_storage_limits() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            for limit in (65, 64, 65):
                statement = sa.select(
                    sa.literal("x" * 63, type_=CompressedText(max_stored_bytes=limit))
                )
                if limit == 64:
                    with pytest.raises(StatementError, match="64 stored bytes"):
                        connection.scalar(statement)
                else:
                    assert connection.scalar(statement) == "x" * 63
    finally:
        engine.dispose()


@pytest.mark.parametrize("value", ["x" * 62, "é" * 31, "é" * 3000])
def test_bounded_type_checks_encoded_bytes_on_insert_and_update(value: str) -> None:
    engine = sa.create_engine("sqlite://")
    table = sa.Table(
        "bounded_text",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("value", CompressedText(max_stored_bytes=64)),
    )
    try:
        table.create(engine)
        with engine.begin() as connection:
            connection.execute(table.insert().values(id=1, value=value))
            assert connection.scalar(sa.select(table.c.value)) == value
            assert connection.scalar(sa.select(sa.func.length(table.c.value))) <= 64
        for statement in (
            table.insert().values(id=2, value="é" * 31 + "x"),
            table.update().where(table.c.id == 1).values(value="é" * 31 + "x"),
        ):
            with pytest.raises(StatementError, match="64 stored bytes"):
                with engine.begin() as connection:
                    connection.execute(statement)
        with engine.connect() as connection:
            assert connection.execute(sa.select(table)).all() == [(1, value)]
    finally:
        engine.dispose()


@pytest.mark.parametrize("write_content_size", [False, True])
def test_bounded_decode_limits_zstd_frames(write_content_size: bool) -> None:
    import zstandard

    packed = zstandard.ZstdCompressor(write_content_size=write_content_size).compress(b"x" * 2048)
    framed = b"\x00\x01" + packed
    assert decode(framed, max_decoded_bytes=2048) == "x" * 2048
    with pytest.raises((ValueError, zstandard.ZstdError)):
        decode(framed, max_decoded_bytes=1024)
    with pytest.raises(zstandard.ZstdError):
        decode(framed[:-1], max_decoded_bytes=2048)


@pytest.mark.parametrize("value", ["é" * 10, ("é" * 10).encode(), b"\x00\x00" + b"x" * 20])
def test_bounded_decode_limits_legacy_and_raw_values(value: str | bytes) -> None:
    with pytest.raises(ValueError, match="size limit"):
        decode(value, max_decoded_bytes=15)


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
