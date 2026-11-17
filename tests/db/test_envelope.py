"""Tests for the storage envelope of encryptable columns."""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from omnigent.db import envelope
from omnigent.db.compression import encode
from omnigent.db.envelope import (
    ENVELOPE_FORMAT,
    EnvelopeCipherUnavailableError,
    EnvelopedJSON,
    open_envelope,
    seal,
    set_envelope_cipher,
)


class _XorCipher:
    """A stand-in cipher: reversible, and visibly not plaintext."""

    def encrypt(self, plaintext: bytes) -> tuple[dict[str, str], bytes]:
        return {"algorithm": "test-xor", "key_id": "k1"}, bytes(b ^ 0x5A for b in plaintext)

    def decrypt(self, metadata: dict[str, str], ciphertext: bytes) -> bytes:
        assert metadata == {"algorithm": "test-xor", "key_id": "k1"}
        return bytes(b ^ 0x5A for b in ciphertext)


@pytest.fixture(autouse=True)
def _no_cipher() -> Iterator[None]:
    set_envelope_cipher(None)
    yield
    set_envelope_cipher(None)


def test_plaintext_round_trip_names_its_variant_up_front() -> None:
    value = {"title": "Ramp up", "links": [{"ref": "https://example.com"}], "n": 1}

    stored = seal(value)

    assert stored[:2] == bytes((ENVELOPE_FORMAT, 0x00))
    assert open_envelope(stored) == value


def test_long_values_are_compressed() -> None:
    value = {"body_md": "repeat " * 2000}

    assert len(seal(value)) < 200
    assert open_envelope(seal(value)) == value


def test_ciphertext_carries_its_metadata_and_needs_the_cipher() -> None:
    set_envelope_cipher(_XorCipher())
    stored = seal({"title": "secret"})
    assert stored[:2] == bytes((ENVELOPE_FORMAT, 0x01))
    assert b"secret" not in stored
    assert open_envelope(stored) == {"title": "secret"}

    set_envelope_cipher(None)
    with pytest.raises(EnvelopeCipherUnavailableError):
        open_envelope(stored)


def test_readers_handle_both_variants_once_a_cipher_is_configured() -> None:
    plaintext = seal(["a"])
    set_envelope_cipher(_XorCipher())

    assert open_envelope(plaintext) == ["a"]
    assert open_envelope(seal(["b"])) == ["b"]


@pytest.mark.parametrize(
    "stored",
    [
        b"",
        b"\x01",
        b"\x02\x00{}",
        b"\x01\x09{}",
        b"\x01\x01\x00",
        b"\x01\x00",
        bytes((ENVELOPE_FORMAT, 0x00)) + (encode("") or b""),
    ],
    ids=[
        "empty",
        "no-variant",
        "unknown-format",
        "unknown-variant",
        "truncated-metadata",
        "header-only-plaintext",
        "empty-plaintext",
    ],
)
def test_unknown_or_truncated_envelopes_are_rejected(stored: bytes) -> None:
    with pytest.raises(ValueError):
        open_envelope(stored)


def test_column_type_round_trips_and_bounds_the_stored_size() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "t",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("value", EnvelopedJSON(max_stored_bytes=64)),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), {"id": 1, "value": {"a": [1, 2]}})
        raw = conn.execute(sa.text("SELECT value FROM t")).scalar_one()
        assert bytes(raw)[:2] == bytes((ENVELOPE_FORMAT, 0x00))
        assert conn.execute(sa.select(table.c.value)).scalar_one() == {"a": [1, 2]}
    with pytest.raises(sa.exc.StatementError) as caught, engine.begin() as conn:
        # Random text compresses poorly, so the envelope exceeds 64 bytes.
        noise = random.Random(0).randbytes(64).hex()
        conn.execute(table.insert(), {"id": 2, "value": {"a": noise}})
    assert isinstance(caught.value.orig, ValueError)


def test_differently_bounded_columns_keep_their_own_bound_on_one_engine() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    narrow = sa.Table(
        "narrow",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("value", EnvelopedJSON(max_stored_bytes=64)),
    )
    wide = sa.Table(
        "wide",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("value", EnvelopedJSON(max_stored_bytes=4096)),
    )
    metadata.create_all(engine)
    noise = random.Random(1).randbytes(64).hex()
    # The wide column takes the value first, so a shared cached bind
    # processor would let the narrow column take it too.
    with engine.begin() as conn:
        conn.execute(wide.insert(), {"id": 1, "value": {"a": noise}})
    with pytest.raises(sa.exc.StatementError) as caught, engine.begin() as conn:
        conn.execute(narrow.insert(), {"id": 1, "value": {"a": noise}})
    assert isinstance(caught.value.orig, ValueError)


def test_values_readers_would_refuse_are_not_written() -> None:
    # The JSON text of {"a": "..."} is 8 bytes plus the string.
    at_limit = {"a": "x" * (envelope._MAX_DECODED_BYTES - 8)}
    assert open_envelope(seal(at_limit)) == at_limit

    over = {"a": "x" * (envelope._MAX_DECODED_BYTES - 7)}
    with pytest.raises(ValueError, match="exceeds"):
        seal(over)
