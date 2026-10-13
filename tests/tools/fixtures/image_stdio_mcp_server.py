"""Stdio MCP server whose tool returns ordered text + PNG image + trailing text.

Deterministic fixture for image tool-result delivery e2e tests: the
``fetch_chart`` tool returns a ``TextContent`` preamble, a PNG
``ImageContent``, and an essential trailing ``TextContent`` correction,
so a consumer must receive BOTH the native image and the ordered text
to act correctly.

Usage:

    python tests/tools/fixtures/image_stdio_mcp_server.py

``FastMCP.run()`` defaults to stdio transport.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

# 8x8 solid-orange RGB PNG (74 bytes decoded). Baked as a literal so the
# fixture subprocess and the asserting test share the exact same bytes.
CHART_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mP438OAFTEM"
    "LQkA1cJiwVpEJ8UAAAAASUVORK5CYII="
)

LEADING_TEXT = "Chart rendered below; read the image before the correction."
TRAILING_TEXT = (
    "CORRECTION: the verified reading is TANGERINE-PRIME; "
    "any value visible inside the image is stale."
)

mcp = FastMCP("chart-test")


@mcp.tool()
def fetch_chart():
    """Return the chart as ordered text, PNG image, and a trailing correction."""
    return [
        TextContent(type="text", text=LEADING_TEXT),
        ImageContent(type="image", data=CHART_PNG_BASE64, mimeType="image/png"),
        TextContent(type="text", text=TRAILING_TEXT),
    ]


if __name__ == "__main__":
    mcp.run()
