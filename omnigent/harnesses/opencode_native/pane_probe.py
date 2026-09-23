"""Pane turn probe for opencode-native: pending permissions plus bridge state."""

from __future__ import annotations

import asyncio

from omnigent.runner.native.pane_probe_types import NativeProbeContext, TurnProbe, TurnState


async def probe_pane_turn(ctx: NativeProbeContext) -> TurnProbe | None:
    """Ask the session's ``opencode serve`` for pending permissions, then infer.

    A permission pending for this session's opencode session is a parked
    prompt (vendor). Otherwise the forwarder's bridge status decides: ``busy``
    with a live forwarder reads ACTIVE, ``idle`` reads INACTIVE (inferred).

    :param ctx: Probe context for the session.
    :returns: The probe answer, or ``None`` for a cheap-only probe.
    """
    if ctx.cheap_only:
        return None
    from omnigent.harnesses.opencode_native.bridge import (
        OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
        read_bridge_state,
    )
    from omnigent.harnesses.opencode_native.client import OpenCodeClient

    bridge_id = await ctx.bridge_id(OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY)
    state = read_bridge_state(bridge_dir_for_bridge_id(bridge_id))
    if state is None:
        return TurnProbe(TurnState.UNKNOWN, "inferred", detail="no opencode bridge state")
    try:
        async with OpenCodeClient(
            state.server_base_url, headers=state.auth_headers(), directory=state.workspace
        ) as client:
            permissions = await asyncio.wait_for(client.list_permissions(), timeout=ctx.timeout_s)
    except Exception:  # noqa: BLE001 - an unreachable serve falls back to inference.
        permissions = None
    if permissions is not None:
        for permission in permissions:
            owner = permission.get("sessionID") or permission.get("session_id")
            if owner == state.opencode_session_id:
                return TurnProbe(
                    TurnState.PARKED, "vendor", blocked_on="permission", detail="GET /permission"
                )
    if state.status == "busy" and state.active_message_id and ctx.forwarder_alive:
        return TurnProbe(TurnState.ACTIVE, "inferred", detail="bridge busy")
    if state.status == "idle":
        return TurnProbe(TurnState.INACTIVE, "inferred", detail="bridge idle")
    return TurnProbe(TurnState.UNKNOWN, "inferred", detail=f"bridge {state.status}")
