"""Terminal rows for a session's context breakdown, shared by the REPL ``/context`` and the CLI.

Reads the ``GET /v1/sessions/{id}/context-breakdown`` response and mirrors the
web context ring: the runner's per-turn breakdown wins over the server's
estimates where both exist, and "Other" is the measured total minus every
itemized estimate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

RowSource = Literal["measured", "estimated", "unknown"]

# Control characters (e.g. ANSI escapes) a filename must not carry into a terminal.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class ContextRow:
    """
    One line of the breakdown.

    :param label: Row name, e.g. ``"Tool definitions"``.
    :param tokens: Token figure, or ``None`` when not measured.
    :param source: Whether ``tokens`` is measured or an estimate.
    :param detail: Optional second line, e.g. ``"18 tools"``.
    :param nested: An item under the previous row, e.g. one attachment.
    :param value_label: Shown in place of a missing figure, e.g. ``"in workspace"``.
    """

    label: str
    tokens: int | None
    source: RowSource
    detail: str | None = None
    nested: bool = False
    value_label: str | None = None


def _int(value: object) -> int:
    """A non-negative int field from the wire, ``0`` when absent or malformed."""
    return value if isinstance(value, int) and value > 0 else 0


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _history_detail(history: Mapping[str, Any], item_count: int, *, exact: bool) -> str:
    noun = "message or tool call" if item_count == 1 else "messages and tool calls"
    # The runner counts what it sent, so the server's scan cap does not apply.
    prefix = "At least " if history.get("partial") and not exact else ""
    parts = [f"{prefix}{item_count} {noun}"]
    summarized = _int(history.get("summarized_item_count"))
    if summarized:
        at_least = "at least " if history.get("summarized_partial") else ""
        items = "item" if summarized == 1 else "items"
        parts.append(f"{at_least}{summarized} earlier {items} replaced by a summary")
    return " · ".join(parts)


def _tools_detail(tools: Mapping[str, Any], tool_count: int | None) -> str:
    parts: list[str] = []
    if tool_count is not None:
        parts.append(_plural(tool_count, "tool"))
    servers = tools.get("mcp_servers")
    if isinstance(servers, list) and servers:
        parts.append("MCP: " + ", ".join(str(name) for name in servers))
    if tool_count is None:
        if builtin := _int(tools.get("builtin_tool_count")):
            parts.append(_plural(builtin, "built-in tool"))
        if local := _int(tools.get("local_tool_count")):
            parts.append(_plural(local, "agent tool"))
    return " · ".join(parts) or "Harness and Omnigent tools only"


def _attachment_rows(
    attachments: list[Mapping[str, Any]], *, with_estimates: bool
) -> list[ContextRow]:
    """One nested row per attachment; without estimates when a runner total heads them."""
    rows: list[ContextRow] = []
    for attachment in attachments:
        name = _CONTROL_CHARS.sub("", str(attachment.get("filename") or "")) or "Attachment"
        delivery = attachment.get("delivery")
        estimate = attachment.get("estimated_tokens")
        if delivery == "inline" and isinstance(estimate, int) and with_estimates:
            rows.append(ContextRow(name, estimate, "estimated", nested=True))
            continue
        value_label = {"workspace": "in workspace", "missing": "not delivered"}.get(
            str(delivery), "size only" if with_estimates else "sent inline"
        )
        rows.append(ContextRow(name, None, "unknown", nested=True, value_label=value_label))
    return rows


def turn_breakdown_rows(turn: Mapping[str, Any]) -> list[ContextRow]:
    """
    Rows for one runner-reported request, e.g. a failed attempt.

    :param turn: A ``TurnContextBreakdown`` JSON object.
    :returns: One estimated row per category with a non-zero figure.
    """
    history_count = _int(turn.get("history_item_count"))
    history_noun = "message or tool call" if history_count == 1 else "messages and tool calls"
    rows = [
        ContextRow("System prompt", _int(turn.get("system_tokens")), "estimated"),
        ContextRow(
            "Tool definitions",
            _int(turn.get("tool_tokens")),
            "estimated",
            detail=_plural(_int(turn.get("tool_count")), "tool"),
        ),
        ContextRow(
            "Conversation history",
            _int(turn.get("history_tokens")),
            "estimated",
            detail=f"{history_count} {history_noun}",
        ),
        ContextRow(
            f"Attachments ({_int(turn.get('attachment_count'))})",
            _int(turn.get("attachment_tokens")),
            "estimated",
        ),
        ContextRow("Current message", _int(turn.get("message_tokens")), "estimated"),
    ]
    return [row for row in rows if row.tokens]


def turn_total_tokens(turn: Mapping[str, Any]) -> int:
    """Sum of a runner-reported request's itemized estimates."""
    return sum(
        _int(turn.get(key))
        for key in (
            "system_tokens",
            "tool_tokens",
            "history_tokens",
            "attachment_tokens",
            "message_tokens",
        )
    )


def context_breakdown_rows(
    breakdown: Mapping[str, Any],
    *,
    tokens_used: int | None,
    context_window: int | None,
) -> list[ContextRow]:
    """
    Rows splitting the window into what fills it.

    :param breakdown: The context-breakdown response JSON.
    :param tokens_used: The measured total of the last turn, or ``None``.
    :param context_window: The model's window, or ``None`` when unknown.
    :returns: Rows from system prompt down to free space.
    """
    reported = breakdown.get("reported")
    reported = reported if isinstance(reported, Mapping) else None
    history = breakdown.get("history")
    history = history if isinstance(history, Mapping) else {}
    tools = breakdown.get("tools")
    tools = tools if isinstance(tools, Mapping) else {}
    raw_attachments = breakdown.get("attachments")
    attachments = [
        attachment
        for attachment in (raw_attachments if isinstance(raw_attachments, list) else [])
        if isinstance(attachment, Mapping)
    ]
    rows: list[ContextRow] = []
    if reported is not None:
        rows.append(
            ContextRow(
                "System prompt",
                _int(reported.get("system_tokens")),
                "estimated",
                detail="Sent by the runner on the last turn",
            )
        )
        rows.append(
            ContextRow(
                "Tool definitions",
                _int(reported.get("tool_tokens")),
                "estimated",
                detail=_tools_detail(tools, _int(reported.get("tool_count"))),
            )
        )
        attachment_count = _int(reported.get("attachment_count"))
        if attachment_count or attachments:
            rows.append(
                ContextRow(
                    f"Attachments ({attachment_count})",
                    _int(reported.get("attachment_tokens")),
                    "estimated",
                )
            )
            rows.extend(_attachment_rows(attachments, with_estimates=False))
        rows.append(
            ContextRow(
                "Conversation history",
                _int(reported.get("history_tokens")),
                "estimated",
                detail=_history_detail(
                    history, _int(reported.get("history_item_count")), exact=True
                ),
            )
        )
        rows.append(
            ContextRow("Current message", _int(reported.get("message_tokens")), "estimated")
        )
        itemized = turn_total_tokens(reported)
    else:
        system = breakdown.get("system_prompt_tokens")
        rows.append(
            ContextRow(
                "System prompt",
                system if isinstance(system, int) else None,
                "estimated" if isinstance(system, int) else "unknown",
                detail="Agent instructions and Omnigent guidance",
            )
        )
        skill_count = _int(breakdown.get("skill_count"))
        skill_tokens = _int(breakdown.get("skill_listing_tokens"))
        if skill_count:
            rows.append(ContextRow(f"Skill listing ({skill_count})", skill_tokens, "estimated"))
        rows.append(
            ContextRow("Tool definitions", None, "unknown", detail=_tools_detail(tools, None))
        )
        inline = [a for a in attachments if a.get("delivery") == "inline"]
        attachment_tokens = sum(_int(a.get("estimated_tokens")) for a in inline)
        if attachments:
            rows.append(
                ContextRow(f"Attachments ({len(attachments)})", attachment_tokens, "estimated")
            )
            rows.extend(_attachment_rows(attachments, with_estimates=True))
        history_tokens = _int(history.get("estimated_tokens"))
        rows.append(
            ContextRow(
                "Conversation history",
                history_tokens,
                "estimated",
                detail=_history_detail(history, _int(history.get("item_count")), exact=False),
            )
        )
        itemized = (
            (system if isinstance(system, int) else 0)
            + skill_tokens
            + attachment_tokens
            + history_tokens
        )
    if tokens_used is not None:
        rows.append(
            ContextRow(
                "Other",
                max(tokens_used - itemized, 0),
                "estimated",
                detail=(
                    "The estimates above exceed the measured total, so they run high"
                    if itemized > tokens_used
                    else "Content the harness adds itself and the last reply"
                ),
            )
        )
        if context_window:
            rows.append(ContextRow("Free space", max(context_window - tokens_used, 0), "measured"))
    return rows


def format_row(row: ContextRow) -> str:
    """
    Plain-text line for a row, e.g. ``"  Tool definitions   ~5,400  estimate"``.

    :param row: The row to format.
    :returns: The line, with its detail on a following indented line.
    """
    if row.tokens is None:
        value = row.value_label or "not measured"
    else:
        value = f"{'~' if row.source == 'estimated' else ''}{row.tokens:,}"
    suffix = {"measured": "measured", "estimated": "estimate", "unknown": ""}[row.source]
    indent = "    " if row.nested else "  "
    label_width = 26 - len(indent)
    line = f"{indent}{row.label:<{label_width}} {value:>12}  {suffix}".rstrip()
    if row.detail:
        line += f"\n{indent}  {row.detail}"
    return line


def breakdown_body_lines(
    breakdown: Mapping[str, Any],
    *,
    tokens_used: int | None,
    context_window: int | None,
) -> list[str]:
    """
    The breakdown's rows, any failed attempt, and the estimate note, as text lines.

    :param breakdown: The context-breakdown response JSON.
    :param tokens_used: The measured total of the last turn, or ``None``.
    :param context_window: The model's window, or ``None`` when unknown.
    :returns: Lines without the usage header.
    """
    lines = [
        format_row(row)
        for row in context_breakdown_rows(
            breakdown, tokens_used=tokens_used, context_window=context_window
        )
    ]
    failed = breakdown.get("failed_attempt")
    if isinstance(failed, Mapping):
        lines.append(
            "Last request was too long for the context window "
            f"(~{turn_total_tokens(failed):,} tokens attempted):"
        )
        lines.extend(format_row(row) for row in turn_breakdown_rows(failed))
    per_token = breakdown.get("bytes_per_token") or 4
    lines.append(f"Estimates come from content size, about {per_token} bytes per token.")
    return lines


def format_context_breakdown(
    breakdown: Mapping[str, Any],
    *,
    tokens_used: int | None,
    context_window: int | None,
) -> str:
    """
    The breakdown as plain text, as ``omnigent session context`` prints it.

    :param breakdown: The context-breakdown response JSON.
    :param tokens_used: The measured total of the last turn, or ``None``.
    :param context_window: The model's window, or ``None`` when unknown.
    :returns: Multi-line text.
    """
    if tokens_used is not None and context_window:
        pct = round(min(tokens_used / context_window, 1.0) * 100)
        header = (
            f"Context window: {tokens_used:,} / {context_window:,} tokens ({pct}% used, measured)"
        )
    elif tokens_used is not None:
        header = f"Context window: {tokens_used:,} tokens used (window size unknown)"
    else:
        header = "Context window: no usage reported yet"
    body = breakdown_body_lines(breakdown, tokens_used=tokens_used, context_window=context_window)
    return "\n".join([header, *body])
