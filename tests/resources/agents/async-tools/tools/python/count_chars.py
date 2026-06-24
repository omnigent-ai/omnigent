"""count_chars fixture tool (sync; returns character count)."""

from __future__ import annotations

from omnigent_client.tools import tool


@tool
def count_chars(text: str) -> int:
    """
    Return the literal character count of ``text``.

    :param text: Text to measure, e.g. ``"abc"``.
    :returns: Length of ``text``, e.g. ``3``.
    """
    return len(text)
