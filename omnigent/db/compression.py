"""Transparent client-side compression (and optional encryption) for opaque
text columns.

A handful of columns hold machine-generated JSON or free text that is never
queried in SQL or read by hand — per-conversation ``session_state`` /
``session_usage``, native ``terminal_launch_args``, comment bodies/anchors,
agent descriptions, and conversation-item ``data``. Compressing them on the
client gives a uniform on-disk size across every backend: MySQL's InnoDB does
not compress ``TEXT``/``BLOB`` by default and SQLite never does, so relying on
per-backend storage compression would leave those two uncompressed while
PostgreSQL (TOAST) compresses.

Columns typed :class:`EncryptedText` are additionally encrypted at rest when a
deployment installs a :class:`ColumnEncryptor` via :func:`set_column_encryptor`
(e.g. for customer-managed-key encryption). OSS installs none, so those columns
are only compressed and behave exactly like :class:`CompressedText`.

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

Reads dispatch on the codec id, so a single column may hold a mix of legacy,
plain-zstd, and encrypted rows and each decodes correctly.
"""

from __future__ import annotations

from typing import Protocol

import zstandard
from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

# Leading byte marking a framed (post-migration) value. Legacy text never
# begins with NUL, so its presence unambiguously distinguishes the two formats.
_SENTINEL = 0x00
# Codec ids, stored as the byte after the sentinel.
_CODEC_RAW = 0x00  # payload stored uncompressed (below the size threshold)
_CODEC_ZSTD = 0x01  # payload compressed with zstd
_CODEC_ZSTD_ENCRYPTED = 0x02  # inner frame (raw/zstd) encrypted by the ColumnEncryptor

# Below this many UTF-8 bytes, zstd's frame overhead outweighs the gain, so the
# payload is framed but left uncompressed.
_MIN_COMPRESS_BYTES = 64
# Write-once / read-rarely columns, so favour ratio over speed. The payloads are
# small enough that the window size a high level implies never fills.
_LEVEL = 19


class ColumnEncryptor(Protocol):
    """Encrypts/decrypts opaque column bytes at rest.

    OSS ships without one, so :class:`EncryptedText` columns are only
    compressed. A deployment needing at-rest encryption (e.g. customer-managed
    keys) installs an implementation via :func:`set_column_encryptor`. The bytes
    handed to :meth:`encrypt` are already compressed, and the ciphertext it
    returns is stored verbatim; this module treats both directions as opaque
    ``bytes -> bytes`` and leaves all key management to the implementation.
    """

    def encrypt(self, plaintext: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> bytes: ...


# Process-wide encryptor for EncryptedText columns. ``None`` (the OSS default)
# means EncryptedText only compresses. TypeDecorator instances are created at
# import time inside the models, so a module-level seam is the only place a
# deployment can inject encryption after the fact — mirroring the
# ``set_lakebase_token_provider`` pattern in ``omnigent/db/utils.py``.
_column_encryptor: ColumnEncryptor | None = None


def set_column_encryptor(encryptor: ColumnEncryptor | None) -> None:
    """Install (or clear) the process-wide encryptor for :class:`EncryptedText`.

    OSS leaves this unset, so ``EncryptedText`` behaves exactly like
    ``CompressedText``. A deployment needing at-rest encryption installs one at
    startup; every subsequent ``EncryptedText`` write then encrypts the
    compressed bytes and reads decrypt transparently. Pass ``None`` to clear it.

    :param encryptor: The encryptor to install, or ``None`` to remove it.
    """
    global _column_encryptor
    _column_encryptor = encryptor


def encode(text: str | None, *, encrypt: bool = False) -> bytes | None:
    """Frame *text* for storage, optionally encrypting it.

    :param text: The plaintext to store, or ``None``.
    :param encrypt: When ``True`` *and* an encryptor is installed via
        :func:`set_column_encryptor`, encrypt the compressed payload. With no
        encryptor installed this flag is a no-op, so the result is byte-for-byte
        the compress-only form.
    :returns: ``sentinel + codec + payload`` bytes, or ``None`` when *text* is
        ``None``.
    """
    if text is None:
        return None
    raw = text.encode("utf-8")
    if len(raw) < _MIN_COMPRESS_BYTES:
        inner_codec, payload = _CODEC_RAW, raw
    else:
        inner_codec = _CODEC_ZSTD
        payload = zstandard.ZstdCompressor(level=_LEVEL).compress(raw)
    if encrypt and _column_encryptor is not None:
        # Encrypt the inner frame (codec byte + payload) so decode can recover
        # the compression codec after decrypting.
        sealed = _column_encryptor.encrypt(bytes((inner_codec,)) + payload)
        return bytes((_SENTINEL, _CODEC_ZSTD_ENCRYPTED)) + sealed
    return bytes((_SENTINEL, inner_codec)) + payload


def decode(value: bytes | str | memoryview | None) -> str | None:
    """Inverse of :func:`encode`; also passes through legacy unframed text.

    :param value: The stored column value: framed bytes, legacy UTF-8 bytes,
        a legacy ``str`` (SQLite dynamic typing), a ``memoryview`` (some
        drivers), or ``None``.
    :returns: The decoded plaintext, or ``None`` when *value* is ``None``.
    :raises RuntimeError: If the value is encrypted but no encryptor is
        installed to decrypt it.
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
    if codec == _CODEC_ZSTD_ENCRYPTED:
        if _column_encryptor is None:
            raise RuntimeError(
                "encountered an encrypted column value but no column encryptor is "
                "installed; call set_column_encryptor() before reading encrypted columns"
            )
        # Decrypt back to the inner frame, then fall through on its codec byte.
        inner = _column_encryptor.decrypt(payload)
        codec, payload = inner[0], inner[1:]
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


class EncryptedText(CompressedText):
    """A :class:`CompressedText` column that is also encrypted at rest when a
    :class:`ColumnEncryptor` is installed via :func:`set_column_encryptor`.

    With no encryptor installed (the OSS default) this is byte-for-byte
    identical to :class:`CompressedText` — the encryption seam stays inert until
    a deployment opts in. Reads are codec-driven (:func:`decode`), so a column
    may hold a mix of legacy, plain-zstd, and encrypted rows and each decodes
    correctly. Same constraint as ``CompressedText``: the stored bytes are
    opaque, so never filter, order, or pattern-match this column in SQL.
    """

    cache_ok = True

    def process_bind_param(self, value: str | None, _dialect: object) -> bytes | None:
        """Compress, then encrypt (if an encryptor is installed), on the way in."""
        return encode(value, encrypt=True)
