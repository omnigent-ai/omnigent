"""Clamp inline image payloads on the native-CLI tool-result relay path.

CUSTOM PATCH (set5think fork) — see the ``omnigent-fork`` skill's customization
manifest. Not present upstream as of base tag v0.15.0.

Why this exists
---------------
Native-CLI harnesses (Kiro and siblings) call Omnigent MCP tools through the
``serve-mcp`` relay. The relay returns tool results — including full-resolution
base64 image blocks from tools like ``browser_screenshot`` or headless
screenshots — verbatim to the CLI. The CLI (kiro-cli) stores those images in
its OWN conversation history (``data.sqlite3``) and replays the entire image
history to its backend (CodeWhisperer ``GenerateAssistantResponse``) on every
turn. That backend enforces a per-image cap (5 MB base64) and an aggregate
request-body limit. Once an oversized image — or too many images in aggregate —
is in history, every subsequent turn fails with ``ValidationException``
("image exceeds 5 MB maximum" → later the generic "Improperly formed request" /
``REQUEST_BODY_INVALID``), and the session can't self-heal: the bad image stays
in retained history and compaction itself needs a successful backend call.

Omnigent does not control kiro-cli's retained history, so the lever we DO have
is the only reliable fix: make every image we hand the CLI small enough that
even many of them stay under the backend's limits. This module:

* **Downscales/recompresses** any image block whose base64 exceeds a safe cap
  (``_MAX_IMAGE_BASE64_BYTES``, below the backend's 5 MB) until it fits —
  preserving a usable (smaller) image for vision.
* **Placeholder-evicts** all but the most recent ``_MAX_IMAGES_PER_RESULT``
  image blocks within a single tool result, so a single call returning many
  images can't itself blow the body.

It walks both the Anthropic-style ``{"type":"image","source":{"type":"base64",
...}}`` shape and the MCP ``{"type":"image","data":...,"mimeType":...}`` shape.
Non-image content passes through unchanged.
"""

from __future__ import annotations

import base64
import binascii
import io
from typing import Any

# Backend hard cap is 5 MB (5_242_880) of base64 per image; stay well under so
# the base64 expansion and JSON framing have headroom.
_MAX_IMAGE_BASE64_BYTES = 4_000_000
# Keep at most this many images per single tool result; older ones in the same
# result become text placeholders. One screenshot per call is typical, so this
# only bites pathological multi-image results.
_MAX_IMAGES_PER_RESULT = 2
# Downscale search floor: never shrink the longest edge below this (keeps the
# image legible rather than producing a useless thumbnail).
_MIN_LONGEST_EDGE = 480


def _placeholder_block(media_type: str | None) -> dict[str, Any]:
    """Return the text block that replaces an evicted/undecodable image."""
    label = f"{media_type} image" if media_type else "image"
    return {
        "type": "text",
        "text": (
            f"[{label} omitted from history to keep the request under the "
            "backend image-size limit — re-run the tool call above to view it again]"
        ),
    }


def _image_source_fields(block: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """Extract (base64_data, media_type, shape) from an image block.

    ``shape`` is ``"anthropic"`` (data under ``source``), ``"mcp"`` (data at the
    top level), or ``""`` when the block is not a recognizable base64 image.
    """
    if block.get("type") != "image":
        return None, None, ""
    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data")
        if isinstance(data, str) and data:
            media_type = source.get("media_type")
            return data, media_type if isinstance(media_type, str) else None, "anthropic"
    data = block.get("data")
    if isinstance(data, str) and data:
        media_type = block.get("mimeType") or block.get("media_type")
        return data, media_type if isinstance(media_type, str) else None, "mcp"
    return None, None, ""


def _downscale_base64(data_b64: str, media_type: str | None) -> str | None:
    """Return a smaller base64 image under the cap, or ``None`` if infeasible.

    Iteratively reduces the longest edge (and, as a fallback, JPEG quality)
    until the re-encoded base64 fits ``_MAX_IMAGE_BASE64_BYTES``. Returns
    ``None`` when Pillow is unavailable, the data won't decode, or no size
    within the floor fits (caller then placeholder-evicts).
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow missing → caller evicts instead
        return None
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:  # noqa: BLE001 — undecodable image → caller evicts
        return None

    # PNG screenshots dominate; converting to JPEG shrinks dramatically. Keep
    # alpha-less output; flatten RGBA/P onto white so JPEG is valid.
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        try:
            mask = img.convert("RGBA").split()[-1]
            background.paste(img.convert("RGB"), mask=mask)
        except Exception:  # noqa: BLE001
            background = img.convert("RGB")
        img = background

    longest = max(img.size)
    quality = 85
    # Try progressively smaller dimensions; within each, drop quality a bit.
    while True:
        scale = min(1.0, longest / max(img.size)) if max(img.size) else 1.0
        if scale < 1.0:
            new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            candidate = img.resize(new_size, Image.LANCZOS)
        else:
            candidate = img
        buf = io.BytesIO()
        candidate.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        if len(encoded) <= _MAX_IMAGE_BASE64_BYTES:
            return encoded
        if quality > 55:
            quality -= 15
            continue
        quality = 85
        longest = int(longest * 0.8)
        if longest < _MIN_LONGEST_EDGE:
            return None


def _rewrite_image_block(block: dict[str, Any]) -> dict[str, Any]:
    """Downscale an oversized image block in place-ish; evict if infeasible."""
    data_b64, media_type, shape = _image_source_fields(block)
    if not data_b64 or not shape:
        return block
    if len(data_b64) <= _MAX_IMAGE_BASE64_BYTES:
        return block
    shrunk = _downscale_base64(data_b64, media_type)
    if shrunk is None:
        return _placeholder_block(media_type)
    if shape == "anthropic":
        new_source = dict(block.get("source", {}))
        new_source["data"] = shrunk
        new_source["media_type"] = "image/jpeg"
        return {**block, "source": new_source}
    # mcp shape
    return {**block, "data": shrunk, "mimeType": "image/jpeg"}


def _clamp_content_list(content: list[Any]) -> list[Any]:
    """Clamp a list of MCP content blocks: downscale, then evict older images."""
    # First pass: downscale oversized images (and evict undecodable ones).
    processed: list[Any] = []
    image_positions: list[int] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image":
            rewritten = _rewrite_image_block(item)
            processed.append(rewritten)
            if isinstance(rewritten, dict) and rewritten.get("type") == "image":
                image_positions.append(len(processed) - 1)
        else:
            processed.append(item)
    # Second pass: keep only the most recent _MAX_IMAGES_PER_RESULT images.
    if len(image_positions) > _MAX_IMAGES_PER_RESULT:
        evict = set(image_positions[: len(image_positions) - _MAX_IMAGES_PER_RESULT])
        for idx in evict:
            block = processed[idx]
            media_type = None
            if isinstance(block, dict):
                _d, media_type, _s = _image_source_fields(block)
            processed[idx] = _placeholder_block(media_type)
    return processed


def clamp_relay_result_images(result: Any) -> Any:
    """Clamp image payloads in a native-CLI relay tool result.

    Accepts the parsed MCP tool-call result object (typically
    ``{"content": [...]}``) and returns the same structure with oversized
    images downscaled under the per-image cap and surplus images in a single
    result evicted to text placeholders. Any non-dict / content-less input is
    returned unchanged. Never raises — on any unexpected shape it returns the
    input as-is so a transform bug can't break tool delivery.
    """
    try:
        if not isinstance(result, dict):
            return result
        content = result.get("content")
        if isinstance(content, list):
            return {**result, "content": _clamp_content_list(content)}
        return result
    except Exception:  # noqa: BLE001 — never let clamping break tool delivery
        return result
