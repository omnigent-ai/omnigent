"""Storage envelope for columns that hold data a deployment may encrypt.

``docs/DATABASE_BEST_PRACTICES.md`` requires such data to be opaque bounded
bytes whose plaintext and ciphertext forms are told apart without parsing the
payload. The envelope is private to the storage layer: callers read and write
JSON values, and :class:`EnvelopedJSON` packs them on the way in and out.

Stored layout (bytes):

* byte 0: the envelope format, :data:`ENVELOPE_FORMAT`.
* byte 1: the variant, plaintext or ciphertext.
* **Plaintext:** the rest is the JSON value framed by
  :func:`omnigent.db.compression.encode`.
* **Ciphertext:** bytes 2-3 are the big-endian length of a UTF-8 JSON
  metadata object naming what is needed to decrypt the rest (for example
  ``{"algorithm": ..., "key_id": ...}``), followed by that object and then the
  ciphertext of the same framed plaintext.

Writers store plaintext unless a deployment registers an
:class:`EnvelopeCipher`. Readers handle both variants; reading ciphertext
without a cipher raises :class:`EnvelopeCipherUnavailableError`.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy.engine import Dialect

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.compression import decode, encode

ENVELOPE_FORMAT = 0x01
_PLAINTEXT = 0x00
_CIPHERTEXT = 0x01
# Bound on a value's JSON text. Writes above it are refused, and reads stop at
# it so a corrupt frame cannot allocate without limit.
_MAX_DECODED_BYTES = 4 * 1024 * 1024


class EnvelopeCipherUnavailableError(RuntimeError):
    """Raised when a ciphertext envelope is read without a registered cipher."""


class EnvelopeCipher(Protocol):
    """Encrypts and decrypts envelope payloads for a deployment."""

    def encrypt(self, plaintext: bytes) -> tuple[dict[str, str], bytes]:
        """Return the metadata needed to decrypt, and the ciphertext."""
        ...

    def decrypt(self, metadata: dict[str, str], ciphertext: bytes) -> bytes:
        """Return the plaintext for *ciphertext* written with *metadata*."""
        ...


_cipher: EnvelopeCipher | None = None


def set_envelope_cipher(cipher: EnvelopeCipher | None) -> None:
    """Encrypt new envelopes with *cipher* and decrypt existing ones; ``None`` writes plaintext."""
    global _cipher
    _cipher = cipher


def seal(value: Any) -> bytes:
    """Return the envelope for a JSON-serializable *value*.

    :raises ValueError: If the value's JSON text is larger than readers accept.
    """
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode("utf-8")) > _MAX_DECODED_BYTES:
        raise ValueError(f"value exceeds {_MAX_DECODED_BYTES} bytes of JSON")
    framed = encode(text)
    assert framed is not None
    if _cipher is None:
        return bytes((ENVELOPE_FORMAT, _PLAINTEXT)) + framed
    metadata, ciphertext = _cipher.encrypt(framed)
    header = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    if len(header) > 0xFFFF:
        raise ValueError("envelope metadata is too large")
    return (
        bytes((ENVELOPE_FORMAT, _CIPHERTEXT))
        + len(header).to_bytes(2, "big")
        + header
        + ciphertext
    )


def open_envelope(stored: bytes | memoryview) -> Any:
    """Return the JSON value inside an envelope written by :func:`seal`.

    :raises ValueError: If *stored* is not a known envelope.
    :raises EnvelopeCipherUnavailableError: If it is ciphertext and no cipher
        is registered.
    """
    data = bytes(stored)
    if len(data) < 2 or data[0] != ENVELOPE_FORMAT:
        raise ValueError("unknown storage envelope format")
    variant, rest = data[1], data[2:]
    if variant == _CIPHERTEXT:
        if len(rest) < 2:
            raise ValueError("truncated envelope metadata")
        size = int.from_bytes(rest[:2], "big")
        metadata = json.loads(rest[2 : 2 + size].decode("utf-8"))
        if _cipher is None:
            raise EnvelopeCipherUnavailableError(
                "this value is encrypted and no envelope cipher is configured"
            )
        rest = _cipher.decrypt(metadata, rest[2 + size :])
    elif variant != _PLAINTEXT:
        raise ValueError("unknown storage envelope variant")
    text = decode(rest, max_decoded_bytes=_MAX_DECODED_BYTES)
    if not text:
        raise ValueError("empty storage envelope")
    return json.loads(text)


class EnvelopedJSON(BoundedBinary):
    """A JSON value stored in a bounded binary column as an envelope.

    :param max_stored_bytes: Most bytes the envelope may take, metadata and
        ciphertext included. A larger value raises ``ValueError`` on write.
    """

    cache_ok = True

    def __init__(self, max_stored_bytes: int) -> None:
        super().__init__(max_stored_bytes)

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        return None if value is None else super().process_bind_param(seal(value), dialect)

    def process_result_value(self, value: bytes | memoryview | None, dialect: Dialect) -> Any:
        del dialect
        return None if value is None else open_envelope(value)
