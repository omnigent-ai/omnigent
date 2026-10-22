"""Shared ``/goal`` command parsing for Codex harnesses."""

from __future__ import annotations

# Codex app-server rejects longer goal objectives with a raw JSON-RPC -32600
# error; the ``PUT /v1/sessions/{id}/codex_goal`` route enforces the same cap.
GOAL_OBJECTIVE_MAX_CHARS = 4000


def goal_objective_length_error(objective: str) -> str | None:
    """
    Return a user-facing error when the objective exceeds Codex's cap.

    :param objective: Parsed ``/goal`` objective text.
    :returns: A clear rejection message, or ``None`` when the objective fits.
    """
    if len(objective) <= GOAL_OBJECTIVE_MAX_CHARS:
        return None
    return (
        f"Goal objective is {len(objective)} characters; Codex allows at most "
        f"{GOAL_OBJECTIVE_MAX_CHARS}. Shorten the /goal objective and send it again."
    )


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
