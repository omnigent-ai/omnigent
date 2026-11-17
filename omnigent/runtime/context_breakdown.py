"""Size-based token estimates shared by the runner's per-turn breakdown and the server's.

Figures come from content size (see :data:`~omnigent.llms.errors.APPROX_BYTES_PER_TOKEN`),
not a tokenizer, so they are labelled estimates wherever they are shown.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.entities.context_breakdown import TurnContextBreakdown
from omnigent.entities.conversation import is_omitted_binary_payload
from omnigent.inner.native_attachments import attachment_is_inlined
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


def _data_uri_media_type(block: Mapping[str, Any]) -> str | None:
    """MIME type of a block's ``data:`` URI payload, e.g. ``"text/plain"``."""
    for key in ("file_data", "image_url"):
        payload = block.get(key)
        if isinstance(payload, str) and payload.startswith("data:"):
            return payload[5:].split(";", 1)[0].split(",", 1)[0] or None
    return None


class _ContentTally:
    """Running totals for one category's text and the attachments it carries."""

    def __init__(self) -> None:
        self.tokens = 0
        self.attachment_tokens = 0
        self.attachment_count = 0
        self.unestimated_attachment_count = 0

    def add(self, content: object) -> None:
        """Count a message's content: plain text, content blocks or a tool payload."""
        if isinstance(content, str):
            self.tokens += estimate_without_binary(content)
            return
        if not isinstance(content, list):
            self.tokens += estimate_without_binary(content) if content else 0
            return
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type in TEXT_BLOCK_TYPES:
                text = block.get("text")
                if isinstance(text, str):
                    self.tokens += estimate_text_tokens(text)
            elif block_type in ATTACHMENT_BLOCK_TYPES:
                self._add_attachment(block)
            else:
                self.tokens += estimate_without_binary(dict(block))

    def _add_attachment(self, block: Mapping[str, Any]) -> None:
        filename = block.get("filename")
        name = filename if isinstance(filename, str) else None
        size = inline_payload_bytes(block)
        if size is None and not attachment_is_inlined(name, block):
            # A workspace file reaches the model only as its path; bytes the
            # block carries were inlined whatever its name.
            return
        self.attachment_count += 1
        estimate = estimate_attachment_tokens(
            is_image=block.get("type") == "input_image",
            content_type=_data_uri_media_type(block),
            filename=name,
            size=size,
        )
        if estimate is None:
            self.unestimated_attachment_count += 1
        else:
            self.attachment_tokens += estimate


def build_turn_context_breakdown(
    *,
    system_prompt: str,
    tools: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
) -> TurnContextBreakdown:
    """
    Estimate what one turn's request to the harness carries.

    The last message is the current one when it is the user's; everything
    before it is history. ``system``-role messages count as instructions.

    :param system_prompt: The composed instructions sent with the turn.
    :param tools: The tool schemas listed for the turn.
    :param messages: The turn's messages, oldest first, e.g.
        ``[{"role": "user", "content": "Hi"}]``.
    :returns: The per-category breakdown.
    """
    system = _ContentTally()
    history = _ContentTally()
    current = _ContentTally()
    if system_prompt:
        system.add(system_prompt)
    history_count = 0
    for index, message in enumerate(messages):
        content = message.get("content")
        role = message.get("role")
        if role == "system":
            system.add(content)
        elif index == len(messages) - 1 and role == "user":
            current.add(content)
        else:
            history.add(content)
            history_count += 1
    tallies = (system, history, current)
    return TurnContextBreakdown(
        system_tokens=system.tokens,
        tool_tokens=(
            estimate_text_tokens(json.dumps(list(tools), ensure_ascii=False)) if tools else 0
        ),
        tool_count=len(tools),
        history_tokens=history.tokens,
        history_item_count=history_count,
        attachment_tokens=sum(tally.attachment_tokens for tally in tallies),
        attachment_count=sum(tally.attachment_count for tally in tallies),
        unestimated_attachment_count=sum(tally.unestimated_attachment_count for tally in tallies),
        message_tokens=current.tokens,
    )
