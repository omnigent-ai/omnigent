"""Map grok ACP ``session/update`` notifications to Omnigent mirror items.

Pure mapping layer for the grok_native forwarder: each ACP
``session/update`` notification's ``update`` payload is translated into the
``external_conversation_item`` kinds the Sessions API accepts (``message`` /
``reasoning`` / ``function_call`` / ``function_call_output``), mirroring the
schema the commandcode-native forwarder posts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MirroredItem:
    """One item extracted from an ACP update, ready to POST."""

    kind: str  # message | reasoning | function_call | function_call_output
    role: str = ""
    text: str = ""
    response_id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


def session_update_to_items(params: dict[str, Any]) -> list[MirroredItem]:
    """Translate one ``session/update`` notification payload into items."""
    update = params.get("update") or {}
    if not isinstance(update, dict):
        return []
    kind = update.get("sessionUpdate")
    out: list[MirroredItem] = []

    if kind == "agent_message_chunk":
        text = _content_text(update.get("content"))
        if text:
            out.append(MirroredItem(kind="message", role="assistant", text=text))
    elif kind == "agent_thought_chunk":
        text = _content_text(update.get("content"))
        if text:
            out.append(MirroredItem(kind="reasoning", role="assistant", text=text))
    elif kind == "user_message_chunk":
        text = _content_text(update.get("content"))
        if text:
            out.append(MirroredItem(kind="message", role="user", text=text))
    elif kind == "tool_call":
        call_id = str(update.get("toolCallId") or "")
        title = update.get("title") or update.get("kind") or ""
        args = update.get("rawInput")
        out.append(
            MirroredItem(
                kind="function_call",
                call_id=call_id,
                name=str(title),
                arguments=json.dumps(args if args is not None else {}, ensure_ascii=False),
            )
        )
    elif kind == "tool_call_update":
        call_id = str(update.get("toolCallId") or "")
        text = _tool_output_text(update.get("content"))
        if text:
            out.append(MirroredItem(kind="function_call_output", call_id=call_id, text=text))
        status = update.get("status")
        if status in ("completed", "failed") and not text:
            out.append(
                MirroredItem(kind="function_call_output", call_id=call_id, text=f"[{status}]")
            )
    return out


def _content_text(content: Any) -> str:
    """Extract text from an ACP content block (or block list)."""
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        return "".join(
            str(b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _tool_output_text(content: Any) -> str:
    """Extract output text from a tool_call_update content payload."""
    if isinstance(content, str):
        return content
    text = _content_text(content)
    if text:
        return text
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "content":
                parts.append(_content_text(block.get("content")))
        return "\n\n".join(p for p in parts if p)
    return ""


__all__ = ["MirroredItem", "session_update_to_items"]
