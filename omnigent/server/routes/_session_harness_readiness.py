"""Reject known-unavailable harnesses before creating a session."""

from __future__ import annotations

import asyncio
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_availability import reported_harness_availability
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
    inference_snapshot: dict[str, Any] | None,
) -> None:
    """Check resolved placement after parent authorization and before persistence.

    Children on an existing runner use their ancestor's host. Legacy hosts
    without readiness reports remain unknown; host launch checks still apply.
    """
    from omnigent.server.routes._host_launch import resolve_host_owner
    from omnigent.server.routes.sandbox_inference import configured_snapshot

    if host_store is None or not harness or harness == "auto":
        return
    if inherited_runner_id is not None:
        host_id = None
        seen: set[str] = set()
        while parent_session_id and parent_session_id not in seen:
            seen.add(parent_session_id)
            parent = await asyncio.to_thread(
                conversation_store.get_conversation, parent_session_id
            )
            if parent is None:
                break
            if parent.host_id:
                host_id = parent.host_id
                break
            parent_session_id = parent.parent_conversation_id
        host = await asyncio.to_thread(host_store.get_host, host_id) if host_id else None
    elif host_id:
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
    else:
        host = None
    if host is None:
        return
    available, reason = reported_harness_availability(harness, host.configured_harnesses)
    if available is not False:
        return
    if reason == "needs-auth" and configured_snapshot(inference_snapshot):
        return
    raise OmnigentError(
        f"Harness {harness!r} is not configured on the target host ({reason}). "
        "Install or configure the harness on that host, or choose an available harness.",
        code=ErrorCode.HARNESS_NOT_CONFIGURED,
    )
