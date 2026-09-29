"""Canonical cap for a ``function_call_output``'s ``output``, applied by every
producer so a multi-MB tool result can't become one giant SSE frame."""

import json

from omnigent.runtime.mcp_tool_result import decode_mcp_image_result, encode_mcp_image_result
from omnigent.runtime.tool_result_replay import image_omitted_placeholder
from omnigent.util.json_types import JsonObject

# 1 MiB: bounds AP's streamed/persisted mirror; the model's own view stays full.
MAX_TOOL_OUTPUT_BYTES = 1024 * 1024


def cap_tool_output(output: str) -> str:
    """Cap *output* to ``MAX_TOOL_OUTPUT_BYTES`` UTF-8 on a char boundary for AP's mirror."""
    encoded = output.encode("utf-8")
    if len(encoded) <= MAX_TOOL_OUTPUT_BYTES:
        return output
    output = _cap_image_envelope(output)
    encoded = output.encode("utf-8")
    if len(encoded) <= MAX_TOOL_OUTPUT_BYTES:
        return output
    omitted = len(encoded) - MAX_TOOL_OUTPUT_BYTES
    # Drop a partial trailing multibyte char left by slicing on a byte boundary.
    kept = encoded[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return f"{kept}\n\n[output truncated by omnigent: {omitted} of {len(encoded)} bytes omitted]"


def _cap_image_envelope(output: str) -> str:
    """Omit large history images before truncating text that follows them."""
    try:
        result = decode_mcp_image_result(json.loads(output))
    except (ValueError, TypeError):
        return output
    if result is None:
        return output
    content: list[JsonObject] = list(result.content)
    image_indices = sorted(
        (index for index, block in enumerate(content) if block["type"] == "image"),
        key=lambda index: len(json.dumps(content[index]).encode("utf-8")),
        reverse=True,
    )
    for remaining, index in enumerate(image_indices):
        block = content[index]
        content[index] = {
            "type": "text",
            "text": image_omitted_placeholder(str(block["mimeType"])),
        }
        if remaining + 1 < len(image_indices):
            output = encode_mcp_image_result(content, is_error=result.is_error)
        else:
            if result.is_error:
                content = [{"type": "text", "text": "Error:"}, *content]
            output = json.dumps(content)
        if len(output.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES:
            return output
    return output
