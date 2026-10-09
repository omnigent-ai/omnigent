"""Tests for the native-CLI relay image clamp (set5think fork custom patch).

Covers: oversized image downscaled under the per-image cap, surplus images in a
single result evicted to placeholders, small images and non-image content left
untouched, and malformed input returned unchanged (never raises).
"""

from __future__ import annotations

import base64
import io

import pytest

from omnigent.runner.relay_image_clamp import (
    _MAX_IMAGE_BASE64_BYTES,
    _MAX_IMAGES_PER_RESULT,
    clamp_relay_result_images,
)


def _big_png_base64(width: int = 3000, height: int = 3000) -> str:
    """Build a PNG whose base64 comfortably exceeds the per-image cap."""
    from PIL import Image

    # Random-ish noise defeats PNG compression so the payload is genuinely large.
    import os

    img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _small_png_base64() -> str:
    from PIL import Image

    img = Image.new("RGB", (16, 16), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_oversized_anthropic_image_is_downscaled_under_cap():
    big = _big_png_base64()
    assert len(big) > _MAX_IMAGE_BASE64_BYTES  # precondition
    result = {
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big}}
        ]
    }
    out = clamp_relay_result_images(result)
    block = out["content"][0]
    # Still an image (not evicted), now JPEG and under the cap.
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    assert len(block["source"]["data"]) <= _MAX_IMAGE_BASE64_BYTES
    # And it still decodes to a valid image.
    from PIL import Image

    Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))).verify()


def test_oversized_mcp_shape_image_is_downscaled():
    big = _big_png_base64()
    result = {"content": [{"type": "image", "data": big, "mimeType": "image/png"}]}
    out = clamp_relay_result_images(result)
    block = out["content"][0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/jpeg"
    assert len(block["data"]) <= _MAX_IMAGE_BASE64_BYTES


def test_small_image_passes_through_untouched():
    small = _small_png_base64()
    result = {
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": small}}
        ]
    }
    out = clamp_relay_result_images(result)
    assert out["content"][0]["source"]["data"] == small
    assert out["content"][0]["source"]["media_type"] == "image/png"


def test_text_blocks_untouched():
    result = {"content": [{"type": "text", "text": "hello"}]}
    out = clamp_relay_result_images(result)
    assert out == result


def test_surplus_images_evicted_to_placeholders():
    small = _small_png_base64()

    def img():
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": small}}

    n = _MAX_IMAGES_PER_RESULT + 2
    result = {"content": [img() for _ in range(n)]}
    out = clamp_relay_result_images(result)
    images = [b for b in out["content"] if isinstance(b, dict) and b.get("type") == "image"]
    placeholders = [
        b
        for b in out["content"]
        if isinstance(b, dict) and b.get("type") == "text" and "omitted from history" in b.get("text", "")
    ]
    assert len(images) == _MAX_IMAGES_PER_RESULT
    assert len(placeholders) == 2
    # The KEPT images are the most recent ones (last in the list).
    assert out["content"][-1]["type"] == "image"


@pytest.mark.parametrize("bad", [None, 42, "string", {"content": "not-a-list"}, {}, {"content": []}])
def test_malformed_input_returned_unchanged(bad):
    assert clamp_relay_result_images(bad) == bad
