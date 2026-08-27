"""Server-side support for ``/btw`` side questions.

A side question is answered from the session's transcript but never
joins it. The server owns two things the runner cannot: turning stored
items into a bounded prompt excerpt, and persisting the result as a
``side_question`` item that renders in the transcript while staying out
of the model's history.
"""

from __future__ import annotations

from typing import Any

from omnigent.entities import NON_CONTENT_ITEM_TYPES, ConversationItem

# How many recent items to read before rendering. The excerpt is capped
# by characters, not items, so this only has to be comfortably larger
# than what the cap can hold.
SIDE_QUESTION_ITEM_SCAN_LIMIT = 200

# Tool payloads are the bulk of a coding transcript and the least useful
# per byte for answering a question about it, so they are summarized
# rather than quoted whole.
_TOOL_TEXT_LIMIT = 400


def _blocks_to_text(content: Any) -> str:
    """Join the text-bearing blocks of a message's content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]
    )


def _truncate(text: str, limit: int) -> str:
    """Clip *text* to *limit* characters, marking that it was clipped."""
    collapsed = text.strip()
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}… [truncated]"


def render_item_for_excerpt(item: ConversationItem) -> str | None:
    """
    Render one item as a transcript line, or ``None`` to skip it.

    Skips every :data:`NON_CONTENT_ITEM_TYPES` member, which also means
    earlier side questions never feed the next one — an aside is not
    part of the conversation it was asked about.

    :param item: A persisted conversation item.
    :returns: One line of plain text, or ``None`` when the item carries
        nothing worth showing.
    """
    if item.type in NON_CONTENT_ITEM_TYPES:
        return None
    data = item.data.model_dump()
    if item.type == "message":
        if data.get("is_meta"):
            return None
        text = _blocks_to_text(data.get("content"))
        if not text.strip():
            return None
        role = data.get("role") or "user"
        return f"{role}: {text.strip()}"
    if item.type == "function_call":
        name = data.get("name") or "tool"
        arguments = _truncate(str(data.get("arguments") or ""), _TOOL_TEXT_LIMIT)
        return f"assistant called {name}({arguments})"
    if item.type == "function_call_output":
        return f"tool result: {_truncate(str(data.get('output') or ''), _TOOL_TEXT_LIMIT)}"
    if item.type == "reasoning":
        # Reasoning is the model talking to itself; it explains the
        # session's direction, which is often exactly what's being asked.
        text = _blocks_to_text(data.get("summary"))
        return f"assistant (thinking): {text.strip()}" if text.strip() else None
    return None


def build_transcript_excerpt(items: list[ConversationItem], *, max_chars: int) -> str:
    """
    Render recent items into a bounded plain-text excerpt.

    Trims from the *front* when over budget: the tail of a session is
    what a question mid-session is usually about, and dropping the head
    keeps the most recent turns intact rather than half-quoting them.

    :param items: Items in chronological order.
    :param max_chars: Hard cap on the returned string.
    :returns: Newline-joined transcript lines, oldest first. Empty when
        nothing renderable is left.
    """
    lines = [line for line in (render_item_for_excerpt(item) for item in items) if line]
    if not lines:
        return ""
    kept: list[str] = []
    budget = max_chars
    for line in reversed(lines):
        cost = len(line) + 1
        if cost > budget:
            break
        kept.append(line)
        budget -= cost
    kept.reverse()
    if not kept:
        # One line already exceeds the whole budget — keep its tail so
        # the model sees the latest turn rather than nothing at all.
        return lines[-1][-max_chars:]
    return "\n".join(kept)
