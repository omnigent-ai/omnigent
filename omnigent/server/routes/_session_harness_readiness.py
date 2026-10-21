"""Reject known-unavailable harnesses before creating a session."""

from __future__ import annotations

import asyncio

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_availability import harness_launch_availability
from omnigent.stores import ConversationStore
from omnigent.stores.host_store import HostStore


async def validate_create_harness_readiness(
    *,
    harness: str | None,
    host_id: str | None,
    parent_session_id: str | None,
    inherited_runner_id: str | None,
    user_id: str | None,
    conversation_store: ConversationStore,
    host_store: HostStore | None,
    parent: Conversation | None = None,
) -> None:
    """Check resolved placement after parent authorization and before persistence.

    Children on an existing runner use their ancestor's host. Legacy hosts
    without readiness reports remain unknown; host launch checks still apply.
    """
    from omnigent.server.routes._host_launch import resolve_host_owner

    if host_store is None or not harness or harness == "auto":
        return
    if inherited_runner_id is not None:
        from omnigent.runner.routing import routing_host_id

        if parent is None and parent_session_id:
            parent = await asyncio.to_thread(
                conversation_store.get_conversation, parent_session_id
            )
        host_id = (
            await asyncio.to_thread(
                routing_host_id, parent, conversation_store, max_ancestor_reads=16
            )
            if parent is not None
            else None
        )
        host = await asyncio.to_thread(host_store.get_host, host_id) if host_id else None
    elif host_id:
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
    else:
        host = None
    if host is None:
        return
    # Sharing a parent session does not grant access to its host telemetry.
    if user_id is not None and host.user_id != user_id:
        return
    available, reason = harness_launch_availability(harness, host.configured_harnesses)
    if available is not False:
        return
    raise OmnigentError(
        f"Harness {harness!r} is not configured on the target host ({reason}). "
        "Install or configure the harness on that host, or choose an available harness.",
        code=ErrorCode.HARNESS_NOT_CONFIGURED,
    )
