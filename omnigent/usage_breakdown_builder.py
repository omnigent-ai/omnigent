"""Build cost breakdown from conversation items and session usage.

Analyzes existing conversation_items to compute a simplified cost breakdown
showing where tokens and cost were spent, without requiring real-time
instrumentation in executors.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omnigent.entities import ConversationItem
from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing
from omnigent.server.schemas import (
    CategoryBreakdown,
    CategoryBreakdownItem,
    SessionCostBreakdown,
    UsageEntry,
)
from omnigent.stores import ConversationStore

logger = logging.getLogger(__name__)

# Estimate tokens from text length using rough approximations
# Claude typically uses ~4 chars per token, but this varies
CHARS_PER_TOKEN = 4.0


def _estimate_tokens_from_text(text: str) -> int:
    """Rough estimate of token count from text length."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def _estimate_tokens_from_content(content: list[dict[str, Any]]) -> int:
    """Estimate tokens from message content blocks."""
    total = 0
    for block in content:
        block_type = block.get("type", "")

        if block_type in ("input_text", "output_text"):
            text = block.get("text", "")
            total += _estimate_tokens_from_text(text)

        elif block_type == "thinking":
            # Thinking blocks also consume tokens
            thinking = block.get("thinking", "")
            total += _estimate_tokens_from_text(thinking)

        elif block_type in ("input_image", "output_image"):
            # Images: rough estimate based on typical Claude vision costs
            # A 1024x1024 image is ~1600 tokens
            total += 1600

        elif block_type == "input_file":
            # Document/file: try to estimate from content
            # This is very rough as we don't know the actual token count
            total += 500  # Conservative estimate

    return total


def _get_tool_name(item_data: dict[str, Any]) -> str:
    """Extract tool name from function call data."""
    name = item_data.get("name", "unknown")
    # Normalize MCP tool names: "mcp__server__tool" -> "tool"
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[-1]  # Just the tool name
    return name


def _is_shell_command(tool_name: str) -> bool:
    """Check if a tool is a shell/bash command."""
    shell_tools = {"Bash", "bash", "shell", "execute_command", "run_command"}
    return tool_name in shell_tools


def build_session_cost_breakdown(
    session_id: str,
    conversation_store: ConversationStore,
    *,
    llm_model: str | None = None,
) -> SessionCostBreakdown:
    """
    Build a cost breakdown for a session by analyzing its conversation items.

    This is a simplified breakdown that categorizes usage by type (tools, shell,
    model output, user input, system overhead) without implementing full carry-cost
    tracking across turns.

    :param session_id: Session/conversation ID to analyze.
    :param conversation_store: Store to read conversation and items from.
    :param llm_model: LLM model to use for cost estimates. If not provided,
        reads from conversation metadata.
    :returns: Populated SessionCostBreakdown with per-category aggregates.
    """
    # Load conversation metadata
    conv = conversation_store.get_conversation(session_id)
    if not conv:
        return SessionCostBreakdown(session_id=session_id)

    # Get the model for pricing
    if not llm_model:
        llm_model = conv.model_override or "claude-sonnet-4-6"  # fallback

    # Fetch pricing for cost estimation
    pricing = fetch_model_pricing(llm_model) if llm_model else None

    # Initialize breakdown
    breakdown = SessionCostBreakdown(session_id=session_id)

    # Track items by category
    tools_by_name: dict[str, UsageEntry] = {}
    shell_by_name: dict[str, UsageEntry] = {}
    system_total = UsageEntry()
    user_total = UsageEntry()
    model_total = UsageEntry()
    images_total = UsageEntry()

    # Load all conversation items
    items = conversation_store.list_conversation_items(session_id)

    # Track tool call/result pairs
    tool_calls: dict[str, dict[str, Any]] = {}  # call_id -> tool data

    for item in items:
        item_data = item.data

        if item.type == "function_call":
            # Tool call (input side)
            tool_name = _get_tool_name(item_data)
            call_id = item_data.get("call_id", "")

            # Estimate tokens from arguments
            args_str = item_data.get("arguments", "")
            call_tokens = _estimate_tokens_from_text(args_str)

            # Store for pairing with result
            tool_calls[call_id] = {
                "name": tool_name,
                "call_tokens": call_tokens,
                "result_tokens": 0,
            }

        elif item.type == "function_call_output":
            # Tool result (also input to next turn, but counts as tool usage)
            call_id = item_data.get("call_id", "")
            output = item_data.get("output", "")

            result_tokens = _estimate_tokens_from_text(output)

            if call_id in tool_calls:
                tool_calls[call_id]["result_tokens"] = result_tokens
            else:
                # Orphaned result, still count it
                tool_calls[call_id] = {
                    "name": "unknown",
                    "call_tokens": 0,
                    "result_tokens": result_tokens,
                }

        elif item.type == "message":
            role = item_data.get("role")
            content = item_data.get("content", [])

            # Estimate tokens
            tokens = _estimate_tokens_from_content(content)

            if role == "user":
                # User input
                user_total.input_tokens += tokens
                user_total.total_tokens += tokens

            elif role == "assistant":
                # Model output
                model_total.output_tokens += tokens
                model_total.total_tokens += tokens

                # Check for images in output (rare but possible)
                for block in content:
                    if block.get("type") in ("output_image", "input_image"):
                        images_total.input_tokens += 1600
                        images_total.total_tokens += 1600

    # Aggregate tool usage
    for call_id, tool_data in tool_calls.items():
        tool_name = tool_data["name"]
        call_tokens = tool_data["call_tokens"]
        result_tokens = tool_data["result_tokens"]
        total_tokens = call_tokens + result_tokens

        # Create usage entry
        usage = UsageEntry(
            input_tokens=total_tokens,  # Both call and result are input on subsequent turns
            output_tokens=0,
            total_tokens=total_tokens,
        )

        # Categorize as shell or regular tool
        if _is_shell_command(tool_name):
            if tool_name not in shell_by_name:
                shell_by_name[tool_name] = UsageEntry()
            shell_by_name[tool_name].input_tokens += usage.input_tokens
            shell_by_name[tool_name].total_tokens += usage.total_tokens
        else:
            if tool_name not in tools_by_name:
                tools_by_name[tool_name] = UsageEntry()
            tools_by_name[tool_name].input_tokens += usage.input_tokens
            tools_by_name[tool_name].total_tokens += usage.total_tokens

    # Estimate system overhead (system prompt + tool schemas)
    # This is roughly 10-20% of input tokens typically
    total_input = (
        user_total.input_tokens
        + sum(t.input_tokens for t in tools_by_name.values())
        + sum(t.input_tokens for t in shell_by_name.values())
    )
    system_overhead_tokens = int(total_input * 0.15)  # 15% estimate
    system_total.input_tokens = system_overhead_tokens
    system_total.total_tokens = system_overhead_tokens

    # Compute costs if pricing is available
    if pricing:
        input_cost_per_mtok = pricing.input_price
        output_cost_per_mtok = pricing.output_price

        # Tools
        for tool_usage in tools_by_name.values():
            cost = (tool_usage.input_tokens / 1_000_000) * input_cost_per_mtok
            tool_usage.total_cost_usd = cost

        # Shell
        for shell_usage in shell_by_name.values():
            cost = (shell_usage.input_tokens / 1_000_000) * input_cost_per_mtok
            shell_usage.total_cost_usd = cost

        # System
        system_total.total_cost_usd = (
            system_total.input_tokens / 1_000_000
        ) * input_cost_per_mtok

        # User
        user_total.total_cost_usd = (user_total.input_tokens / 1_000_000) * input_cost_per_mtok

        # Model output
        model_total.total_cost_usd = (
            model_total.output_tokens / 1_000_000
        ) * output_cost_per_mtok

        # Images
        images_total.total_cost_usd = (
            images_total.input_tokens / 1_000_000
        ) * input_cost_per_mtok

    # Build category breakdowns
    breakdown.tools = CategoryBreakdown(
        total=UsageEntry(
            input_tokens=sum(t.input_tokens for t in tools_by_name.values()),
            output_tokens=0,
            total_tokens=sum(t.total_tokens for t in tools_by_name.values()),
            total_cost_usd=sum(
                t.total_cost_usd or 0 for t in tools_by_name.values()
            ),
        ),
        items=[
            CategoryBreakdownItem(name=name, usage=usage)
            for name, usage in sorted(
                tools_by_name.items(), key=lambda x: x[1].total_tokens, reverse=True
            )
        ],
    )

    breakdown.shell = CategoryBreakdown(
        total=UsageEntry(
            input_tokens=sum(t.input_tokens for t in shell_by_name.values()),
            output_tokens=0,
            total_tokens=sum(t.total_tokens for t in shell_by_name.values()),
            total_cost_usd=sum(
                t.total_cost_usd or 0 for t in shell_by_name.values()
            ),
        ),
        items=[
            CategoryBreakdownItem(name=name, usage=usage)
            for name, usage in sorted(
                shell_by_name.items(), key=lambda x: x[1].total_tokens, reverse=True
            )
        ],
    )

    breakdown.system = CategoryBreakdown(
        total=system_total,
        items=[CategoryBreakdownItem(name="System prompt + schemas", usage=system_total)],
    )

    breakdown.user = CategoryBreakdown(
        total=user_total,
        items=[CategoryBreakdownItem(name="User messages", usage=user_total)],
    )

    breakdown.model = CategoryBreakdown(
        total=model_total,
        items=[CategoryBreakdownItem(name="Assistant output", usage=model_total)],
    )

    breakdown.images = CategoryBreakdown(
        total=images_total,
        items=[CategoryBreakdownItem(name="Images & attachments", usage=images_total)]
        if images_total.total_tokens > 0
        else [],
    )

    # Compute totals
    breakdown.total_tokens = (
        breakdown.tools.total.total_tokens
        + breakdown.shell.total.total_tokens
        + breakdown.system.total.total_tokens
        + breakdown.user.total.total_tokens
        + breakdown.model.total.total_tokens
        + breakdown.images.total.total_tokens
    )

    breakdown.total_cost_usd = (
        (breakdown.tools.total.total_cost_usd or 0)
        + (breakdown.shell.total.total_cost_usd or 0)
        + (breakdown.system.total.total_cost_usd or 0)
        + (breakdown.user.total.total_cost_usd or 0)
        + (breakdown.model.total.total_cost_usd or 0)
        + (breakdown.images.total.total_cost_usd or 0)
    )

    return breakdown


__all__ = ["build_session_cost_breakdown"]
