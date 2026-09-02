"""Shared helpers for translating multimodal content.

Provides data-URI parsing used by Anthropic, Gemini, and Bedrock
adapters when converting Chat Completions ``image_url`` parts to
their provider-native image formats, plus recursive redaction for
history paths that serialize inline attachments as text.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar, cast

_INLINE_BASE64_DATA_URI = re.compile(
    r"data:([^;,\s]*)(?:;[^;,\r\n]*)*;base64,[ \t]?([A-Za-z0-9+/=_-]+)",
    re.IGNORECASE,
)

_Value = TypeVar("_Value")

#: Standard and URL-safe base64 alphabets, plus ``=`` padding.
_BASE64_ALPHABET_ONLY = re.compile(r"[A-Za-z0-9+/=_-]+")

#: Source ``type`` values that name content rather than a binary payload. A
#: source with no ``type`` at all is treated as binary, since a real payload
#: field is the likelier reading and leaking one is worse than over-redacting.
_NON_BINARY_SOURCE_TYPES = frozenset({"text", "url", "content", "file"})

#: Length above which an alphabet-only value is taken as binary even without
#: base64's length or padding tells. Short punctuation-free text (``README``,
#: ``line1\nline2``) stays below it; a clipped attachment runs into thousands.
_MIN_UNTELLED_BLOB_CHARS = 64

#: Payload length below which replacing an inline ``data:`` URI is not worth it:
#: callers' markers run ~70-80 characters, so a shorter payload grows the text
#: while destroying a legitimate small inline asset (an icon SVG, a 1px PNG).
#: Real attachments are orders of magnitude larger.
_MIN_REDACTABLE_URI_PAYLOAD = 128


@dataclass
class DataUriParts:
    """
    Parsed components of a ``data:`` URI.

    :param media_type: MIME type, e.g. ``"image/png"``.
    :param data: Base64-encoded payload.
    """

    media_type: str
    data: str


def parse_data_uri(uri: str) -> DataUriParts | None:
    """
    Parse a ``data:`` URI into its media type and base64 payload.

    Returns ``None`` for non-data URIs (e.g. ``https://...``)
    so callers can distinguish between inline content and external
    URLs that must be passed through to the provider.

    :param uri: A URI string, e.g.
        ``"data:image/png;base64,iVBOR..."`` or
        ``"https://example.com/img.png"``.
    :returns: Parsed :class:`DataUriParts`, or ``None`` if the
        URI is not a ``data:`` URI.
    """
    if not uri.startswith("data:"):
        return None

    # Format: data:<media_type>;base64,<data>
    rest = uri[len("data:") :]
    separator = ";base64,"
    sep_idx = rest.find(separator)
    if sep_idx == -1:
        return None

    media_type = rest[:sep_idx]
    data = rest[sep_idx + len(separator) :]
    return DataUriParts(media_type=media_type, data=data)


def redact_inline_data_uris(
    value: _Value,
    marker: Callable[[str, int], str],
) -> _Value:
    """Recursively replace inline base64 data URIs with compact markers.

    Dict keys and all non-data-URI values are preserved. String values may
    contain surrounding text; only matching ``data:*;base64,...`` spans are
    replaced. Payload matching is intentionally single-line; embedded newlines
    are not joined because doing so could consume adjacent prose.

    :param value: Arbitrarily nested dict/list/string content.
    :param marker: Builds replacement text from media type and payload length.
    :returns: A copy with inline base64 payloads redacted.
    """
    if isinstance(value, str):
        return cast(
            _Value,
            _INLINE_BASE64_DATA_URI.sub(
                lambda match: (
                    marker(match.group(1), len(match.group(2)))
                    if len(match.group(2)) >= _MIN_REDACTABLE_URI_PAYLOAD
                    else match.group(0)
                ),
                value,
            ),
        )
    if isinstance(value, list):
        return cast(_Value, [redact_inline_data_uris(item, marker) for item in value])
    if isinstance(value, dict):
        return cast(
            _Value,
            {key: redact_inline_data_uris(item, marker) for key, item in value.items()},
        )
    return value


# Content-block types whose payload is a base64 blob rather than text.
_BINARY_BLOCK_TYPES = frozenset({"image", "document", "file"})


def _is_base64_payload(value: str) -> bool:
    """Whether *value* is a base64 blob rather than ordinary text.

    Deliberately not a decode: a payload clipped mid-blob is exactly what must
    still be redacted, and it no longer decodes. Instead the value must be
    alphabet-only *and* show one of base64's tells — ``=`` padding, a length
    that is a multiple of four, or sheer size. That keeps short punctuation-free
    text such as ``README`` or ``line1\\nline2`` intact.

    :param value: Candidate payload, whitespace tolerated.
    :returns: True when the value reads as base64 rather than text.
    """
    compact = "".join(value.split())
    if not compact or not _BASE64_ALPHABET_ONLY.fullmatch(compact):
        return False
    return "=" in compact or len(compact) % 4 == 0 or len(compact) >= _MIN_UNTELLED_BLOB_CHARS


def _redact_data_field(block: dict[str, object], marker: Callable[[str, int], str]) -> None:
    """Replace a block's bare base64 ``data`` field.

    Only an actual base64 blob is replaced: a ``document`` or ``file`` record
    may legitimately carry prose in ``data``, and rewriting that would corrupt
    the history rather than shrink it.

    Mutates *block*, which callers only ever pass as a freshly built copy.

    :param block: A content block or its ``source`` object.
    :param marker: Builds replacement text from media type and payload length.
    """
    data = block.get("data")
    if not isinstance(data, str) or not data or not _is_base64_payload(data):
        return
    media_type = block.get("media_type")
    block["data"] = marker(media_type if isinstance(media_type, str) else "", len(data))


def redact_binary_payloads(
    value: _Value,
    marker: Callable[[str, int], str],
) -> _Value:
    """Recursively replace base64 payloads in structured content blocks.

    Complements :func:`redact_inline_data_uris`, which only matches payloads
    written as ``data:`` URIs: an Anthropic-shaped block carries bare base64
    under ``source.data``, which the URI regex never sees. Nothing is mutated
    in place, and every field other than the payload survives.

    A ``source`` is skipped only when its ``type`` explicitly names content
    rather than a payload (``text``, ``url``, ``content``, ``file``) — a
    ``document`` may carry its text under ``source.type: "text"``. A typeless
    source is still treated as binary.

    :param value: Arbitrarily nested dict/list/string content.
    :param marker: Builds replacement text from media type and payload length.
    :returns: A copy with base64 payloads redacted.
    """
    if isinstance(value, str):
        return redact_inline_data_uris(value, marker)
    if isinstance(value, list):
        return cast(_Value, [redact_binary_payloads(item, marker) for item in value])
    if isinstance(value, dict):
        block: dict[str, object] = dict(value)
        # Drop the bare payload before recursing, or the URI regex scans a
        # multi-megabyte base64 string that is overwritten here anyway.
        block_type = block.get("type")
        if isinstance(block_type, str) and block_type in _BINARY_BLOCK_TYPES:
            _redact_data_field(block, marker)
            source = block.get("source")
            if isinstance(source, dict):
                source = dict(source)
                source_type = source.get("type")
                if not (isinstance(source_type, str) and source_type in _NON_BINARY_SOURCE_TYPES):
                    _redact_data_field(source, marker)
                block["source"] = source
        return cast(
            _Value,
            {key: redact_binary_payloads(item, marker) for key, item in block.items()},
        )
    return value
