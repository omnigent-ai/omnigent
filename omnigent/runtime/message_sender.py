"""Name a collaborator to the agent when they send a message in someone else's session."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def message_sender_line(sender: str | None) -> str:
    """
    Return the context line naming *sender*, or ``""`` without one.

    :param sender: Authenticated identity, e.g. ``"bob@example.com"``.
    :returns: e.g. ``'[Message sender: "bob@example.com"]'``. JSON quoting keeps
        an identity on one line and stops it from closing the bracket early.
    """
    if not sender:
        return ""
    return f"[Message sender: {json.dumps(sender, ensure_ascii=True)}]"


def with_message_sender(content: list[dict[str, Any]], sender: str | None) -> list[dict[str, Any]]:
    """
    Insert the line naming *sender* ahead of the message's text for the model.

    The line goes after any leading attachments, so a native terminal types it
    where its generated attachment markers end, whichever order it keeps.

    :param content: Message content blocks, e.g.
        ``[{"type": "input_text", "text": "Hi"}]``. Not mutated.
    :param sender: Identity the server chose to name, or ``None`` to return
        *content* unchanged.
    :returns: The content with an ``input_text`` block naming the sender.
    """
    line = message_sender_line(sender)
    if not line or not content:
        return content
    index = sender_line_index(content)
    return [*content[:index], {"type": "input_text", "text": line}, *content[index:]]


def sender_line_index(content: list[dict[str, Any]]) -> int:
    """
    Return where :func:`with_message_sender` puts the line: ahead of the first text block.

    :param content: Message content blocks before the line is added.
    :returns: The block index, or ``len(content)`` when there is no text block.
    """
    return next(
        (
            index
            for index, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") in ("input_text", "text")
        ),
        len(content),
    )


def replayed_content(item: Mapping[str, object]) -> object:
    """
    Return a stored message item's content as the model received it.

    :param item: A conversation item as served by the items API, e.g.
        ``{"type": "message", "role": "user", "created_by": "bob@example.com",
        "sender_in_context": True, "content": [...]}``.
    :returns: The content behind its sender line when the server named one,
        else the stored content unchanged.
    """
    content = item.get("content")
    sender = item.get("created_by")
    if (
        item.get("sender_in_context") is True
        and item.get("role", "user") == "user"
        and isinstance(content, list)
        and isinstance(sender, str)
    ):
        return with_message_sender(content, sender)
    return content
