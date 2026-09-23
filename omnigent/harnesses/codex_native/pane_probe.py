"""Pane turn probe for codex-native: the app-server's own thread status."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from omnigent.runner.native.pane_probe_types import NativeProbeContext, TurnProbe, TurnState

_logger = logging.getLogger(__name__)


async def probe_pane_turn(ctx: NativeProbeContext) -> TurnProbe | None:
    """Ask the session's Codex app-server whether its thread is working.

    Uses the read-only ``thread/read`` (never ``thread/resume``, which would
    load or subscribe to the thread). A refused or missing app-server means no
    agent can be working. When the app-server does not answer usably, falls
    back to the bridge's ``active_turn_id`` while the forwarder is alive.

    After a ``/clear`` rotation the bridge state names the new session, whose
    thread runs in this session's app-server; that thread is read too.

    :param ctx: Probe context for the session.
    :returns: The probe answer, or ``None`` for a cheap-only probe.
    """
    if ctx.cheap_only:
        return None
    from omnigent.harnesses.codex_native.app_server import client_for_transport
    from omnigent.harnesses.codex_native.bridge import (
        CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
        read_bridge_state,
    )

    bridge_id = await ctx.bridge_id(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY)
    state = read_bridge_state(bridge_dir_for_bridge_id(bridge_id))
    if state is None or not _serves_thread_of(ctx, state.session_id):
        return TurnProbe(TurnState.UNKNOWN, "inferred", detail="no codex bridge state")

    def _inferred(reason: str) -> TurnProbe:
        if state.active_turn_id is not None and ctx.forwarder_alive:
            return TurnProbe(TurnState.ACTIVE, "inferred", detail=f"{reason}; turn in bridge")
        return TurnProbe(TurnState.UNKNOWN, "inferred", detail=reason)

    client = client_for_transport(
        state.socket_path, client_name="omnigent-codex-native-pane-probe"
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=ctx.timeout_s)
        envelope = await asyncio.wait_for(
            client.request("thread/read", {"threadId": state.thread_id, "includeTurns": False}),
            timeout=ctx.timeout_s,
        )
    except (ConnectionRefusedError, FileNotFoundError):
        return TurnProbe(TurnState.INACTIVE, "vendor", detail="app-server not listening")
    except Exception as exc:  # noqa: BLE001 - any failure falls back to inference.
        _logger.debug("codex pane probe thread/read failed: %r", exc)
        return _inferred(f"thread/read failed: {type(exc).__name__}")
    finally:
        with contextlib.suppress(Exception):
            await client.close()
    return _thread_status_probe(envelope) or _inferred("thread/read: no status")


def _serves_thread_of(ctx: NativeProbeContext, state_session_id: str) -> bool:
    """Whether *ctx*'s session runs the thread of the session a bridge state names.

    Its own, or a session a ``/clear`` rotation moved its TUI to: while that
    TUI lives, and after it was lost with the new session's status kept for
    this session's app-server.
    """
    if state_session_id == ctx.session_id:
        return True
    registry = ctx.resource_registry
    return ctx.session_id in (
        registry.sidecar_home(state_session_id),
        registry.vendor_turn_home(state_session_id),
    )


def _thread_status_probe(envelope: object) -> TurnProbe | None:
    """Map a ``thread/read`` response envelope to a probe, or ``None``."""
    result = envelope.get("result") if isinstance(envelope, dict) else None
    thread = result.get("thread") if isinstance(result, dict) else None
    status = thread.get("status") if isinstance(thread, dict) else None
    kind = status.get("type") if isinstance(status, dict) else None
    if kind in ("idle", "notLoaded", "systemError"):
        return TurnProbe(TurnState.INACTIVE, "vendor", detail=f"thread/read: {kind}")
    if kind != "active":
        return None
    flags = status.get("activeFlags") if isinstance(status, dict) else None
    waiting = [f for f in flags if isinstance(f, str)] if isinstance(flags, list) else []
    if waiting:
        return TurnProbe(
            TurnState.PARKED, "vendor", blocked_on=waiting[0], detail="thread/read: active"
        )
    return TurnProbe(TurnState.ACTIVE, "vendor", detail="thread/read: active")
