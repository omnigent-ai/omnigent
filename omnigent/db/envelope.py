"""Storage envelope for text that may be encrypted later.

The database guide asks that such values be opaque bytes whose format says,
without parsing the payload, whether they hold plaintext or ciphertext. Every
value starts with two bytes:

* byte 0, the format version: ``1``;
* byte 1, the variant: ``0`` for plaintext, ``1`` for ciphertext.

A plaintext value continues with the text's :mod:`omnigent.db.compression`
frame. A ciphertext value continues with one byte giving the length *n* (1-255)
of the key reference, *n* bytes of UTF-8 key reference naming the key that
encrypted it, then the ciphertext.

Nothing writes ciphertext yet. Readers decode plaintext and raise
:class:`EncryptedValueError` for ciphertext, so an encrypted value is never
returned as text. The envelope stays inside the storage layer; callers read and
write ``str``.
"""

from __future__ import annotations

from typing import cast

import zstandard
from sqlalchemy import LargeBinary
from sqlalchemy.dialects.mysql import VARBINARY as MySQLVarbinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from omnigent.db.compression import decode, encode

_VERSION = 1
_PLAINTEXT = 0
_CIPHERTEXT = 1


class EnvelopeError(ValueError):
    """A stored value is not a readable envelope."""


class EncryptedValueError(EnvelopeError):
    """
    A stored value is ciphertext, and this build cannot decrypt it.

    :param key_ref: The key reference recorded with the ciphertext.
    """

    def __init__(self, key_ref: str) -> None:
        super().__init__(f"Stored value is encrypted with key {key_ref!r}")
        self.key_ref = key_ref


def seal(text: str) -> bytes:
    """
    Wrap *text* in a plaintext envelope.

    :param text: The text to store, e.g. ``"Prefers tabs"``.
    :returns: ``version + variant + compression frame``.
    """
    # ``encode`` returns ``None`` only for ``None``.
    return bytes((_VERSION, _PLAINTEXT)) + cast("bytes", encode(text))


def unseal(value: bytes | memoryview, *, max_decoded_bytes: int) -> str:
    """
    Read the text out of an envelope.

    :param value: The stored bytes.
    :param max_decoded_bytes: Largest text to decompress, in UTF-8 bytes.
    :returns: The plaintext.
    :raises EncryptedValueError: For a ciphertext envelope.
    :raises EnvelopeError: For a truncated envelope, or an unknown version or variant.
    """
    data = bytes(value)
    if len(data) < 2 or data[0] != _VERSION:
        raise EnvelopeError("Stored value has no known envelope version")
    variant, body = data[1], data[2:]
    if variant == _PLAINTEXT:
        # The frame must be present: unframed bytes would decode as legacy text.
        if body[:1] != b"\x00":
            raise EnvelopeError("Stored plaintext is not a compression frame")
        try:
            text = decode(body, max_decoded_bytes=max_decoded_bytes)
        except (ValueError, zstandard.ZstdError) as exc:
            raise EnvelopeError("Stored plaintext is not a valid frame") from exc
        return text or ""
    if variant == _CIPHERTEXT:
        key_ref_length = body[0] if body else 0
        if key_ref_length == 0 or len(body) <= key_ref_length:
            raise EnvelopeError("Stored ciphertext has no key reference")
        raise EncryptedValueError(body[1 : 1 + key_ref_length].decode("utf-8", "replace"))
    raise EnvelopeError(f"Stored value has unknown envelope variant {variant}")


class EnvelopedText(TypeDecorator[str]):
    """
    A ``str`` column stored as a size-limited envelope.

    MySQL gets ``VARBINARY(max_stored_bytes)``. PostgreSQL (``BYTEA``) and SQLite
    (``BLOB``) have no length-bounded binary type, so writes are checked here
    instead. Both the stored envelope and the text it holds are limited to
    ``max_stored_bytes``, so every value written can be read back. The stored
    bytes are opaque to the database: never filter, sort or index on them.
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(self, max_stored_bytes: int) -> None:
        """
        :param max_stored_bytes: Largest envelope, and largest UTF-8 text, to
            write, e.g. ``16_383``; also the MySQL ``VARBINARY`` length and the
            decompression bound on reads.
        """
        super().__init__()
        self.max_stored_bytes = max_stored_bytes

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[bytes]:
        """Use a length-bounded binary type where the dialect has one."""
        if dialect.name == "mysql":
            return dialect.type_descriptor(MySQLVarbinary(self.max_stored_bytes))
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: str | None, dialect: object) -> bytes | None:
        """Seal on the way into the database, enforcing ``max_stored_bytes``."""
        del dialect
        if value is None:
            return None
        # Reads decompress at most max_stored_bytes, so larger text, however well it
        # compresses, would be stored but unreadable.
        if len(value.encode("utf-8")) > self.max_stored_bytes:
            raise ValueError("Enveloped text exceeds size limit")
        sealed = seal(value)
        if len(sealed) > self.max_stored_bytes:
            raise ValueError("Enveloped value exceeds size limit")
        return sealed

    def process_result_value(
        self, value: bytes | memoryview | None, dialect: object
    ) -> str | None:
        """Unseal on the way out of the database."""
        del dialect
        if value is None:
            return None
        return unseal(value, max_decoded_bytes=self.max_stored_bytes)
