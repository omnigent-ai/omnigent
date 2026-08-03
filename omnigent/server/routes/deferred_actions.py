"""
REST API routes for listing, viewing, approving, and rejecting deferred actions.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from omnigent.runtime.deferred.manager import (
    DeferredActionError,
    DeferredActionManager,
    DeferredExpiredError,
    DeferredStateError,
    HashDriftError,
)
from omnigent.runtime.deferred.store import get_deferred_store

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["deferred_actions"])


class ApproveRequest(BaseModel):
    """Payload for approving a deferred action."""

    actor: str = "user"
    current_base_hash: str


class RejectRequest(BaseModel):
    """Payload for rejecting a deferred action."""

    actor: str = "user"
    reason: str | None = None


@router.get("/v1/sessions/{session_id}/deferred_actions")
async def list_deferred_actions(session_id: str) -> dict[str, Any]:
    """List all deferred actions for a given session."""
    store = get_deferred_store()
    actions = await store.list_actions_for_session(session_id)
    return {"deferred_actions": [action.to_dict() for action in actions]}


@router.get("/v1/deferred_actions/{action_id}")
async def get_deferred_action(action_id: str) -> dict[str, Any]:
    """Retrieve details and audit trail events for a deferred action."""
    store = get_deferred_store()
    action = await store.get_action(action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deferred action {action_id!r} not found",
        )
    events = await store.list_audit_events(action_id)
    return {
        "action": action.to_dict(),
        "audit_events": [
            {
                "id": evt.id,
                "action_id": evt.action_id,
                "event_type": evt.event_type,
                "timestamp": evt.timestamp,
                "actor": evt.actor,
                "details": evt.details,
            }
            for evt in events
        ],
    }


@router.post("/v1/deferred_actions/{action_id}/approve")
async def approve_deferred_action(
    action_id: str,
    payload: ApproveRequest,
) -> dict[str, Any]:
    """
    Approve a deferred action.

    Verifies expiration and base hash equality, transitioning status to APPROVED.
    Returns immediately without blocking on tool execution.
    """
    manager = DeferredActionManager()
    try:
        action = await manager.approve(
            action_id,
            actor=payload.actor,
            current_base_hash=payload.current_base_hash,
        )
        return {"status": "success", "action": action.to_dict()}
    except DeferredExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=str(exc),
        ) from exc
    except HashDriftError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (DeferredStateError, DeferredActionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("/v1/deferred_actions/{action_id}/reject")
async def reject_deferred_action(
    action_id: str,
    payload: RejectRequest,
) -> dict[str, Any]:
    """Reject a pending deferred action."""
    manager = DeferredActionManager()
    try:
        action = await manager.reject(
            action_id,
            actor=payload.actor,
            reason=payload.reason,
        )
        return {"status": "success", "action": action.to_dict()}
    except (DeferredStateError, DeferredActionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
