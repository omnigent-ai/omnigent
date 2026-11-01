"""A storage envelope for values that may be encrypted.

The database guide asks for data that could be encrypted to be stored as
opaque, bounded bytes whose variant is explicit, so turning encryption on
later needs no schema change and no reader has to guess. The envelope is
private to the storage layer: stores hand callers plain values.

Layout (bytes):

* ``[0]`` format version, :data:`ENVELOPE_VERSION`.
* ``[1]`` variant: :data:`PLAINTEXT` or :data:`CIPHERTEXT`.
* Plaintext: the payload.
* Ciphertext: a two-byte big-endian length, that many UTF-8 bytes naming the
  key that sealed it (the encryption configuration a reader needs), then the
  ciphertext.

Every stored value, envelope included, is at most :data:`MAX_ENVELOPE_BYTES`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Stored values stay below the 16 KiB per-value limit, envelope overhead included.
MAX_ENVELOPE_BYTES = 16_383
ENVELOPE_VERSION = 1
PLAINTEXT = 0
CIPHERTEXT = 1


class EnvelopeError(ValueError):
    """A stored value is not a well-formed envelope, or does not fit one."""


@dataclass(frozen=True)
class Sealed:
    """The ciphertext variant: *ciphertext* under the key *key_ref* names."""

    key_ref: str
    ciphertext: bytes


def seal(payload: bytes | Sealed) -> bytes:
    """Wrap *payload* (plaintext bytes, or a :class:`Sealed` value) for storage.

    :raises EnvelopeError: When the stored bytes would exceed :data:`MAX_ENVELOPE_BYTES`.
    """
    if isinstance(payload, Sealed):
        key = payload.key_ref.encode("utf-8")
        if len(key) > 0xFFFF:
            raise EnvelopeError("key reference is too long")
        stored = (
            bytes((ENVELOPE_VERSION, CIPHERTEXT))
            + len(key).to_bytes(2, "big")
            + key
            + payload.ciphertext
        )
    else:
        stored = bytes((ENVELOPE_VERSION, PLAINTEXT)) + payload
    if len(stored) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(
            f"stored value of {len(stored)} bytes is larger than {MAX_ENVELOPE_BYTES}"
        )
    return stored


def open_envelope(stored: bytes | memoryview) -> bytes | Sealed:
    """The plaintext payload, or the :class:`Sealed` value, *stored* holds.

    :raises EnvelopeError: On an unknown version or variant, or a truncated value.
    """
    data = bytes(stored)
    if len(data) < 2 or data[0] != ENVELOPE_VERSION:
        raise EnvelopeError("not a storage envelope this version reads")
    if data[1] == PLAINTEXT:
        return data[2:]
    if data[1] == CIPHERTEXT:
        if len(data) < 4:
            raise EnvelopeError("truncated ciphertext envelope")
        length = int.from_bytes(data[2:4], "big")
        if len(data) < 4 + length:
            raise EnvelopeError("truncated ciphertext envelope")
        return Sealed(data[4 : 4 + length].decode("utf-8"), data[4 + length :])
    raise EnvelopeError(f"unknown envelope variant {data[1]}")
