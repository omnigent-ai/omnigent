"""Shared builders for agy trajectory step dicts.

The pure predicate tests and the executor's completion-gate tests both construct
the same ``CORTEX_STEP_TYPE_*`` shapes agy records on a cascade. Centralise the
builders so each test keeps supplying its own explicit inputs and assertions
without duplicating the step dicts.
"""

from __future__ import annotations

from typing import Any


def user_step(text: str) -> dict[str, Any]:
    """
    Build the USER_INPUT step agy records for a delivered turn.

    :param text: The user turn text agy echoed back onto the cascade.
    :returns: One USER_INPUT step dict.
    """
    return {"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {"userResponse": text}}


def planner_step(
    *, status: str, text: str | None = None, error: str | None = None
) -> dict[str, Any]:
    """
    Build a PLANNER_RESPONSE step at ``status``.

    :param status: ``CORTEX_STEP_STATUS_*`` value.
    :param text: Assistant text the planner carries, if any.
    :param error: agy error detail, for the ERROR shape.
    :returns: One PLANNER_RESPONSE step dict.
    """
    planner: dict[str, Any] = {}
    if text is not None:
        planner["response"] = text
    if error is not None:
        planner["error"] = error
    return {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": status,
        "plannerResponse": planner,
    }


def tool_step(status: str) -> dict[str, Any]:
    """
    Build a RUN_COMMAND tool step at ``status``.

    :param status: ``CORTEX_STEP_STATUS_*`` value.
    :returns: One tool step dict.
    """
    return {
        "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
        "status": status,
        "runCommand": {"command": "pytest"},
        # ``metadata.toolAction`` is what marks a step as a tool call on the live
        # wire, in both RPC shapes (see ``_is_tool_step``).
        "metadata": {"toolAction": "Running command"},
    }
