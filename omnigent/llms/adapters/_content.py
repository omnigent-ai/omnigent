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
from typing import Any

_INLINE_BASE64_DATA_URI = re.compile(
    r"data:([^;,\s]*)(?:;[^;,\r\n]*)*;base64,[ \t]?([A-Za-z0-9+/=_-]+)",
    re.IGNORECASE,
)


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
    value: Any,  # type: ignore[explicit-any]
    marker: Callable[[str, int], str],
) -> Any:  # type: ignore[explicit-any]
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
        return _INLINE_BASE64_DATA_URI.sub(
            lambda match: marker(match.group(1), len(match.group(2))),
            value,
        )
    if isinstance(value, list):
        return [redact_inline_data_uris(item, marker) for item in value]
    if isinstance(value, dict):
        return {key: redact_inline_data_uris(item, marker) for key, item in value.items()}
    return value
