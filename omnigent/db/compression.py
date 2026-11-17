"""Transparent client-side compression for opaque text columns.

A handful of columns hold machine-generated JSON or free text that is never
queried in SQL or read by hand — per-conversation ``session_state`` /
``session_usage``, native ``terminal_launch_args``, comment bodies/anchors, and
agent descriptions. Compressing them on the client gives a uniform on-disk size
across every backend: MySQL's InnoDB does not compress ``TEXT``/``BLOB`` by
default and SQLite never does, so relying on per-backend storage compression
would leave those two uncompressed while PostgreSQL (TOAST) compresses.

Stored layout (bytes), chosen so post-migration and legacy rows coexist without
a backfill:

* **New values are framed:** a leading NUL sentinel (``0x00``) followed by a
  one-byte variant and the payload: raw plaintext, zstd plaintext, or ciphertext
  with its encryption configuration. Valid text in these columns can never
  start with NUL — PostgreSQL forbids NUL in ``text`` outright, and the JSON
  they hold always leads with ``{``/``[``/``"`` — so the sentinel is an
  unambiguous "this row is framed" marker.
* **Legacy values are unframed UTF-8 text** (written while the column was
  ``TEXT``). They are detected by the absent sentinel — or, under SQLite's
  dynamic typing, by arriving as ``str`` — and returned unchanged. Each such
  row re-frames itself the next time it is written.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import zstandard
from sqlalchemy import LargeBinary
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.types import TypeDecorator

# Leading byte marking a framed (post-migration) value. Legacy text never
# begins with NUL, so its presence unambiguously distinguishes the two formats.
_SENTINEL = 0x00
# Codec ids, stored as the byte after the sentinel.
_CODEC_RAW = 0x00  # payload stored uncompressed (below the size threshold)
_CODEC_ZSTD = 0x01  # payload compressed with zstd
_CODEC_ENCRYPTED = 0x02  # configuration length (uint32), JSON configuration, ciphertext

# Below this many UTF-8 bytes, zstd's frame overhead outweighs the gain, so the
# payload is framed but left uncompressed.
_MIN_COMPRESS_BYTES = 64
# Write-once / read-rarely columns, so favour ratio over speed. The payloads are
# small enough that the window size a high level implies never fills.
_LEVEL = 19


@dataclass(frozen=True)
class EncryptedTextPayload:
    """Ciphertext and the non-secret configuration a deployment needs to decrypt it."""

    ciphertext: bytes
    configuration: Mapping[str, str]


def _encryption_configuration(value: object) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or not value
        or not all(
            isinstance(key, str) and key and isinstance(item, str) and item
            for key, item in value.items()
        )
    ):
        raise ValueError("Encrypted text requires non-empty string configuration fields")
    return value


def encode_encrypted(payload: EncryptedTextPayload) -> bytes:
    """Frame deployment-produced ciphertext without also retaining its plaintext."""
    configuration = json.dumps(
        _encryption_configuration(dict(payload.configuration)), separators=(",", ":")
    ).encode("utf-8")
    if not payload.ciphertext:
        raise ValueError("Encrypted text requires ciphertext")
    return (
        bytes((_SENTINEL, _CODEC_ENCRYPTED))
        + len(configuration).to_bytes(4, "big")
        + configuration
        + payload.ciphertext
    )


def encode(text: str | None) -> bytes | None:
    """Frame *text* for storage.

    :param text: The plaintext to store, or ``None``.
    :returns: ``sentinel + codec + payload`` bytes, or ``None`` when *text* is
        ``None``.
    """
    if text is None:
        return None
    raw = text.encode("utf-8")
    if len(raw) < _MIN_COMPRESS_BYTES:
        return bytes((_SENTINEL, _CODEC_RAW)) + raw
    packed = zstandard.ZstdCompressor(level=_LEVEL).compress(raw)
    return bytes((_SENTINEL, _CODEC_ZSTD)) + packed


def decode(
    value: bytes | str | memoryview | None,
    *,
    max_decoded_bytes: int | None = None,
    decrypt: Callable[[EncryptedTextPayload], str] | None = None,
) -> str | None:
    """Inverse of :func:`encode`; also passes through legacy unframed text.

    :param value: The stored column value: framed bytes, legacy UTF-8 bytes,
        a legacy ``str`` (SQLite dynamic typing), a ``memoryview`` (some
        drivers), or ``None``.
    :param max_decoded_bytes: Optional limit enforced before decoding or allocating
        the full decompressed payload; oversized/invalid frames raise ``ValueError``
        or ``zstandard.ZstdError``.
    :param decrypt: Deployment codec for encrypted envelopes. Without one, encrypted
        rows fail closed; they are never interpreted as plaintext.
    :returns: The decoded plaintext, or ``None`` when *value* is ``None``.
    """
    if value is None:
        return None
    # SQLite is dynamically typed: a value written before the column became a
    # BLOB comes back as ``str``. It is legacy plaintext, unchanged.
    if isinstance(value, str):
        if max_decoded_bytes is not None and (
            len(value) > max_decoded_bytes or len(value.encode("utf-8")) > max_decoded_bytes
        ):
            raise ValueError("Decoded text exceeds size limit")
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not value or value[0] != _SENTINEL:
        # Empty, or legacy UTF-8 text (no sentinel — cannot start with NUL).
        payload = value
    else:
        if len(value) < 2:
            raise ValueError("Truncated compression frame")
        codec, payload = value[1], value[2:]
        if codec == _CODEC_ENCRYPTED:
            configuration_size = int.from_bytes(payload[:4], "big")
            if len(payload) < 4 or not 0 < configuration_size < len(payload) - 4:
                raise ValueError("Invalid encrypted text envelope")
            configuration = _encryption_configuration(
                json.loads(payload[4 : 4 + configuration_size])
            )
            if decrypt is None:
                raise ValueError("Encrypted text requires a configured decryptor")
            plaintext = decrypt(
                EncryptedTextPayload(payload[4 + configuration_size :], configuration)
            )
            return decode(plaintext, max_decoded_bytes=max_decoded_bytes)
        if codec == _CODEC_ZSTD:
            if max_decoded_bytes is None:
                payload = zstandard.ZstdDecompressor().decompress(payload)
            else:
                size = zstandard.frame_content_size(payload)
                if size != zstandard.CONTENTSIZE_UNKNOWN and size > max_decoded_bytes:
                    raise ValueError("Decoded text exceeds size limit")
                decompressor = zstandard.ZstdDecompressor(
                    max_window_size=max(1024, max_decoded_bytes)
                )
                payload = decompressor.decompress(payload, max_output_size=max_decoded_bytes)
        elif codec != _CODEC_RAW:
            raise ValueError("Unknown compression codec")
    if max_decoded_bytes is not None and len(payload) > max_decoded_bytes:
        raise ValueError("Decoded text exceeds size limit")
    return payload.decode("utf-8")


class CompressedText(TypeDecorator[str]):
    """A ``str`` column stored as a zstd-compressed ``BLOB`` / ``BYTEA``.

    Transparent at the ORM boundary: callers read and write ``str`` exactly as
    they would with :class:`~sqlalchemy.Text`, and compression happens on the
    way in and out. Legacy rows written when the column was ``TEXT`` decode
    unchanged and re-frame on their next write, so no backfill is required.

    Use only for columns that are never filtered, ordered, or pattern-matched
    in SQL — the stored bytes are opaque to the database.
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(
        self,
        max_stored_bytes: int | None = None,
        *,
        bounded_binary: bool = False,
        encrypt: Callable[[str], EncryptedTextPayload] | None = None,
        decrypt: Callable[[EncryptedTextPayload], str] | None = None,
    ) -> None:
        """
        :param max_stored_bytes: Largest framed (compressed) value to write, e.g.
            ``65_535`` for a MySQL ``BLOB``; ``None`` leaves writes unbounded.
        :param bounded_binary: Use a length-bounded MySQL column for a new payload.
        :param encrypt: Optional deployment codec; its full envelope must fit the limit.
        :param decrypt: Matching deployment decoder, also used for existing ciphertext.
        """
        super().__init__()
        self.max_stored_bytes = max_stored_bytes
        self.bounded_binary = bounded_binary
        if encrypt is not None and decrypt is None:
            raise ValueError("An encryption codec requires a matching decryptor")
        self.encrypt = encrypt
        self.decrypt = decrypt
        if bounded_binary:
            if max_stored_bytes is None or not 0 < max_stored_bytes < 16_384:
                raise ValueError("Bounded binary columns require a limit below 16,384 bytes")
            self.impl = LargeBinary().with_variant(VARBINARY(max_stored_bytes), "mysql")

    def process_bind_param(self, value: str | None, dialect: object) -> bytes | None:
        """Compress on the way into the database, enforcing ``max_stored_bytes``."""
        del dialect
        encoded = (
            encode_encrypted(self.encrypt(value))
            if value is not None and self.encrypt is not None
            else encode(value)
        )
        if (
            encoded is not None
            and self.max_stored_bytes is not None
            and len(encoded) > self.max_stored_bytes
        ):
            raise ValueError("Compressed text exceeds size limit")
        return encoded

    def process_result_value(
        self, value: bytes | str | memoryview | None, dialect: object
    ) -> str | None:
        """Decompress on the way out of the database."""
        del dialect
        return decode(value, decrypt=self.decrypt)
