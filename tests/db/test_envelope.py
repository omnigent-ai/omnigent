"""Tests for the storage envelope (``omnigent.db.envelope``)."""

from __future__ import annotations

import random
import string

import pytest
import zstandard
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from omnigent.db.compression import encode
from omnigent.db.envelope import (
    EncryptedValueError,
    EnvelopedText,
    EnvelopeError,
    seal,
    unseal,
)


def _ciphertext(key_ref: bytes, payload: bytes = b"\x8a\x11opaque") -> bytes:
    return bytes((1, 1, len(key_ref))) + key_ref + payload


@pytest.mark.parametrize(
    "text",
    ["", "Prefers tabs", "é" * 40, "".join(random.Random(0).choices(string.printable, k=5_000))],
    ids=["empty", "short", "multibyte", "long"],
)
def test_plaintext_round_trips_behind_its_variant_bytes(text: str) -> None:
    sealed = seal(text)

    assert sealed[:2] == b"\x01\x00"
    assert sealed[2:] == encode(text)
    assert unseal(sealed, max_decoded_bytes=16_383) == text
    assert unseal(memoryview(sealed), max_decoded_bytes=16_383) == text


def test_ciphertext_is_never_returned_as_text() -> None:
    with pytest.raises(EncryptedValueError) as excinfo:
        unseal(_ciphertext(b"kms:memories-2026"), max_decoded_bytes=16_383)

    assert excinfo.value.key_ref == "kms:memories-2026"


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"\x01",
        b"\x00\x00\x00plain",  # a bare compression frame, not an envelope
        b"\x02\x00\x00\x00x",  # unknown version
        b"\x01\x07\x00\x00x",  # unknown variant
        b"\x01\x00plain",  # plaintext without a compression frame
        b"\x01\x01",  # ciphertext without a key reference
        b"\x01\x01\x00payload",  # zero-length key reference
        b"\x01\x01\x05key",  # key reference longer than the value
        b"\x01\x00\x00\x01not zstd",  # corrupt compressed frame
    ],
    ids=[
        "empty",
        "version-only",
        "bare-frame",
        "unknown-version",
        "unknown-variant",
        "unframed-plaintext",
        "no-key-ref",
        "empty-key-ref",
        "short-key-ref",
        "corrupt-frame",
    ],
)
def test_unreadable_envelopes_are_rejected(value: bytes) -> None:
    with pytest.raises(EnvelopeError) as excinfo:
        unseal(value, max_decoded_bytes=16_383)

    # Malformed input must not pass for a well-formed encrypted value.
    assert not isinstance(excinfo.value, EncryptedValueError)


def test_decompression_is_bounded() -> None:
    bomb = bytes((1, 0, 0, 1)) + zstandard.ZstdCompressor().compress(b"x" * 100_000)

    with pytest.raises(EnvelopeError):
        unseal(bomb, max_decoded_bytes=16_383)


@pytest.mark.parametrize(
    "value",
    [
        "x" * 10,
        # Short random text is still framed with zstd but barely shrinks.
        "".join(random.Random(1).choices(string.printable, k=100)),
    ],
    ids=["raw", "zstd"],
)
def test_writes_are_bounded_including_the_envelope(value: str) -> None:
    stored = len(seal(value))
    assert stored >= len(value.encode())

    assert EnvelopedText(stored).process_bind_param(value, None) == seal(value)
    with pytest.raises(ValueError, match="size limit"):
        EnvelopedText(stored - 1).process_bind_param(value, None)


@pytest.mark.parametrize("limit", [16_383, 2_100])
def test_text_that_compresses_well_is_still_bounded_so_it_stays_readable(limit: int) -> None:
    column = EnvelopedText(limit)

    at_limit = column.process_bind_param("x" * limit, None)
    assert at_limit is not None
    assert column.process_result_value(at_limit, None) == "x" * limit
    with pytest.raises(ValueError, match="size limit"):
        column.process_bind_param("x" * (limit + 1), None)


def test_none_round_trips_as_null() -> None:
    column = EnvelopedText(100)

    assert column.process_bind_param(None, None) is None
    assert column.process_result_value(None, None) is None


def test_the_column_type_declares_its_bound_where_the_engine_can() -> None:
    table = Table("t", MetaData(), Column("body", EnvelopedText(16_383)))

    def ddl(dialect: object) -> str:
        return str(CreateTable(table).compile(dialect=dialect))

    assert "body VARBINARY(16383)" in ddl(mysql.dialect())
    assert "body BYTEA" in ddl(postgresql.dialect())
    assert "body BLOB" in ddl(sqlite.dialect())
