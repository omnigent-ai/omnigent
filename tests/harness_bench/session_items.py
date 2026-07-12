"""Helpers for reading session-item wire shapes used by bench drivers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def item_data(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the nested item payload when one is present."""
    data = item.get("data")
    return data if isinstance(data, Mapping) else item


def item_type(item: Mapping[str, Any]) -> str | None:
    """Return an item's type from either supported envelope shape."""
    data = item_data(item)
    value = item.get("type") or data.get("type")
    return value if isinstance(value, str) else None


def item_role(item: Mapping[str, Any]) -> str | None:
    """Return an item's role from either supported envelope shape."""
    data = item_data(item)
    value = data.get("role") or item.get("role")
    return value if isinstance(value, str) else None


def assistant_text(
    items: Mapping[str, Any] | Iterable[Mapping[str, Any]], *, separator: str = "\n"
) -> str:
    """Concatenate assistant text blocks from one item or an item collection."""
    candidates = [items] if isinstance(items, Mapping) else items
    output: list[str] = []
    for item in candidates:
        data = item_data(item)
        if item_role(item) != "assistant":
            continue
        content = data.get("content")
        if not isinstance(content, list):
            continue
        output.extend(
            block["text"]
            for block in content
            if isinstance(block, Mapping)
            and block.get("type") in (None, "output_text", "text")
            and isinstance(block.get("text"), str)
        )
    return separator.join(text for text in output if text)


def function_calls(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return normalized function-call records from session items."""
    calls: list[dict[str, Any]] = []
    for item in items:
        if item_type(item) != "function_call":
            continue
        data = item_data(item)
        calls.append(
            {
                "call_id": data.get("call_id"),
                "name": data.get("name"),
                "arguments": data.get("arguments"),
            }
        )
    return calls


def tool_output_states(
    items: Iterable[Mapping[str, Any]], *, deny_marker: str
) -> tuple[bool, bool]:
    """Return whether tool outputs show an allowed or denied call."""
    allowed = False
    denied = False
    for item in items:
        if item_type(item) != "function_call_output":
            continue
        data = item_data(item)
        output = str(data.get("output", ""))
        if data.get("status") == "blocked" or deny_marker in output:
            denied = True
        else:
            allowed = True
    return allowed, denied


def contains_user_text(items: Iterable[Mapping[str, Any]], marker: str) -> bool:
    """Return whether a user message contains *marker*."""
    for item in items:
        data = item_data(item)
        if item_type(item) != "message" or data.get("role") != "user":
            continue
        content = data.get("content")
        if not isinstance(content, list):
            continue
        if any(
            marker in block.get("text", "")
            for block in content
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ):
            return True
    return False


__all__ = [
    "assistant_text",
    "contains_user_text",
    "function_calls",
    "item_data",
    "item_role",
    "item_type",
    "tool_output_states",
]
