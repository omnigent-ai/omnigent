"""Pane turn probe for antigravity-native: agy's own cascade run status."""

from __future__ import annotations

import asyncio

import httpx

from omnigent.runner.native.pane_probe_types import NativeProbeContext, TurnProbe, TurnState


async def probe_pane_turn(ctx: NativeProbeContext) -> TurnProbe | None:
    """Ask agy whether the session's cascade is still running.

    agy reports RUNNING both while working and while a permission gate is
    parked, so RUNNING reads ACTIVE (never idle under a live gate).

    :param ctx: Probe context for the session.
    :returns: The probe answer, or ``None`` for a cheap-only probe.
    """
    if ctx.cheap_only:
        return None
    from omnigent.harnesses.antigravity_native.bridge import (
        ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )
    from omnigent.harnesses.antigravity_native.reader import (
        _cascade_is_idle,
        _resolve_cascade_id,
        _resolve_rpc_port,
    )
    from omnigent.harnesses.antigravity_native.rpc import get_all_cascade_trajectories

    bridge_id = await ctx.bridge_id(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY)
    cascade_id = _resolve_cascade_id(bridge_dir_for_bridge_id(bridge_id))
    if cascade_id is None:
        return TurnProbe(TurnState.UNKNOWN, "vendor", detail="cascade unresolved")
    port = await asyncio.to_thread(_resolve_rpc_port, cascade_id)
    if port is None:
        return TurnProbe(TurnState.UNKNOWN, "vendor", detail="rpc port unresolved")
    try:
        body = await asyncio.wait_for(
            asyncio.to_thread(get_all_cascade_trajectories, port), timeout=ctx.timeout_s
        )
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        return TurnProbe(TurnState.UNKNOWN, "vendor", detail=f"rpc failed: {type(exc).__name__}")
    summaries = body.get("trajectorySummaries") if isinstance(body, dict) else None
    if not isinstance(summaries, dict) or not isinstance(summaries.get(cascade_id), dict):
        return TurnProbe(TurnState.UNKNOWN, "vendor", detail="cascade not reported")
    if _cascade_is_idle(summaries, cascade_id):
        return TurnProbe(TurnState.INACTIVE, "vendor", detail="cascade idle")
    return TurnProbe(TurnState.ACTIVE, "vendor", detail="cascade running")
