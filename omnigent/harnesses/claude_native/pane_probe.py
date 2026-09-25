"""Pane turn probe for claude-native: Claude's own session status file."""

from __future__ import annotations

import asyncio

from omnigent.harnesses.claude_native.status_file import RUNNING, read_session_status
from omnigent.runner.native.pane_probe_types import NativeProbeContext, TurnProbe, TurnState


async def probe_pane_turn(ctx: NativeProbeContext) -> TurnProbe | None:
    """Read ``sessions/<pid>.json`` for what Claude is doing right now.

    Cheap (one local file read), so it also answers ``cheap_only`` probes.
    ``waiting`` means a dialog owns the input; ``busy`` means the turn runs.

    :param ctx: Probe context for the session.
    :returns: The probe answer; UNKNOWN until the poller resolves the file.
    """
    path = ctx.resource_registry.status_poller_path(ctx.session_id)
    if path is None:
        return TurnProbe(TurnState.UNKNOWN, "vendor", detail="status file unresolved")
    status = await asyncio.to_thread(read_session_status, path)
    if status is None:
        return TurnProbe(TurnState.UNKNOWN, "vendor", detail="status file unreadable")
    detail = f"status file: {status.raw_status}"
    if status.raw_status == "waiting":
        return TurnProbe(
            TurnState.PARKED, "vendor", blocked_on=status.blocked_on or "dialog", detail=detail
        )
    if status.runner_status == RUNNING:
        return TurnProbe(TurnState.ACTIVE, "vendor", detail=detail)
    return TurnProbe(TurnState.INACTIVE, "vendor", detail=detail)
