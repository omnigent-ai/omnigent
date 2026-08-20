"""Local word-count tool (e2e fixture, primitive str arg)."""

from __future__ import annotations

from omnigent_client.tools import tool


@tool
def word_count(text: str) -> dict[str, int]:
    """
    Count whitespace-delimited words in ``text``.

    :param text: Text to count, e.g. ``"one two three"``.
    :returns: A JSON-serializable dict, e.g. ``{"word_count": 3}``.
    """
    return {"word_count": len(text.split())}
