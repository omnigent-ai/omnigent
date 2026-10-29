"""Route helpers that begin, finish and cancel a session's account move."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import HTTPException, Request

from omnigent.entities import Conversation
from omnigent.harness_aliases import canonicalize_harness
from omnigent.server.host_accounts import (
    account_registry,
    validate_account_selection,
    validate_target_workspace,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.schemas import UpdateSessionRequest
from omnigent.stores import ConversationStore


async def confirm_runner_stopped(
    request: Request,
    host_registry: HostRegistry,
    session_id: str,
    host_id: str,
    runner_id: str,
) -> bool:
    """Return whether the host confirms ``runner_id`` is stopped, stopping it if needed."""
    # Imported late: the sessions package imports the hosts router.
    from omnigent.server.routes import sessions as sessions_facade

    conn = host_registry.get(host_id)
    if conn is None:
        return False
    status = await sessions_facade._query_host_runner_status(conn, host_registry, runner_id)
    router = getattr(request.app.state, "runner_router", None)
    if status in {"dead", "unknown"} and not (
        router is not None and router.runner_is_online(runner_id)
    ):
        return True
    return await sessions_facade._stop_session_host_runner(
        session_id, host_id, runner_id, host_registry
    )


async def retire_transition_runners(
    request: Request,
    host_registry: HostRegistry,
    conversation_store: ConversationStore,
    session_id: str,
    host_id: str,
    marker: dict[str, Any],
) -> None:
    """Stop the runners an unfinished account move left behind.

    An earlier launch attempt must be confirmed stopped before it is released.
    The pre-move runner blocks the launch only when it ran on the destination host.

    :raises HTTPException: 409 when a runner that must stop has not stopped.
    """
    attempt = marker.get("launch_runner_id")
    if attempt:
        if not await confirm_runner_stopped(request, host_registry, session_id, host_id, attempt):
            raise HTTPException(status_code=409, detail="Earlier launch has not stopped; retry")
        await asyncio.to_thread(
            conversation_store.reset_account_launch, session_id, marker["id"], attempt
        )
    previous_runner = marker.get("previous_runner_id")
    if (
        previous_runner
        and marker.get("previous_host_id") == host_id
        and not await confirm_runner_stopped(
            request, host_registry, session_id, host_id, previous_runner
        )
    ):
        raise HTTPException(
            status_code=409, detail="Previous runner has not stopped; retry the switch"
        )


async def begin_account_transition(
    request: Request,
    body: UpdateSessionRequest,
    conv: Conversation,
    harness: str | None,
    *,
    user_id: str | None,
    conversation_store: ConversationStore,
    host_registry: HostRegistry | None,
) -> Conversation:
    """Validate a destination, record it and detach the runner.

    :param body: Update carrying ``target_*`` fields and ``runner_id=""``.
    :param harness: The session's current native harness.
    :returns: The detached session with its ``account_transition``.
    :raises HTTPException: 400 for an invalid destination, 409 when the session
        is busy, changed or shares a runner it does not own.
    """
    from omnigent.server.routes._sessions.helpers import (
        _owns_host_runner,
        _session_status_from_cache,
        _spawn_superseded_runner_stop,
    )
    from omnigent.server.routes._sessions.orchestration import _session_is_starting

    if (
        account_registry(request) is None
        or body.target_host_id is None
        or body.runner_id != ""
        or not body.account_id
        or not body.target_harness
        or body.target_model is None
    ):
        raise HTTPException(
            400,
            "An account destination needs a configured registry, an explicit detach, "
            "an account, a harness and a model",
        )
    if (canonicalize_harness(body.target_harness) or body.target_harness) != harness:
        raise HTTPException(400, "A session keeps its harness; fork it to use another provider")
    validate_target_workspace(body.target_workspace)
    if _session_status_from_cache(conv.id, conv.live_status) == "running" or _session_is_starting(
        conv.id
    ):
        raise HTTPException(409, "Session is busy")
    if conv.parent_conversation_id is not None:
        raise HTTPException(409, "A sub-agent cannot be moved on its own; move its parent")
    owns_runner = await _owns_host_runner(conv, conversation_store)
    if conv.runner_id is not None and not owns_runner:
        raise HTTPException(409, "This session has no host binding, so it cannot be moved")
    host_store = getattr(request.app.state, "host_store", None)
    if host_store is None:
        raise HTTPException(503, "Host store unavailable")
    await asyncio.to_thread(
        resolve_host_owner,
        user_id=user_id,
        host_id=body.target_host_id,
        host_store=host_store,
    )
    destination = await validate_account_selection(
        request,
        host_id=body.target_host_id,
        harness=body.target_harness,
        model=body.target_model,
        effort=body.target_effort,
        host_registry=host_registry,
        account_id=body.account_id,
    )
    transition = {
        "id": secrets.token_hex(16),
        "account_id": body.account_id,
        "host_id": body.target_host_id,
        "harness": destination.harness if destination else body.target_harness,
        "model": body.target_model,
        "effort": body.target_effort,
        "workspace": body.target_workspace,
        # Restored if the move is cancelled, so a later relaunch keeps the old settings.
        "previous_model": conv.model_override,
        "previous_effort": conv.reasoning_effort,
    }
    updated = await asyncio.to_thread(
        conversation_store.begin_account_transition,
        conv.id,
        transition,
        expected_runner_id=conv.runner_id,
    )
    if owns_runner and host_registry is not None and conv.host_id and conv.runner_id:
        # The detached session no longer serves this runner; stop it in the background.
        _spawn_superseded_runner_stop(conv.id, conv.host_id, conv.runner_id, host_registry)
    return updated


async def abandon_account_transition(
    request: Request,
    body: UpdateSessionRequest,
    conv: Conversation,
    *,
    conversation_store: ConversationStore,
    host_registry: HostRegistry | None,
) -> Conversation:
    """Cancel a pending move: stop its launch attempt, clear the marker, stay detached.

    The model and effort return to their pre-move values when the session is back on
    its old host. The attempt's stop is
    confirmed only while it is still bound; an attempt already unbound (a failed
    launch cleaned it up) is stopped best-effort, so a cancel never waits on an
    offline host.

    :raises HTTPException: 400 for extra fields, 409 when no matching move is
        pending, or a still-bound launch attempt has not stopped (retry).
    """
    # Imported late: the hosts router imports this module.
    from omnigent.server.routes._sessions.helpers import _spawn_superseded_runner_stop
    from omnigent.server.routes.hosts import _runner_launch_locks

    async with _runner_launch_locks.setdefault(conv.id, asyncio.Lock()):
        fresh = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
        marker = fresh.account_transition if fresh is not None else None
        if fresh is None or marker is None or body.transition_id != marker["id"]:
            raise HTTPException(409, "Account change is stale")
        attempt = marker.get("launch_runner_id")
        if attempt and fresh.runner_id == attempt:
            if host_registry is None or not await confirm_runner_stopped(
                request, host_registry, conv.id, marker["host_id"], attempt
            ):
                raise HTTPException(409, "The destination runner has not stopped; retry")
        elif attempt and host_registry is not None and host_registry.get(marker["host_id"]):
            _spawn_superseded_runner_stop(conv.id, marker["host_id"], attempt, host_registry)
        updated = await asyncio.to_thread(
            conversation_store.abandon_account_transition,
            conv.id,
            marker["id"],
            expected_launch_runner_id=attempt,
        )
    # Only a session that is back on its old host returns to the old settings; one a
    # launch attempt left on the destination host keeps the destination's.
    if (
        "previous_model" in marker
        and updated.host_id == marker.get("previous_host_id")
        and (
            updated.model_override != marker["previous_model"]
            or updated.reasoning_effort != marker["previous_effort"]
        )
    ):
        await asyncio.to_thread(
            conversation_store.update_conversation,
            conv.id,
            model_override=marker["previous_model"],
            _unset_model_override=marker["previous_model"] is None,
            reasoning_effort=marker["previous_effort"],
            _unset_reasoning_effort=marker["previous_effort"] is None,
        )
        updated = await asyncio.to_thread(conversation_store.get_conversation, conv.id) or updated
    return updated
