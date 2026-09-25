"""Image container validation and partially redacted managed MCP histories."""

import base64
import json

import pytest
from PIL import Image

from omnigent.llms.adapters._content import redact_binary_payloads
from omnigent.runtime.mcp_tool_result import (
    decode_mcp_image_result,
    encode_mcp_image_result,
    native_image_payload,
)
from omnigent.runtime.tool_result_replay import tool_result_content_blocks
from tests._image_fixtures import (
    _TINY_GIF_BASE64,
    _TINY_JPEG_BASE64,
    _TINY_PNG_BASE64,
    _TINY_WEBP_BASE64,
)


def _envelope(data: str) -> str:
    return encode_mcp_image_result(
        [
            {"type": "text", "text": "before"},
            {"type": "image", "data": _TINY_PNG_BASE64, "mimeType": "image/png"},
            {"type": "image", "data": data, "mimeType": "image/png"},
            {"type": "text", "text": "Required trailing fact: blue."},
        ],
        is_error=True,
    )


@pytest.mark.parametrize(
    "data",
    [
        "[image/png content omitted from conversation history]",
        base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"not an image container").decode(),
        "not base64",
    ],
)
def test_replay_keeps_valid_image_when_another_image_is_unavailable(data: str) -> None:
    replay = tool_result_content_blocks(_envelope(data))
    assert replay.blocks is not None
    assert [block["type"] for block in replay.blocks] == ["text", "text", "image", "text", "text"]
    assert replay.blocks[0] == {"type": "text", "text": "Error:"}
    assert replay.blocks[1] == {"type": "text", "text": "before"}
    assert replay.blocks[2]["source"]["data"] == _TINY_PNG_BASE64
    assert json.loads(replay.blocks[3]["text"])["data"] == data
    assert replay.blocks[4] == {"type": "text", "text": "Required trailing fact: blue."}


def test_binary_redaction_of_envelope_keeps_text_and_never_emits_invalid_images() -> None:
    marker = "[image/png content omitted from conversation history]"
    redacted = redact_binary_payloads(json.loads(_envelope(_TINY_PNG_BASE64)), lambda *_: marker)
    output = json.dumps(redacted)
    assert _TINY_PNG_BASE64 not in output
    replay = tool_result_content_blocks(output)
    assert replay.blocks is not None
    assert all(block["type"] == "text" for block in replay.blocks)
    assert replay.blocks[1]["text"] == "before"
    assert replay.blocks[-1]["text"] == "Required trailing fact: blue."
    assert marker in json.dumps(replay.blocks)
    decoded = decode_mcp_image_result(redacted)
    assert decoded is not None
    assert all(block["type"] == "text" for block in decoded.native_content())


@pytest.mark.parametrize(
    "data,media_type",
    [
        (_TINY_PNG_BASE64, "image/png"),
        (_TINY_JPEG_BASE64, "image/jpeg"),
        (_TINY_GIF_BASE64, "image/gif"),
        (_TINY_WEBP_BASE64, "image/webp"),
    ],
)
def test_container_validation_preserves_supported_image_bytes(data: str, media_type: str) -> None:
    assert native_image_payload(data, media_type) == data


def test_native_validation_rejects_decompression_warning_without_allocating_large_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 0.5)
    assert native_image_payload(_TINY_PNG_BASE64, "image/png") is None


def test_native_validation_rejects_incomplete_container_and_declared_type_mismatch() -> None:
    incomplete = base64.b64encode(base64.b64decode(_TINY_PNG_BASE64)[:-12]).decode()
    assert native_image_payload(incomplete, "image/png") is None
    assert native_image_payload(_TINY_PNG_BASE64, "image/jpeg") is None
