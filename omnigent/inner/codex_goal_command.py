"""Shared ``/goal`` command parsing for Codex harnesses."""

from __future__ import annotations


def goal_objective_from_content(content: object) -> str | None:
    """Return the objective from a standalone, text-only ``/goal`` command."""
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                return None
            if block.get("type") not in {"input_text", "text"}:
                return None
            text = block.get("text")
            if not isinstance(text, str):
                return None
            text_parts.append(text)
        content = "".join(text_parts)
    if not isinstance(content, str):
        return None
    command, separator, objective = content.strip().partition(" ")
    if command != "/goal" or not separator:
        return None
    objective = objective.strip()
    return objective or None
