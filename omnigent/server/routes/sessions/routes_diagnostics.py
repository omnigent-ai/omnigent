"""Bounded browser observations for native-session stall investigations."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.debug_logging import debug_event
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)
_MAX_BODY_BYTES = 32_768
_Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
_Status = Literal["idle", "launching", "running", "waiting", "failed", "unknown"]
_Blocked = Literal["none", "permission_prompt", "dialog_open", "other", "unknown"]


class BrowserDiagnosticEvent(BaseModel):
    """Allowlisted observations; never accepts prompts, tool inputs, or arbitrary text."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_name: Literal[
        "browser_approval_received",
        "browser_approval_applied",
        "browser_approval_rendered",
        "browser_approval_visibility",
        "browser_approval_verdict_submitted",
        "browser_approval_verdict_request_completed",
        "browser_status_received",
        "browser_status_applied",
        "browser_status_displayed",
        "browser_status_reconnect",
    ]
    sequence: int = Field(ge=1, le=2**53 - 1)
    client_time_ms: int = Field(ge=0, le=2**53 - 1)
    target_session_id: _Identifier | None = None
    elicitation_id: _Identifier | None = None
    card_instance_id: _Identifier | None = None
    status: _Status | None = None
    previous_status: _Status | None = None
    blocked_on: _Blocked | None = None
    previous_blocked_on: _Blocked | None = None
    snapshot_blocked_on: _Blocked | None = None
    actionable: bool | None = None
    in_view: bool | None = None
    tab_visible: bool | None = None
    status_indicator_visible: bool | None = None
    action: Literal["accept", "decline", "cancel"] | None = None
    outcome: Literal["success", "error"] | None = None
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    observation_source: Literal["stream", "snapshot", "card"] | None = None
    observation_trigger: Literal["periodic_reconcile", "stream_reconnect"] | None = None


class BrowserDiagnosticsBatch(BaseModel):
    """One small, best-effort batch from one browser tab."""

    model_config = ConfigDict(extra="forbid")

    client_instance_id: UUID
    client_bundle: _Identifier | None = None
    # Cumulative across all sessions observed by this browser instance.
    dropped_events: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] = 0
    events: list[BrowserDiagnosticEvent] = Field(min_length=1, max_length=20)


def register_diagnostics_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the authenticated browser diagnostics sink."""

    @router.post("/sessions/{session_id}/diagnostics", include_in_schema=False, status_code=204)
    async def record_browser_diagnostics(request: Request, session_id: str) -> Response:
        user_id = require_user(request, auth_provider)
        await require_access(user_id, session_id, LEVEL_READ, permission_store, conversation_store)
        if await asyncio.to_thread(conversation_store.get_conversation, session_id) is None:
            raise HTTPException(404, "Session not found")

        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_BODY_BYTES:
                raise HTTPException(413, "Diagnostic batch too large")
            raw.extend(chunk)
        try:
            batch = BrowserDiagnosticsBatch.model_validate_json(raw)
        except ValidationError:
            # Validation errors can echo rejected values; diagnostics never reflect input text.
            raise HTTPException(422, "Invalid diagnostic batch") from None

        targets = {event.target_session_id for event in batch.events} - {None, session_id}
        for target in targets:
            assert target is not None
            await require_access(user_id, target, LEVEL_READ, permission_store, conversation_store)
            if await asyncio.to_thread(conversation_store.get_conversation, target) is None:
                raise HTTPException(404, "Session not found")

        for event in batch.events:
            attributes = event.model_dump(exclude_none=True, exclude={"event_name", "sequence"})
            if batch.client_bundle is not None:
                attributes["client_bundle"] = batch.client_bundle
            _logger.info(
                "Browser session observation",
                extra=debug_event(
                    event.event_name,
                    session_id=session_id,
                    user_id=user_id,
                    emitter="browser",
                    client_instance_id=str(batch.client_instance_id),
                    client_sequence=event.sequence,
                    dropped_events=batch.dropped_events,
                    **attributes,
                ),
            )
        return Response(status_code=204)
