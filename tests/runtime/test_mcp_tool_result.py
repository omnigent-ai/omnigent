"""Native image delivery across the managed MCP subscription adapters."""

from __future__ import annotations

import json

import pytest
from claude_agent_sdk import create_sdk_mcp_server
from mcp.types import (
    AudioContent,
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    TextContent,
)

from omnigent.harnesses.claude_native.bridge import _mcp_response_from_tool_result
from omnigent.inner.claude_sdk_executor import _build_mcp_tools
from omnigent.inner.codex_executor import _dynamic_tool_result_payload
from omnigent.inner.executor import ToolCallStatus, classify_tool_result
from omnigent.runtime.harnesses._executor_adapter import _bridge_one_dispatch
from omnigent.runtime.mcp_tool_result import decode_mcp_image_result
from omnigent.runtime.tool_result_replay import tool_result_content_blocks
from omnigent.tools.mcp import _format_call_result
from tests._image_fixtures import _TINY_PNG_BASE64


def _image_output(*, is_error: bool = False) -> str:
    return _format_call_result(
        CallToolResult(
            content=[
                TextContent(type="text", text="before\nwith a newline"),
                ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png"),
                TextContent(type="text", text="after"),
            ],
            isError=is_error,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_image_result_reaches_subscription_clients_in_order(is_error: bool) -> None:
    output = _image_output(is_error=is_error)

    class Context:
        async def dispatch_tool(self, **kwargs: object) -> str:
            return output

    result = await _bridge_one_dispatch(Context(), "agent", "probe", {})
    native = _mcp_response_from_tool_result(result)
    assert native["isError"] is is_error
    assert [block["type"] for block in native["content"]] == ["text", "image", "text"]
    assert native["content"][1]["data"] == _TINY_PNG_BASE64
    assert native["content"][0]["text"] == "before\nwith a newline"
    assert native["content"][2]["text"] == "after"

    async def execute(name: str, args: dict[str, object]) -> dict[str, object]:
        return result

    tools = _build_mcp_tools([{"name": "probe", "description": "probe"}], execute)
    server = create_sdk_mcp_server(name="offline", tools=tools)["instance"]
    response = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call", params=CallToolRequestParams(name="probe", arguments={})
        )
    )
    sdk_result = response.root
    assert sdk_result.isError is is_error
    assert [block.type for block in sdk_result.content] == ["text", "image", "text"]
    assert sdk_result.content[1].data == _TINY_PNG_BASE64
    assert sdk_result.content[0].text == "before\nwith a newline"
    assert sdk_result.content[2].text == "after"

    codex = _dynamic_tool_result_payload(result)
    assert codex["success"] is not is_error
    assert codex["contentItems"] == [
        {"type": "inputText", "text": "before\nwith a newline"},
        {"type": "inputImage", "imageUrl": f"data:image/png;base64,{_TINY_PNG_BASE64}"},
        {"type": "inputText", "text": "after"},
    ]
    for raw in (result, output):
        classification = classify_tool_result(raw)
        assert classification.status == (
            ToolCallStatus.ERROR if is_error else ToolCallStatus.SUCCESS
        )
    replayed = tool_result_content_blocks(output)
    assert replayed.blocks is not None
    image = next(block for block in replayed.blocks if block["type"] == "image")
    assert image["source"]["data"] == _TINY_PNG_BASE64
    assert replayed.blocks[0]["text"] == ("Error:" if is_error else "before\nwith a newline")


@pytest.mark.parametrize(
    "changes",
    [
        {"__omnigent_mcp_image_result__": 2},
        {"__omnigent_mcp_image_result__": True},
        {"isError": "false"},
        {"content": [{"type": "text", "text": "only text"}]},
        {"content": [{"type": "image", "data": 12, "mimeType": "image/png"}]},
        {"content": [{"type": "image", "data": "data", "mimeType": "audio/wav"}]},
        {"content": [{"type": "text", "text": 12}]},
        {"blocked": True},
    ],
)
def test_invalid_envelopes_keep_the_legacy_text_path(changes: dict[str, object]) -> None:
    payload = {**json.loads(_image_output()), **changes}
    assert decode_mcp_image_result(payload) is None
    native = _mcp_response_from_tool_result(payload)
    assert native["content"] == [{"type": "text", "text": json.dumps(payload)}]
    codex = _dynamic_tool_result_payload(payload)
    assert codex["contentItems"] == [{"type": "inputText", "text": json.dumps(payload)}]
    if changes.get("blocked"):
        assert native["isError"] is True
        assert codex["success"] is False


@pytest.mark.parametrize(
    "text",
    [
        '{"type":"image","data":"literal example"}',
        "plain text",
        "[Result suppressed by policy: denied]",
    ],
)
@pytest.mark.parametrize("is_error", [False, True])
def test_text_only_mcp_results_retain_their_exact_representation(
    text: str, is_error: bool
) -> None:
    output = _format_call_result(
        CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)
    )
    assert output == ("Error: " if is_error else "") + text
    assert decode_mcp_image_result({"result": output}) is None
    native = _mcp_response_from_tool_result({"result": output})
    assert native["content"] == [{"type": "text", "text": json.dumps({"result": output})}]


def test_other_modalities_retain_the_legacy_text_fallback() -> None:
    audio = AudioContent(type="audio", data="YXVkaW8=", mimeType="audio/wav")
    audio_text = json.dumps(audio.model_dump())
    assert _format_call_result(CallToolResult(content=[audio])) == audio_text
    mixed = _format_call_result(
        CallToolResult(
            content=[
                ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png"),
                audio,
            ]
        )
    )
    native = _mcp_response_from_tool_result(json.loads(mixed))
    assert native["content"][1] == {"type": "text", "text": audio_text}


def test_image_only_error_is_classified_without_a_text_message() -> None:
    output = _format_call_result(
        CallToolResult(
            content=[ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png")],
            isError=True,
        )
    )
    assert classify_tool_result(output).status == ToolCallStatus.ERROR
    assert classify_tool_result(output).error == "MCP tool returned an error"


@pytest.mark.parametrize(
    "data,mime_type", [("not base64", "image/png"), ("PHN2Zy8+", "image/svg+xml")]
)
def test_invalid_or_unsupported_image_keeps_lossless_text(data: str, mime_type: str) -> None:
    image = ImageContent(type="image", data=data, mimeType=mime_type)
    output = _format_call_result(CallToolResult(content=[image]))
    assert output == json.dumps(image.model_dump())
    assert _mcp_response_from_tool_result(json.loads(output))["content"] == [
        {"type": "text", "text": output}
    ]
    tagged = json.loads(_image_output(is_error=True))
    tagged["content"][1].update(data=data, mimeType=mime_type)
    native = _mcp_response_from_tool_result(tagged)
    assert native["isError"] is True
    assert native["content"][1] == {"type": "text", "text": json.dumps(tagged["content"][1])}
    assert _dynamic_tool_result_payload(tagged)["contentItems"][1]["type"] == "inputText"


@pytest.mark.parametrize("is_error", [False, True])
def test_clipped_envelope_preserves_available_text_through_native_stripper(is_error: bool) -> None:
    from omnigent.runtime.tool_result_replay import strip_unparseable_image_output

    output = _image_output(is_error=is_error)
    clipped = output[: output.index(_TINY_PNG_BASE64) + 12] + "[truncated by conversation-store]"
    normalized = strip_unparseable_image_output(clipped)
    replayed = tool_result_content_blocks(normalized)
    assert replayed.blocks is not None
    rendered = json.dumps(replayed.blocks)
    assert "before" in rendered
    assert "omitted from history" in rendered
    assert _TINY_PNG_BASE64[:12] not in rendered
    assert ("Error:" in rendered) is is_error


@pytest.mark.parametrize(
    "data",
    [
        _TINY_PNG_BASE64.rstrip("="),
        "\n".join(_TINY_PNG_BASE64[i : i + 20] for i in range(0, len(_TINY_PNG_BASE64), 20)),
    ],
)
def test_wrapped_or_unpadded_image_preserves_decoded_bytes(data: str) -> None:
    import base64

    output = _format_call_result(
        CallToolResult(content=[ImageContent(type="image", data=data, mimeType="image/png")])
    )
    native = _mcp_response_from_tool_result(json.loads(output))
    assert native["content"][0]["type"] == "image"
    assert base64.b64decode(native["content"][0]["data"]) == base64.b64decode(_TINY_PNG_BASE64)


def test_mixed_invalid_image_keeps_live_bytes_and_existing_replay_safeguard() -> None:
    invalid = ImageContent(type="image", data="A" * 10000, mimeType="image/png")
    output = _format_call_result(
        CallToolResult(
            content=[
                ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png"),
                invalid,
            ]
        )
    )
    native = _mcp_response_from_tool_result(json.loads(output))
    assert native["content"][0]["type"] == "image"
    assert native["content"][1] == {"type": "text", "text": json.dumps(invalid.model_dump())}
    replayed = tool_result_content_blocks(output)
    assert replayed.blocks is not None
    assert replayed.blocks[0]["type"] == "image"
    assert "omitted from history" in replayed.blocks[1]["text"]
    assert invalid.data not in json.dumps(replayed.blocks)


def test_successful_envelope_does_not_classify_literal_error_examples() -> None:
    payload = json.loads(_image_output())
    payload["content"].append({"type": "text", "text": '{"error":"documentation example"}'})
    for value in (payload, json.dumps(payload)):
        assert classify_tool_result(value).status == ToolCallStatus.SUCCESS


def test_multiple_images_preserve_bytes_and_late_text() -> None:
    import base64

    from tests._image_fixtures import _TINY_JPEG_BASE64

    output = _format_call_result(
        CallToolResult(
            content=[
                ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png"),
                TextContent(type="text", text="between"),
                ImageContent(type="image", data=_TINY_JPEG_BASE64, mimeType="image/jpeg"),
                TextContent(type="text", text="final correction"),
            ]
        )
    )
    native = _mcp_response_from_tool_result(json.loads(output))
    assert [block["type"] for block in native["content"]] == ["image", "text", "image", "text"]
    for index, data in ((0, _TINY_PNG_BASE64), (2, _TINY_JPEG_BASE64)):
        assert base64.b64decode(native["content"][index]["data"]) == base64.b64decode(data)
    assert native["content"][1]["text"] == "between"
    assert native["content"][3]["text"] == "final correction"


def test_corrupt_container_with_image_signature_stays_lossless_text() -> None:
    import base64

    data = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"not an image container").decode()
    corrupt = ImageContent(type="image", data=data, mimeType="image/png")
    output = _format_call_result(CallToolResult(content=[corrupt]))
    assert output == json.dumps(corrupt.model_dump())
    payload = json.loads(_image_output())
    payload["content"].append(corrupt.model_dump())
    native = _mcp_response_from_tool_result(payload)
    assert native["content"][-1] == {"type": "text", "text": json.dumps(corrupt.model_dump())}
