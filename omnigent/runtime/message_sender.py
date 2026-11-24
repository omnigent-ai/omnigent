"""Name a collaborator to the agent when they send a message in someone else's session."""

from __future__ import annotations

import json
import re
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
    Prepend the sender line to user message content sent to the model.

    Slash and shell commands are left as typed so a native harness still runs them.

    :param content: Message content blocks, e.g.
        ``[{"type": "input_text", "text": "Hi"}]``. Not mutated.
    :param sender: Identity to name, or ``None`` to return *content* unchanged.
    :returns: The content, with a leading ``input_text`` block naming the sender.
    """
    line = message_sender_line(sender)
    if not line or not content:
        return content
    text = "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    if text.lstrip().startswith(("/", "!")) or text.startswith(line):
        return content
    return [{"type": "input_text", "text": line}, *content]


def strip_message_sender(text: str, sender: str | None) -> str:
    """
    Remove the line :func:`with_message_sender` added for *sender*.

    :param text: Text a native harness recorded for a typed message, e.g.
        ``'[Message sender: "bob@example.com"]\\n\\nHi'``.
    :param sender: Identity the line names. Lines naming anyone else stay.
    :returns: The text without that line and the blank space after it, e.g. ``"Hi"``.
    """
    line = message_sender_line(sender)
    if not line:
        return text
    return re.sub(rf"{re.escape(line)}\s*", "", text, count=1)
