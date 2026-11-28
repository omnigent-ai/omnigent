"""Public native-harness MCP result conversion boundaries."""

from __future__ import annotations

import base64
import json

import pytest

from omnigent.harnesses.claude_native.bridge import _mcp_response_from_tool_result
from omnigent.runtime.mcp_tool_result import (
    encode_mcp_image_result,
    mcp_response_from_tool_result,
)
from tests._image_fixtures import _TINY_JPEG_BASE64, _TINY_PNG_BASE64


def test_claude_compatibility_wrapper_uses_the_shared_converter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runtime import mcp_tool_result

    result = {"result": "ok"}
    response = {"content": [{"type": "text", "text": "shared converter"}]}
    calls: list[object] = []

    def convert(value: object) -> dict:
        calls.append(value)
        return response

    monkeypatch.setattr(mcp_tool_result, "mcp_response_from_tool_result", convert)

    assert _mcp_response_from_tool_result(result) is response
    assert calls == [result]


@pytest.mark.parametrize("is_error", [False, True])
def test_shared_converter_preserves_multiple_images_and_essential_trailing_text(
    is_error: bool,
) -> None:
    trailing = "Required final correction: East 61; verification word amber."
    encoded = encode_mcp_image_result(
        [
            {"type": "text", "text": "before"},
            {"type": "image", "data": _TINY_PNG_BASE64, "mimeType": "image/png"},
            {"type": "text", "text": "between"},
            {"type": "image", "data": _TINY_JPEG_BASE64, "mimeType": "image/jpeg"},
            {"type": "text", "text": trailing},
        ],
        is_error=is_error,
    )

    response = mcp_response_from_tool_result(json.loads(encoded))

    assert response["isError"] is is_error
    assert [block["type"] for block in response["content"]] == [
        "text",
        "image",
        "text",
        "image",
        "text",
    ]
    assert base64.b64decode(response["content"][1]["data"]) == base64.b64decode(_TINY_PNG_BASE64)
    assert base64.b64decode(response["content"][3]["data"]) == base64.b64decode(_TINY_JPEG_BASE64)
    assert response["content"][-1] == {"type": "text", "text": trailing}


def test_shared_converter_keeps_unsupported_image_as_lossless_text() -> None:
    unsupported = {"type": "image", "data": "PHN2Zy8+", "mimeType": "image/svg+xml"}
    encoded = encode_mcp_image_result(
        [
            {"type": "image", "data": _TINY_PNG_BASE64, "mimeType": "image/png"},
            unsupported,
            {"type": "text", "text": "Essential text after unsupported image."},
        ],
        is_error=True,
    )

    response = mcp_response_from_tool_result(json.loads(encoded))

    assert response["isError"] is True
    assert response["content"][0]["type"] == "image"
    assert response["content"][1] == {"type": "text", "text": json.dumps(unsupported)}
    assert response["content"][2]["text"] == "Essential text after unsupported image."


@pytest.mark.parametrize(
    ("result", "is_error"),
    [
        ("plain text", False),
        ({"result": "ok"}, False),
        ({"error": "boom"}, True),
        ({"blocked": True, "reason": "policy"}, True),
    ],
)
def test_shared_converter_retains_ordinary_result_semantics(
    result: object,
    is_error: bool,
) -> None:
    payload = result if isinstance(result, dict) else {"result": result}

    response = mcp_response_from_tool_result(result)

    assert response["content"] == [{"type": "text", "text": json.dumps(payload)}]
    assert response.get("isError", False) is is_error
