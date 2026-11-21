"""Map cline hub events to Omnigent mirror items.

Pure mapping layer for the cline_native forwarder: each hub ``event`` envelope
is translated into the ``external_conversation_item`` kinds the Sessions API
accepts (``message`` / ``reasoning`` / ``function_call`` /
``function_call_output``). Event payloads come from the hub's session event
projector (``assistant.delta``/``assistant.finished``, ``reasoning.delta``/
``reasoning.finished``, ``tool.started``/``tool.finished``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MirroredItem:
    """One item extracted from a hub event, ready to POST."""

    kind: str  # message | reasoning | function_call | function_call_output
    role: str = ""
    text: str = ""
    response_id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


def hub_event_to_items(envelope: dict[str, Any]) -> list[MirroredItem]:
    """Translate one hub ``event`` envelope into items (possibly none)."""
    event = envelope.get("event")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return []
    out: list[MirroredItem] = []

    if event == "assistant.finished":
        text = str(payload.get("text") or "")
        if text:
            out.append(MirroredItem(kind="message", role="assistant", text=text))
    elif event == "reasoning.finished":
        text = str(payload.get("reasoning") or "")
        if text:
            out.append(MirroredItem(kind="reasoning", role="assistant", text=text))
    elif event == "tool.started":
        call_id = str(payload.get("toolCallId") or "")
        name = str(payload.get("toolName") or "")
        args = payload.get("input")
        out.append(
            MirroredItem(
                kind="function_call",
                call_id=call_id,
                name=name,
                arguments=json.dumps(args if args is not None else {}, ensure_ascii=False),
            )
        )
    elif event == "tool.finished":
        call_id = str(payload.get("toolCallId") or "")
        text = _tool_output_text(payload.get("output"), payload.get("error"))
        if text:
            out.append(MirroredItem(kind="function_call_output", call_id=call_id, text=text))
    return out


def _tool_output_text(output: Any, error: Any) -> str:
    """Flatten a ``tool.finished`` output/error into display text."""
    if error:
        return str(error if isinstance(error, str) else json.dumps(error))
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("text", "output", "content"):
            value = output.get(key)
            if isinstance(value, str) and value:
                return value
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


__all__ = ["MirroredItem", "hub_event_to_items"]
