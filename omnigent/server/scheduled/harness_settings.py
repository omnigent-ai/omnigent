"""Harness-specific permission overrides for saved automations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS

_logger = logging.getLogger(__name__)


def validate_scheduled_permission_mode(mode: str | None) -> str | None:
    """Validate the vocabulary before resolving the bound agent's harness."""
    if mode is None:
        return None
    if not any(mode in modes for modes in PERMISSION_LAUNCH_ARGS.values()):
        raise OmnigentError(f"Unsupported permission_mode: {mode!r}", code=ErrorCode.INVALID_INPUT)
    return mode


async def scheduled_agent_harness(agent: Any, agent_cache: Any) -> str | None:
    """Resolve the saved agent; return None when its harness cannot be determined."""
    if agent is None or agent_cache is None or getattr(agent, "bundle_location", None) is None:
        return None
    try:
        loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
        executor = getattr(loaded.spec, "executor", None)
        raw = (executor.config.get("harness") or executor.type) if executor else None
        return canonicalize_harness(raw) or raw
    except Exception:
        _logger.exception("Could not resolve harness for scheduled agent %s", agent.id)
        return None


async def validate_scheduled_permission_support(
    *, permission_mode: str | None, agent: Any, agent_cache: Any
) -> None:
    """Reject modes belonging to a different harness before saving."""
    if permission_mode is None:
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
