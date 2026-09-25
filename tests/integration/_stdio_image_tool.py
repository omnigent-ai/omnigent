"""Self-contained, credential-free stdio MCP image fixture for HTTP journeys."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

# Real one-pixel images also used in tests/_image_fixtures.py. Keep this script
# self-contained: the MCP stdio launcher deliberately filters PYTHONPATH.
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "pfZFQAAAAABJRU5ErkJggg=="
)
TINY_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR"
    "CAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
    "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
    "5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
    "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3E//Z"
)
EARLY_TEXT = "before-image-probe-8051"
BETWEEN_TEXT = "between-image-probe-8052"
LATE_TEXT = "required-final-fact-8053: East 61; verification word amber."
TRANSFORMED_TEXT = "image-result-replaced-8053"


def replace_image_result() -> Callable[[dict[str, Any]], dict[str, str] | None]:
    """Return a policy callable that replaces the image-bearing output."""

    def _replace(event: dict[str, Any]) -> dict[str, str] | None:
        if event.get("type") == "tool_result" and event.get("target") == "image_mcp__snapshot":
            return {"result": "allow", "data": TRANSFORMED_TEXT}
        return None

    return _replace


async def _serve() -> None:
    """Serve ordered text/image/text/image/text with optional MCP error state."""
    server = Server("image-transport-test")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="snapshot",
                description="Return two distinct images followed by required text.",
                inputSchema={
                    "type": "object",
                    "properties": {"error": {"type": "boolean"}},
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        if name != "snapshot":
            raise ValueError(f"unknown test tool: {name}")
        return CallToolResult(
            content=[
                TextContent(type="text", text=EARLY_TEXT),
                ImageContent(type="image", data=TINY_PNG_BASE64, mimeType="image/png"),
                TextContent(type="text", text=BETWEEN_TEXT),
                ImageContent(type="image", data=TINY_JPEG_BASE64, mimeType="image/jpeg"),
                TextContent(type="text", text=LATE_TEXT),
            ],
            isError=bool(arguments.get("error", False)),
        )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_serve())
