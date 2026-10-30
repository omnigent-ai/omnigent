"""Canonical UUID normalization shared by clients and database columns."""

from __future__ import annotations

import uuid

# Hex length of a bare uuid4 id, the canonical Python-side form.
_UUID_HEX_LEN = 32

# Accept historical prefixes so old URLs and clients keep resolving; reject
# unknown prefixes so a different ID type cannot silently become a UUID.
_LEGACY_ID_PREFIXES = frozenset(
    {
        "ag",
        "conv",
        "host",
        "pol",
        "file",
        "cmt",
        # conversation-item per-type prefixes
        "msg",
        "fc",
        "fco",
        "err",
        "rs",
        "cmp",
        "nt",
        "rse",
        "sc",
        "tc",
        "rd",
        # runner-internal conversation binding
        "agy_conv",
    }
)


class InvalidUuidError(ValueError):
    """An id string could not be normalised to a 32-char hex uuid.

    Subclasses ``ValueError`` so existing ``except ValueError`` sites keep
    working.
    """


def uuid_to_bytes(value: str | uuid.UUID) -> bytes:
    """Normalise a supported UUID spelling to its 16 raw bytes.

    Accepts, reducing them all to the same 16 bytes: a :class:`uuid.UUID`
    object; the bare 32-char hex form (what generators emit); the dashed
    canonical uuid (``str(uuid4())``); and a legacy id carrying one of the
    known :data:`_LEGACY_ID_PREFIXES` (``conv_<hex>``, ``ag_<hex>``, …) — so
    old bookmarked URLs, pasted ids, and pre-migration clients keep resolving.
    Anything else — a truncated id, non-hex text, an unknown prefix — fails
    loud rather than silently storing the wrong bytes.

    :param value: A ``uuid.UUID``, or a 32-char hex uuid optionally dashed or
        legacy-prefixed.
    :returns: The 16-byte big-endian value.
    :raises InvalidUuidError: If *value* is not a 32-char hex uuid.
    """
    if isinstance(value, uuid.UUID):
        return value.bytes
    normalized = value.replace("-", "")
    if "_" in normalized:
        prefix, _, tail = normalized.rpartition("_")
        if prefix in _LEGACY_ID_PREFIXES and len(tail) == _UUID_HEX_LEN:
            normalized = tail
    if len(normalized) != _UUID_HEX_LEN:
        raise InvalidUuidError(f"expected a 32-char hex uuid, got {value!r}")
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise InvalidUuidError(f"invalid hex uuid: {value!r}") from exc


def normalize_uuid(value: str | None) -> str | None:
    """Return the bare 32-char hex form of *value*, or *value* unchanged.

    The forgiving companion to :func:`uuid_to_bytes` for **Python-side** id
    comparisons (e.g. a store's scope check against an ORM attribute, which
    always reads back bare hex). A legacy-prefixed or dashed input normalises
    to bare hex; a malformed input is returned as-is so the comparison simply
    mismatches — preserving the pre-migration "unknown id = not found"
    behaviour instead of raising. ``None`` passes through.

    :param value: Any caller-supplied id string, or ``None``.
    :returns: The bare 32-char hex form, or *value* verbatim if not a uuid.
    """
    if value is None:
        return None
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError:
        return value
