"""Size-based token estimates for context-window breakdowns.

Figures come from content size (see :data:`~omnigent.llms.errors.APPROX_BYTES_PER_TOKEN`),
not a tokenizer, so they are labelled estimates wherever they are shown.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from omnigent.entities.conversation import is_omitted_binary_payload
from omnigent.llms.adapters._content import redact_binary_payloads
from omnigent.llms.errors import APPROX_BYTES_PER_TOKEN
from omnigent.runtime.content_resolver import is_text_like_attachment

# Roughly one full-resolution image on current vision models; its token cost
# does not follow its encoded size.
IMAGE_TOKEN_ESTIMATE = 1_600

TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text", "text", "refusal"})
ATTACHMENT_BLOCK_TYPES = frozenset({"input_image", "input_file"})


def bytes_to_tokens(size: int) -> int:
    """
    Estimated tokens for *size* bytes of text, rounded up.

    :param size: Byte count, e.g. ``9``.
    :returns: Estimated tokens, e.g. ``3``.
    """
    return -(-size // APPROX_BYTES_PER_TOKEN)


def estimate_text_tokens(text: str) -> int:
    """
    Estimate prompt tokens for *text* from its UTF-8 size.

    :param text: Any prompt text, e.g. ``"Hello"``.
    :returns: Estimated tokens, rounded up, e.g. ``2``.
    """
    return bytes_to_tokens(len(text.encode("utf-8")))


def estimate_without_binary(value: object) -> int:
    """
    Estimate structured content, counting each embedded image as one image.

    :param value: A string or JSON-shaped value that may embed base64 payloads.
    :returns: Estimated tokens.
    """
    images = 0

    def drop_payload(media_type: str, _length: int) -> str:
        nonlocal images
        if media_type.startswith("image/"):
            images += 1
        return ""

    stripped = redact_binary_payloads(value, drop_payload)
    text = stripped if isinstance(stripped, str) else json.dumps(stripped, ensure_ascii=False)
    return estimate_text_tokens(text) + images * IMAGE_TOKEN_ESTIMATE


def estimate_attachment_tokens(
    *, is_image: bool, content_type: str | None, filename: str | None, size: int | None
) -> int | None:
    """
    Estimate an inline attachment, or ``None`` when its type has no reliable estimate.

    :param is_image: The block is an image, e.g. ``input_image``.
    :param content_type: Its MIME type, e.g. ``"text/csv"``; ``None`` if unknown.
    :param filename: Its filename, used as an extension fallback.
    :param size: Its decoded size in bytes, or ``None`` if unknown.
    :returns: Estimated tokens; ``None`` for e.g. a PDF or an unknown size.
    """
    if is_image or (content_type or "").startswith("image/"):
        return IMAGE_TOKEN_ESTIMATE
    if size is not None and is_text_like_attachment(content_type or "", filename):
        return bytes_to_tokens(size)
    return None


def inline_payload_bytes(block: Mapping[str, Any]) -> int | None:
    """
    Decoded size of a block's inline base64 payload, if it carries one.

    :param block: An ``input_image`` / ``input_file`` block.
    :returns: Decoded bytes, or ``None`` for a remote URL, a ``file_id``
        reference or a block without a payload.
    """
    for key in ("file_data", "image_url"):
        payload = block.get(key)
        if not isinstance(payload, str) or not payload or is_omitted_binary_payload(payload):
            continue
        if payload.startswith("data:"):
            payload = payload.split(",", 1)[-1]
        elif "://" in payload:
            # A remote URL carries no payload of its own.
            return None
        return len(payload.rstrip("=")) * 3 // 4
    return None
