"""Storage envelope for columns that hold data a deployment may encrypt.

``docs/DATABASE_BEST_PRACTICES.md`` requires such data to be opaque bounded
bytes whose plaintext and ciphertext forms are told apart without parsing the
payload. The envelope is private to the storage layer: callers read and write
JSON values through :class:`EnvelopedJSON`, or text through
:class:`EnvelopedText`, which pack them on the way in and out.

Stored layout (bytes):

* byte 0: the envelope format, :data:`ENVELOPE_FORMAT`.
* byte 1: the variant, plaintext or ciphertext.
* **Plaintext:** the rest is the value framed by
  :func:`omnigent.db.compression.encode`: a JSON value's text, or the text itself.
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

import zstandard
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


def _wrap(framed: bytes) -> bytes:
    """Envelope a compression frame, encrypting it when a cipher is registered."""
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


def _unwrap(stored: bytes | memoryview) -> bytes:
    """Return the compression frame inside an envelope, decrypting ciphertext.

    :raises ValueError: If *stored* is not a well-formed envelope.
    :raises EnvelopeCipherUnavailableError: If it is ciphertext and no cipher
        is registered.
    """
    data = bytes(stored)
    if len(data) < 2 or data[0] != ENVELOPE_FORMAT:
        raise ValueError("unknown storage envelope format")
    variant, rest = data[1], data[2:]
    if variant == _PLAINTEXT:
        return rest
    if variant != _CIPHERTEXT:
        raise ValueError("unknown storage envelope variant")
    if len(rest) < 2:
        raise ValueError("truncated envelope metadata")
    size = int.from_bytes(rest[:2], "big")
    if len(rest) < 2 + size:
        raise ValueError("truncated envelope metadata")
    # Malformed input must not pass for a well-formed encrypted value.
    metadata = json.loads(rest[2 : 2 + size].decode("utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("envelope metadata is not an object")
    ciphertext = rest[2 + size :]
    if not ciphertext:
        raise ValueError("ciphertext envelope holds no ciphertext")
    if _cipher is None:
        raise EnvelopeCipherUnavailableError(
            "this value is encrypted and no envelope cipher is configured"
        )
    return _cipher.decrypt(metadata, ciphertext)


def seal(value: Any) -> bytes:
    """Return the envelope for a JSON-serializable *value*.

    :raises ValueError: If the value's JSON text is larger than readers accept.
    """
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode("utf-8")) > _MAX_DECODED_BYTES:
        raise ValueError(f"value exceeds {_MAX_DECODED_BYTES} bytes of JSON")
    framed = encode(text)
    assert framed is not None
    return _wrap(framed)


def open_envelope(stored: bytes | memoryview) -> Any:
    """Return the JSON value inside an envelope written by :func:`seal`.

    :raises ValueError: If *stored* is not a known envelope.
    :raises EnvelopeCipherUnavailableError: If it is ciphertext and no cipher
        is registered.
    """
    text = decode(_unwrap(stored), max_decoded_bytes=_MAX_DECODED_BYTES)
    if not text:
        raise ValueError("empty storage envelope")
    return json.loads(text)


def seal_text(text: str) -> bytes:
    """Return the envelope for *text*, e.g. ``"Prefers tabs"``."""
    framed = encode(text)
    assert framed is not None
    return _wrap(framed)


def open_text(stored: bytes | memoryview, *, max_decoded_bytes: int) -> str:
    """Return the text inside an envelope written by :func:`seal_text`.

    :param stored: The stored envelope.
    :param max_decoded_bytes: Largest text to decompress, in UTF-8 bytes.
    :raises ValueError: If *stored* is not a well-formed text envelope, or its
        text is larger than *max_decoded_bytes*.
    :raises EnvelopeCipherUnavailableError: If it is ciphertext and no cipher
        is registered.
    """
    framed = _unwrap(stored)
    # Without the frame's NUL marker the bytes would decode as legacy text.
    if framed[:1] != b"\x00":
        raise ValueError("storage envelope holds no compression frame")
    try:
        text = decode(framed, max_decoded_bytes=max_decoded_bytes)
    except zstandard.ZstdError as exc:
        raise ValueError("storage envelope holds a corrupt compression frame") from exc
    return text or ""


class EnvelopedJSON(BoundedBinary):
    """A JSON value stored in a bounded binary column as an envelope.

    :param max_stored_bytes: Most bytes the envelope may take, metadata and
        ciphertext included. A larger value raises ``ValueError`` on write.
    """

    cache_ok = True

    def __init__(self, max_stored_bytes: int) -> None:
        super().__init__(max_stored_bytes)
        # SQLAlchemy's type cache key reads constructor arguments by name, so
        # two differently bounded columns must not share one bind processor.
        self.max_stored_bytes = max_stored_bytes

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        return None if value is None else super().process_bind_param(seal(value), dialect)

    def process_result_value(self, value: bytes | memoryview | None, dialect: Dialect) -> Any:
        del dialect
        return None if value is None else open_envelope(value)


class EnvelopedText(BoundedBinary):
    """Text stored in a bounded binary column as an envelope.

    Both the envelope and the text it holds are limited to *max_stored_bytes*,
    and reads decompress at most that much, so every value written can be read
    back and a corrupt frame cannot allocate past the column's size.

    :param max_stored_bytes: Most bytes the envelope may take, and most UTF-8
        bytes of text, e.g. ``16_383``. A larger value raises ``ValueError`` on
        write.
    """

    cache_ok = True

    def __init__(self, max_stored_bytes: int) -> None:
        super().__init__(max_stored_bytes)
        # See EnvelopedJSON: the type cache key reads this argument by name.
        self.max_stored_bytes = max_stored_bytes

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        # Text that compresses well could fit the column yet be refused on read.
        if len(value.encode("utf-8")) > self.max_stored_bytes:
            raise ValueError(f"text exceeds {self.max_stored_bytes} bytes")
        return super().process_bind_param(seal_text(value), dialect)

    def process_result_value(self, value: bytes | memoryview | None, dialect: Dialect) -> Any:
        del dialect
        return None if value is None else open_text(value, max_decoded_bytes=self.max_stored_bytes)
