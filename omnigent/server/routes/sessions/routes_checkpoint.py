"""Framework checkpoint endpoints for session recovery."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import telemetry
from omnigent.runtime.session_checkpoint import CHECKPOINT_KEY, SessionCheckpoint
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.schemas import (
    SessionCheckpointReplaceRequest,
    SessionCheckpointResponse,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def _checkpoint_attributes(
    checkpoint: SessionCheckpoint | None,
    *,
    session_id: str,
    outcome: str,
    latency_ms: float,
) -> dict[str, Any]:
    """Build tool-observation attributes without exposing checkpoint content."""
    return {
        "openinference.span.kind": "TOOL",
        "openinference.tool.name": "session_checkpoint",
        "tool.name": "session_checkpoint",
        "session.id": session_id,
        "outcome": outcome,
        "checkpoint.outcome": outcome,
        "latency_ms": latency_ms,
        "checkpoint.latency_ms": latency_ms,
        "status": checkpoint.status if checkpoint is not None else "absent",
        "checkpoint.status": checkpoint.status if checkpoint is not None else "absent",
        "phase": checkpoint.phase if checkpoint is not None else "absent",
        "checkpoint.phase": checkpoint.phase if checkpoint is not None else "absent",
        "covered_item_count": len(checkpoint.covered_items) if checkpoint is not None else 0,
        "checkpoint.covered_item_count": len(checkpoint.covered_items)
        if checkpoint is not None
        else 0,
    }


def _record_checkpoint_payload(
    span: Any,
    *,
    key: str,
    payload: dict[str, Any],
) -> None:
    """Attach a capped checkpoint payload only when content capture is enabled."""
    if telemetry.should_capture_content() and getattr(span, "is_recording", lambda: False)():
        span.set_attribute(key, telemetry.redact_and_cap_payload(payload))


def register_checkpoint_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register isolated framework checkpoint routes."""

    async def _authorized_session_state(
        request: Request,
        session_id: str,
        required_level: int,
    ) -> dict[str, Any]:
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            required_level,
            permission_store,
            conversation_store,
        )
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        return dict(conversation.session_state)

    async def _load_authorized_checkpoint(
        request: Request,
        session_id: str,
        required_level: int,
    ) -> SessionCheckpoint | None:
        raw = (await _authorized_session_state(request, session_id, required_level)).get(
            CHECKPOINT_KEY
        )
        if raw is None:
            return None
        try:
            checkpoint = SessionCheckpoint.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise OmnigentError(
                "Stored framework checkpoint is invalid",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        if checkpoint.session_id != session_id:
            raise OmnigentError(
                "Stored framework checkpoint belongs to another session",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return checkpoint

    @router.get(
        "/sessions/{session_id}/checkpoint",
        response_model=SessionCheckpointResponse,
    )
    async def get_session_checkpoint(
        request: Request,
        session_id: str,
    ) -> SessionCheckpointResponse:
        started = time.perf_counter()
        checkpoint: SessionCheckpoint | None = None
        outcome = "error"
        with telemetry.span(
            "session_checkpoint.read",
            attributes=_checkpoint_attributes(
                None,
                session_id=session_id,
                outcome="started",
                latency_ms=0,
            ),
        ) as span:
            _record_checkpoint_payload(
                span,
                key="input.value",
                payload={"session_id": session_id},
            )
            try:
                checkpoint = await _load_authorized_checkpoint(request, session_id, LEVEL_READ)
                outcome = "success" if checkpoint is not None else "absent"
                _record_checkpoint_payload(
                    span,
                    key="output.value",
                    payload={
                        "session_id": session_id,
                        "checkpoint": checkpoint.model_dump(mode="json")
                        if checkpoint is not None
                        else None,
                    },
                )
                return SessionCheckpointResponse(session_id=session_id, checkpoint=checkpoint)
            except Exception as exc:
                telemetry.record_error(span, exc)
                raise
            finally:
                for key, value in _checkpoint_attributes(
                    checkpoint,
                    session_id=session_id,
                    outcome=outcome,
                    latency_ms=(time.perf_counter() - started) * 1000,
                ).items():
                    span.set_attribute(key, value)

    @router.put(
        "/sessions/{session_id}/checkpoint",
        response_model=SessionCheckpointResponse,
    )
    async def replace_session_checkpoint(
        request: Request,
        session_id: str,
        body: SessionCheckpointReplaceRequest,
    ) -> SessionCheckpointResponse:
        if body.checkpoint is not None and body.checkpoint.session_id != session_id:
            raise OmnigentError(
                "checkpoint session_id must match the route session_id",
                code=ErrorCode.INVALID_INPUT,
            )
        started = time.perf_counter()
        checkpoint = body.checkpoint
        outcome = "error"
        with telemetry.span(
            "session_checkpoint.write",
            attributes=_checkpoint_attributes(
                checkpoint,
                session_id=session_id,
                outcome="started",
                latency_ms=0,
            ),
        ) as span:
            _record_checkpoint_payload(
                span,
                key="input.value",
                payload={
                    "session_id": session_id,
                    "checkpoint": checkpoint.model_dump(mode="json")
                    if checkpoint is not None
                    else None,
                },
            )
            try:
                await _authorized_session_state(request, session_id, LEVEL_EDIT)
                stored_checkpoint = (
                    checkpoint.model_dump(mode="json") if checkpoint is not None else None
                )
                await asyncio.to_thread(
                    conversation_store.set_session_state_key,
                    session_id,
                    CHECKPOINT_KEY,
                    stored_checkpoint,
                )
                outcome = "success"
                _record_checkpoint_payload(
                    span,
                    key="output.value",
                    payload={"session_id": session_id, "checkpoint": stored_checkpoint},
                )
                return SessionCheckpointResponse(session_id=session_id, checkpoint=checkpoint)
            except Exception as exc:
                telemetry.record_error(span, exc)
                raise
            finally:
                for key, value in _checkpoint_attributes(
                    checkpoint,
                    session_id=session_id,
                    outcome=outcome,
                    latency_ms=(time.perf_counter() - started) * 1000,
                ).items():
                    span.set_attribute(key, value)
