"""Image transport and shared native-harness MCP result conversion.

A versioned envelope preserves images on the string-only dispatch wire.
Native MCP consumers share the conversion here, regardless of harness.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import warnings
from dataclasses import dataclass

from omnigent.util.json_types import JsonObject

_ENVELOPE_KEY = "__omnigent_mcp_image_result__"
_ENVELOPE_VERSION = 1

#: Magic-byte prefixes for the image types Claude accepts, keyed by the media
#: type the caller declares. A payload must match its own declared type —
#: Claude is told that type, so a mismatch fails the resume.
_IMAGE_MAGIC_BY_MEDIA_TYPE: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),  # plus b"WEBP" at offset 8, checked below
}

#: Floor below which a payload cannot be a real image of any accepted type.
#: Measured from fixtures, not guessed: the smallest real image here is a
#: 37-byte 1x1 GIF, while the largest non-image shape that still matches a
#: magic prefix is a bare 12-byte ``RIFF``+``WEBP`` header.
_MIN_IMAGE_BYTES = 16


def canonical_image_payload(data: str, media_type: str) -> str | None:
    """
    Return *data* as canonical base64 when it is plausibly a *media_type* image.

    Producers legitimately emit line-wrapped or unpadded base64, which strict
    decoding rejects, so ASCII whitespace is dropped and ``=`` padding restored
    before validating. The canonical form is returned rather than the original
    because a provider may reject the noncanonical spelling.

    Validation is a declared-type magic-byte match. An invalid image block makes
    Claude reject the resume on every launch, so the bytes must match the type we
    declare to it. Container validation is unnecessary: a store-truncated payload
    never parses as JSON and so never reaches here, which means a magic-valid but
    internally corrupt payload is accepted by design.

    :param data: Candidate base64 payload, possibly wrapped or unpadded.
    :param media_type: Declared MIME type, e.g. ``"image/png"``.
    :returns: Canonical base64, or ``None`` when the payload is unusable.
    """
    magic = _IMAGE_MAGIC_BY_MEDIA_TYPE.get(media_type)
    if magic is None:
        return None
    compact = "".join(data.split())
    if not compact:
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) < _MIN_IMAGE_BYTES or not decoded.startswith(magic):
        return None
    if media_type == "image/webp" and decoded[8:12] != b"WEBP":
        return None
    return padded


def native_image_payload(data: str, media_type: str) -> str | None:
    """Validate an image container before sending it to a live vision client."""
    canonical = canonical_image_payload(data, media_type)
    if canonical is None:
        return None
    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(base64.b64decode(canonical))) as image:
                image.verify()
            with Image.open(io.BytesIO(base64.b64decode(canonical))) as image:
                image.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None
    return canonical


@dataclass(frozen=True)
class McpImageResult:
    """Ordered MCP text/image blocks and the upstream tool error flag."""

    content: list[JsonObject]
    is_error: bool

    def to_mcp(self) -> JsonObject:
        """Return the native MCP result consumed by subscription clients."""
        return {"content": self.native_content(), "isError": self.is_error}

    def native_content(self) -> list[JsonObject]:
        """Canonicalize supported image bytes; retain other images as legacy text."""
        blocks: list[JsonObject] = []
        for block in self.content:
            if block["type"] == "image":
                canonical = native_image_payload(str(block["data"]), str(block["mimeType"]))
                blocks.append(
                    {**block, "data": canonical}
                    if canonical is not None
                    else {"type": "text", "text": json.dumps(block)}
                )
            else:
                blocks.append(block)
        return blocks


def encode_mcp_image_result(content: list[JsonObject], *, is_error: bool) -> str:
    """Encode native image blocks without changing the dispatch wire type.

    :param content: Ordered image blocks and text fallbacks for other modalities.
    :param is_error: The upstream MCP ``isError`` flag.
    :returns: An explicitly tagged JSON string for supported terminal adapters.
    """
    return json.dumps({_ENVELOPE_KEY: _ENVELOPE_VERSION, "isError": is_error, "content": content})


def decode_mcp_image_result(value: object) -> McpImageResult | None:
    """Recognize only the exact versioned transport envelope.

    :param value: Parsed tool result, after policy evaluation and dispatch.
    :returns: Validated text/image blocks, or ``None`` for ordinary tool data.
    """
    if not isinstance(value, dict) or set(value) != {_ENVELOPE_KEY, "content", "isError"}:
        return None
    version = value[_ENVELOPE_KEY]
    if type(version) is not int or version != _ENVELOPE_VERSION:
        return None
    is_error = value["isError"]
    content = value["content"]
    if not isinstance(is_error, bool) or not isinstance(content, list):
        return None
    blocks: list[JsonObject] = []
    found_image = False
    for block in content:
        if not isinstance(block, dict) or not all(isinstance(key, str) for key in block):
            return None
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                return None
        elif block.get("type") == "image":
            if not isinstance(block.get("data"), str) or not block["data"]:
                return None
            mime_type = block.get("mimeType")
            if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
                return None
            found_image = True
        else:
            return None
        blocks.append(dict(block))
    return McpImageResult(blocks, is_error) if found_image else None


def mcp_response_from_tool_result(result: object) -> JsonObject:
    """
    Convert a shared native-harness tool result into MCP response shape.

    :param result: Result returned by ``_tool_executor``. Existing
        harnesses usually return a dict, e.g. ``{"result": "ok"}``.
    :returns: MCP tool-call response.
    """
    image_result = decode_mcp_image_result(result)
    if image_result is not None:
        return image_result.to_mcp()
    payload = result if isinstance(result, dict) else {"result": result}
    response: JsonObject = {
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }
    if payload.get("blocked") is True or ("error" in payload and payload.get("error")):
        response["isError"] = True
    return response
