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
                lambda match: marker(match.group(1), len(match.group(2))),
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


def _redact_data_field(block: dict[str, object], marker: Callable[[str, int], str]) -> None:
    """Replace a block's bare base64 ``data`` field.

    Mutates *block*, which callers only ever pass as a freshly built copy.

    :param block: A content block or its ``source`` object.
    :param marker: Builds replacement text from media type and payload length.
    """
    data = block.get("data")
    if not isinstance(data, str) or not data:
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
                _redact_data_field(source, marker)
                block["source"] = source
        return cast(
            _Value,
            {key: redact_binary_payloads(item, marker) for key, item in block.items()},
        )
    return value
