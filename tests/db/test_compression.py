"""Tests for the opaque-column compression codec (``omnigent.db.compression``).

Covers the frame format, the raw/zstd threshold, and — critically — that
values written before a column was migrated (unframed ``TEXT``) still decode
unchanged, so the ``TEXT`` → ``BLOB`` migration needs no backfill.
"""

from __future__ import annotations

import json

import pytest

from omnigent.db.compression import (
    _MIN_COMPRESS_BYTES,
    CompressedText,
    EncryptedText,
    decode,
    encode,
    set_column_encryptor,
)


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


# --- Encryption seam (EncryptedText / set_column_encryptor) -------------------

_ENCRYPTED_CODEC = 0x02


class _ReversibleEncryptor:
    """Test column encryptor: a marker prefix + XOR, so the ciphertext is
    distinct from the compressed input yet round-trips exactly."""

    _MARKER = b"ENC:"
    _KEY = 0x5A

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._MARKER + bytes(b ^ self._KEY for b in plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(self._MARKER), "ciphertext not produced by this encryptor"
        return bytes(b ^ self._KEY for b in ciphertext[len(self._MARKER) :])


@pytest.fixture
def column_encryptor() -> object:
    """Install the reversible test encryptor for the test, then clear it.

    The seam is process-wide, so always restore it to keep tests isolated.
    """
    encryptor = _ReversibleEncryptor()
    set_column_encryptor(encryptor)
    try:
        yield encryptor
    finally:
        set_column_encryptor(None)


def test_encrypt_flag_is_noop_without_encryptor() -> None:
    """With no encryptor installed, ``encrypt=True`` is byte-for-byte the plain path."""
    value = json.dumps({"k": "v" * 200})
    assert encode(value, encrypt=True) == encode(value, encrypt=False)


def test_encrypted_text_matches_compressed_without_encryptor() -> None:
    """``EncryptedText`` is inert (== ``CompressedText``) until an encryptor is installed."""
    value = "y" * 200
    assert EncryptedText().process_bind_param(value, None) == CompressedText().process_bind_param(
        value, None
    )


def test_encrypted_round_trip(column_encryptor: object) -> None:
    """A large value is compressed, then encrypted, and decodes back exactly."""
    value = json.dumps({"history": [{"turn": i, "note": f"entry {i}"} for i in range(300)]})
    blob = encode(value, encrypt=True)
    assert blob is not None
    assert blob[0] == 0x00 and blob[1] == _ENCRYPTED_CODEC  # sentinel + encrypted codec
    assert b"ENC:" in blob, "the installed encryptor should have run"
    assert decode(blob) == value


def test_small_value_is_encrypted_too(column_encryptor: object) -> None:
    """Sub-threshold payloads are framed raw internally, then encrypted."""
    blob = encode("hi", encrypt=True)
    assert blob is not None and blob[1] == _ENCRYPTED_CODEC
    assert decode(blob) == "hi"


def test_encrypted_text_decorator_round_trips(column_encryptor: object) -> None:
    """The ``EncryptedText`` decorator encrypts on bind and decrypts on result."""
    column = EncryptedText()
    value = json.dumps({"secret": "value", "n": 1})
    stored = column.process_bind_param(value, None)
    assert stored is not None and stored[1] == _ENCRYPTED_CODEC
    assert column.process_result_value(stored, None) == value


def test_encrypted_value_needs_encryptor_to_decode(column_encryptor: object) -> None:
    """An encrypted value can't be read once the encryptor is gone."""
    blob = encode("secret payload " * 10, encrypt=True)
    assert blob is not None
    set_column_encryptor(None)
    with pytest.raises(RuntimeError, match="no column encryptor"):
        decode(blob)


def test_clearing_encryptor_restores_plain_path(column_encryptor: object) -> None:
    """``set_column_encryptor(None)`` makes ``encrypt=True`` a no-op again."""
    set_column_encryptor(None)
    value = "x" * 200
    assert encode(value, encrypt=True) == encode(value, encrypt=False)


def test_plain_encrypted_and_legacy_rows_coexist(column_encryptor: object) -> None:
    """decode dispatches on the codec, so mixed row formats in one column all read."""
    plain = encode("legacy compressed " * 20, encrypt=False)
    encrypted = encode("newly encrypted " * 20, encrypt=True)
    legacy = b'{"unframed":"text"}'
    assert decode(plain) == "legacy compressed " * 20
    assert decode(encrypted) == "newly encrypted " * 20
    assert decode(legacy) == '{"unframed":"text"}'
