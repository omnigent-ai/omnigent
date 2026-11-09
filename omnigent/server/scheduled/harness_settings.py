"""Harness-specific permission overrides for saved automations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.native.permission_modes import (
    APPROVAL_SKIPPING_PERMISSION_MODES,
    PERMISSION_LAUNCH_ARGS,
)

_logger = logging.getLogger(__name__)

INHERITED_APPROVAL_SKIPPING_MESSAGE = (
    "The agent's spec sets permission mode {mode!r}, which skips approval, and an automation "
    "cannot inherit it; set a mode in the agent spec that asks for approval or uses the auto "
    "classifier"
)


def validate_scheduled_permission_mode(mode: str | None) -> str | None:
    """Validate the vocabulary before resolving the bound agent's harness."""
    if mode is None:
        return None
    if not any(mode in modes for modes in PERMISSION_LAUNCH_ARGS.values()):
        raise OmnigentError(f"Unsupported permission_mode: {mode!r}", code=ErrorCode.INVALID_INPUT)
    return mode


async def _scheduled_agent_executor(agent: Any, agent_cache: Any) -> tuple[str | None, Any]:
    """Load the saved agent's executor; ``(None, None)`` when it cannot be loaded."""
    if agent is None or agent_cache is None or getattr(agent, "bundle_location", None) is None:
        return None, None
    try:
        loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
        executor = getattr(loaded.spec, "executor", None)
        raw = (executor.config.get("harness") or executor.type) if executor else None
        return canonicalize_harness(raw) or raw, executor
    except Exception:
        _logger.exception("Could not resolve harness for scheduled agent %s", agent.id)
        return None, None


async def scheduled_agent_harness(agent: Any, agent_cache: Any) -> str | None:
    """Resolve the saved agent; return None when its harness cannot be determined."""
    harness, _ = await _scheduled_agent_executor(agent, agent_cache)
    return harness


async def scheduled_agent_default_permission_mode(agent: Any, agent_cache: Any) -> str | None:
    """The mode a run with no task override launches with, or ``None`` for the harness default.

    Native harnesses start in their CLI's own default. SDK harnesses take
    ``executor.config.permission_mode`` from the spec; without one they run in
    ``auto``. ``None`` also when the agent cannot be loaded.
    """
    harness, executor = await _scheduled_agent_executor(agent, agent_cache)
    if harness is None or executor is None or is_native_harness(harness):
        return None
    configured = executor.config.get("permission_mode")
    return None if configured is None else str(configured)


async def inherited_approval_skipping_problem(agent: Any, agent_cache: Any) -> str | None:
    """Why a task without a permission mode may not run in its agent's default, or ``None``.

    An explicit task mode is shown in the automation's settings and its approval
    prompt; a mode inherited from the agent spec is not, so it may not skip approval.
    """
    mode = await scheduled_agent_default_permission_mode(agent, agent_cache)
    if mode not in APPROVAL_SKIPPING_PERMISSION_MODES:
        return None
    return INHERITED_APPROVAL_SKIPPING_MESSAGE.format(mode=mode)


async def validate_scheduled_permission_support(
    *, permission_mode: str | None, agent: Any, agent_cache: Any
) -> None:
    """Reject modes belonging to a different harness, or an inherited approval-skipping mode."""
    if permission_mode is None:
        problem = await inherited_approval_skipping_problem(agent, agent_cache)
        if problem is not None:
            raise OmnigentError(problem, code=ErrorCode.INVALID_INPUT)
        return
    harness = await scheduled_agent_harness(agent, agent_cache)
    if harness is None:
        raise OmnigentError(
            "Cannot resolve the agent's harness to validate permission_mode; try again later",
            code=ErrorCode.INVALID_INPUT,
        )
    if permission_mode not in PERMISSION_LAUNCH_ARGS.get(harness, {}):
        raise OmnigentError(
            f"permission_mode {permission_mode!r} is not supported for {harness!r}",
            code=ErrorCode.INVALID_INPUT,
        )
