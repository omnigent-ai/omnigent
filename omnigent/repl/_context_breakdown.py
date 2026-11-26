"""Terminal text for a session's context breakdown, shared by the REPL ``/context`` and the CLI.

Prints the rows of ``GET /v1/sessions/{id}/context-breakdown`` as the server
resolved them; nothing is recomputed here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Control characters (e.g. ANSI escapes) server text must not carry into a terminal.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_ATTACHMENT_DELIVERY_LABELS = {"workspace": "in workspace", "missing": "not delivered"}
_SOURCE_SUFFIXES = {"measured": "measured", "estimated": "estimate"}


def _clean(value: object) -> str:
    """*value* as text without control characters."""
    return _CONTROL_CHARS.sub("", str(value))


def _category_label(category: Mapping[str, Any]) -> str:
    """The row title for a category, e.g. ``"Attachments (2)"``."""
    kind = _clean(category.get("kind") or "")
    count = category.get("count")
    counted = f" ({count})" if isinstance(count, int) else ""
    labels = {
        "system_prompt": "System prompt",
        "skills": f"Skill listing{counted}",
        "tools": "Tool definitions",
        "attachments": f"Attachments{counted}",
        "history": "Conversation history",
        "message": "Current message",
        "other": "Other",
        "free": "Free space",
    }
    # A kind added by a newer server still gets a readable row.
    return labels.get(kind, kind.replace("_", " "))


def _row(label: str, value: str, suffix: str = "", *, nested: bool = False) -> str:
    """One aligned line, e.g. ``"  Tool definitions        ~5,400  estimate"``."""
    indent = "    " if nested else "  "
    return f"{indent}{label:<{26 - len(indent)}} {value:>12}  {suffix}".rstrip()


def _category_lines(category: Mapping[str, Any]) -> list[str]:
    """A category's line, with its detail on a following indented line."""
    tokens = category.get("tokens")
    source = category.get("source")
    if isinstance(tokens, int):
        value = f"{'~' if source == 'estimated' else ''}{tokens:,}"
    else:
        value = "not measured"
    lines = [_row(_category_label(category), value, _SOURCE_SUFFIXES.get(str(source), ""))]
    detail = category.get("detail")
    if detail:
        lines.append(f"    {_clean(detail)}")
    return lines


def _attachment_line(attachment: Mapping[str, Any]) -> str:
    """One attachment's nested line: its estimate, or how it was delivered."""
    name = _clean(attachment.get("filename") or "") or "Attachment"
    delivery = attachment.get("delivery")
    estimate = attachment.get("estimated_tokens")
    if delivery == "inline" and isinstance(estimate, int):
        return _row(name, f"~{estimate:,}", "estimate", nested=True)
    return _row(name, _ATTACHMENT_DELIVERY_LABELS.get(str(delivery), "size only"), nested=True)


def _mappings(value: object) -> list[Mapping[str, Any]]:
    """The JSON objects in a list field; anything else is skipped."""
    return (
        [entry for entry in value if isinstance(entry, Mapping)] if isinstance(value, list) else []
    )


def breakdown_body_lines(breakdown: Mapping[str, Any]) -> list[str]:
    """
    The breakdown's rows, any failed attempt, and the estimate note, as text lines.

    :param breakdown: The context-breakdown response JSON.
    :returns: Lines without the usage header.
    """
    lines: list[str] = []
    for category in _mappings(breakdown.get("categories")):
        lines.extend(_category_lines(category))
        if category.get("kind") == "attachments":
            lines.extend(
                _attachment_line(attachment)
                for attachment in _mappings(breakdown.get("attachments"))
            )
    failed = breakdown.get("failed_attempt")
    if isinstance(failed, Mapping):
        total = failed.get("total_tokens")
        attempted = f" (~{total:,} tokens when the turn started)" if isinstance(total, int) else ""
        lines.append(f"Last request was too long for the context window{attempted}:")
        for category in _mappings(failed.get("categories")):
            lines.extend(_category_lines(category))
    per_token = breakdown.get("bytes_per_token")
    if isinstance(per_token, int):
        lines.append(f"Estimates come from content size, about {per_token} bytes per token.")
    return lines


def format_context_breakdown(breakdown: Mapping[str, Any]) -> str:
    """
    The breakdown as plain text, as ``omnigent session context`` prints it.

    :param breakdown: The context-breakdown response JSON.
    :returns: Multi-line text, headed by the measured usage the rows split.
    """
    measured = breakdown.get("measured")
    total = measured.get("total_tokens") if isinstance(measured, Mapping) else None
    window = measured.get("context_window") if isinstance(measured, Mapping) else None
    if isinstance(total, int) and isinstance(window, int) and window > 0:
        pct = round(min(total / window, 1.0) * 100)
        header = f"Context window: {total:,} / {window:,} tokens ({pct}% used, measured)"
    elif isinstance(total, int):
        header = f"Context window: {total:,} tokens used (window size unknown)"
    else:
        header = "Context window: no usage reported yet"
    return "\n".join([header, *breakdown_body_lines(breakdown)])
