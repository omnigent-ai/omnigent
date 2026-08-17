"""Shared recognition helpers for Claude Code transcript records."""

from __future__ import annotations

import re

_INTERRUPT_RECORD_RE = re.compile(r"^\[Request interrupted by user(?: for tool use)?\]$")


def is_claude_interrupt_text(text: str) -> bool:
    """Return whether text is Claude Code's synthetic interrupted-turn marker."""
    first_line = text.strip().split("\n", 1)[0]
    return _INTERRUPT_RECORD_RE.fullmatch(first_line) is not None
