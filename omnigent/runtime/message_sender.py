"""Name a collaborator to the agent when they send a message in someone else's session."""

from __future__ import annotations

import json
import re
from typing import Any

from omnigent.inner.native_attachments import ATTACHMENT_MARKER_STRIP_PATTERN


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


def message_sender_for(content: object, sender: str | None) -> str | None:
    """
    Decide whether *content* goes to the model behind a line naming *sender*.

    Slash and shell commands go as typed so a native harness still runs them.

    :param content: Message content blocks, e.g.
        ``[{"type": "input_text", "text": "Hi"}]``.
    :param sender: Identity to name, or ``None``.
    :returns: *sender* when the line is added, else ``None``.
    """
    line = message_sender_line(sender)
    if not line or not isinstance(content, list) or not content:
        return None
    text = "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    if text.lstrip().startswith(("/", "!")) or text.startswith(line):
        return None
    return sender


def with_message_sender(content: list[dict[str, Any]], sender: str | None) -> list[dict[str, Any]]:
    """
    Prepend the line naming *sender* when :func:`message_sender_for` adds one.

    :param content: Message content blocks. Not mutated.
    :param sender: Identity to name, or ``None`` to return *content* unchanged.
    :returns: The content, with a leading ``input_text`` block naming the sender.
    """
    named = message_sender_for(content, sender)
    if named is None:
        return content
    return [{"type": "input_text", "text": message_sender_line(named)}, *content]


def strip_message_sender(text: str, sender: str | None) -> str:
    """
    Remove the line a native executor typed ahead of a message naming *sender*.

    Only a line at the start, behind any generated attachment markers, is removed,
    with the line breaks after it; text the person typed keeps its spacing.

    :param text: Transcript text of a delivered message, e.g.
        ``'[Message sender: "bob@example.com"]\\n\\nHi'``.
    :param sender: Identity the server named for that message.
    :returns: The text without that line, e.g. ``"Hi"``.
    """
    line = message_sender_line(sender)
    if not line:
        return text
    leading = rf"^((?:\s*(?:{ATTACHMENT_MARKER_STRIP_PATTERN}))*\s*)"
    return re.sub(rf"{leading}{re.escape(line)}(?:(?:\r?\n)+|\Z)", r"\1", text, count=1)
