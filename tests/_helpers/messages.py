"""Response text helpers shared by HTTP journey tests."""

from typing import Any


def all_message_text(body: dict[str, Any]) -> str:
    """Join nonempty text blocks from message outputs, preserving their order."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)
