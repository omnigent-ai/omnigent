"""Usage and cost breakdown tracking.

Simplified cost breakdown that aggregates costs by category (tools, shell,
model, system, user) and by individual tool/command names. Does not implement
full carry-cost (tracking survival across turns), but provides clear
attribution of where tokens/cost were spent.

The breakdown data is stored in the existing ``session_usage`` JSON column,
nested under a ``breakdown`` key to avoid schema changes.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# Category types for cost breakdown
CategoryType = Literal[
    "tools",      # Tool calls (combined call + result)
    "shell",      # Shell/bash commands
    "model",      # Model output (assistant messages)
    "system",     # System prompts, tool schemas, harness overhead
    "user",       # User input messages
    "images",     # Images and attachments
]


class UsageEntry(TypedDict, total=False):
    """Token and cost counters for a single breakdown entry."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_cost_usd: float  # only present when priced


class ToolBreakdownEntry(UsageEntry, total=False):
    """Extended breakdown entry for tools, with call/result split."""

    call_tokens: int      # Tokens in tool call (input)
    result_tokens: int    # Tokens in tool result (input on next request)


class CategoryBreakdown(TypedDict, total=False):
    """Breakdown for one category with per-item details."""

    total: UsageEntry
    # Map of item name to its usage (tool name, command, etc.)
    # For "system" this might be: {"preamble": {...}, "harness_reminders": {...}}
    # For "tools" this would be: {"Read": {...}, "Edit": {...}, ...}
    by_item: dict[str, UsageEntry | ToolBreakdownEntry]


class UsageBreakdown(TypedDict, total=False):
    """Top-level breakdown structure stored in session_usage["breakdown"]."""

    # Per-category aggregates
    tools: CategoryBreakdown
    shell: CategoryBreakdown
    model: CategoryBreakdown
    system: CategoryBreakdown
    user: CategoryBreakdown
    images: CategoryBreakdown


def init_usage_entry() -> UsageEntry:
    """Create an empty usage entry with all counters at zero."""
    return UsageEntry(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def init_tool_breakdown_entry() -> ToolBreakdownEntry:
    """Create an empty tool breakdown entry."""
    entry = ToolBreakdownEntry(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        call_tokens=0,
        result_tokens=0,
    )
    return entry


def init_category_breakdown() -> CategoryBreakdown:
    """Create an empty category breakdown."""
    return CategoryBreakdown(
        total=init_usage_entry(),
        by_item={},
    )


def init_usage_breakdown() -> UsageBreakdown:
    """Create an empty usage breakdown with all categories."""
    return UsageBreakdown(
        tools=init_category_breakdown(),
        shell=init_category_breakdown(),
        model=init_category_breakdown(),
        system=init_category_breakdown(),
        user=init_category_breakdown(),
        images=init_category_breakdown(),
    )


def accumulate_usage(
    target: UsageEntry,
    source: UsageEntry | dict[str, Any],
) -> None:
    """Add source usage counters to target in place.

    :param target: Usage entry to accumulate into.
    :param source: Usage data to add (UsageEntry or dict).
    """
    for key in ("input_tokens", "output_tokens", "total_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens",
                "total_cost_usd"):
        if key in source:
            value = source[key]
            if isinstance(value, (int, float)):
                target[key] = target.get(key, 0) + value  # type: ignore


def accumulate_category(
    breakdown: UsageBreakdown,
    category: CategoryType,
    item_name: str,
    usage: UsageEntry | dict[str, Any],
) -> None:
    """Accumulate usage into a specific category and item.

    :param breakdown: The usage breakdown to update.
    :param category: Which category to add to (tools, shell, model, etc.).
    :param item_name: Name of the specific item (tool name, command, etc.).
    :param usage: Usage data to accumulate.
    """
    # Ensure category exists
    if category not in breakdown:
        breakdown[category] = init_category_breakdown()

    cat_breakdown = breakdown[category]

    # Accumulate into category total
    accumulate_usage(cat_breakdown["total"], usage)

    # Accumulate into per-item breakdown
    if "by_item" not in cat_breakdown:
        cat_breakdown["by_item"] = {}

    if item_name not in cat_breakdown["by_item"]:
        cat_breakdown["by_item"][item_name] = init_usage_entry()

    accumulate_usage(cat_breakdown["by_item"][item_name], usage)


def get_breakdown_from_session_usage(
    session_usage: dict[str, Any] | None,
) -> UsageBreakdown:
    """Extract or initialize breakdown from session_usage dict.

    :param session_usage: The session_usage JSON dict from the database.
    :returns: The breakdown structure (existing or newly initialized).
    """
    if not session_usage:
        return init_usage_breakdown()

    breakdown = session_usage.get("breakdown")
    if isinstance(breakdown, dict):
        return breakdown  # type: ignore

    return init_usage_breakdown()


def set_breakdown_in_session_usage(
    session_usage: dict[str, Any],
    breakdown: UsageBreakdown,
) -> None:
    """Store breakdown back into session_usage dict.

    :param session_usage: The session_usage dict to update.
    :param breakdown: The breakdown to store.
    """
    session_usage["breakdown"] = breakdown


__all__ = [
    "CategoryType",
    "UsageEntry",
    "ToolBreakdownEntry",
    "CategoryBreakdown",
    "UsageBreakdown",
    "init_usage_entry",
    "init_tool_breakdown_entry",
    "init_category_breakdown",
    "init_usage_breakdown",
    "accumulate_usage",
    "accumulate_category",
    "get_breakdown_from_session_usage",
    "set_breakdown_in_session_usage",
]
