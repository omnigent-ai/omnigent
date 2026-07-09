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
  one-byte codec id and the payload. Valid text in these columns can never
  start with NUL — PostgreSQL forbids NUL in ``text`` outright, and the JSON
  they hold always leads with ``{``/``[``/``"`` — so the sentinel is an
  unambiguous "this row is framed" marker.
* **Legacy values are unframed UTF-8 text** (written while the column was
  ``TEXT``). They are detected by the absent sentinel — or, under SQLite's
  dynamic typing, by arriving as ``str`` — and returned unchanged. Each such
  row re-frames itself the next time it is written.
"""

from __future__ import annotations

import zstandard
from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

# Leading byte marking a framed (post-migration) value. Legacy text never
# begins with NUL, so its presence unambiguously distinguishes the two formats.
_SENTINEL = 0x00
# Codec ids, stored as the byte after the sentinel.
_CODEC_RAW = 0x00  # payload stored uncompressed (below the size threshold)
_CODEC_ZSTD = 0x01  # payload compressed with zstd

# Below this many UTF-8 bytes, zstd's frame overhead outweighs the gain, so the
# payload is framed but left uncompressed.
_MIN_COMPRESS_BYTES = 64
# Write-once / read-rarely columns, so favour ratio over speed. The payloads are
# small enough that the window size a high level implies never fills.
_LEVEL = 19


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


def decode(value: bytes | str | memoryview | None) -> str | None:
    """Inverse of :func:`encode`; also passes through legacy unframed text.

    :param value: The stored column value: framed bytes, legacy UTF-8 bytes,
        a legacy ``str`` (SQLite dynamic typing), a ``memoryview`` (some
        drivers), or ``None``.
    :returns: The decoded plaintext, or ``None`` when *value* is ``None``.
    """
    if value is None:
        return None
    # SQLite is dynamically typed: a value written before the column became a
    # BLOB comes back as ``str``. It is legacy plaintext, unchanged.
    if isinstance(value, str):
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not value or value[0] != _SENTINEL:
        # Empty, or legacy UTF-8 text (no sentinel — cannot start with NUL).
        return value.decode("utf-8")
    codec, payload = value[1], value[2:]
    if codec == _CODEC_ZSTD:
        return zstandard.ZstdDecompressor().decompress(payload).decode("utf-8")
    return payload.decode("utf-8")


class CompressedText(TypeDecorator):
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

    def process_bind_param(self, value: str | None, _dialect: object) -> bytes | None:
        """Compress on the way into the database."""
        return encode(value)

    def process_result_value(
        self, value: bytes | str | memoryview | None, _dialect: object
    ) -> str | None:
        """Decompress on the way out of the database."""
        return decode(value)
