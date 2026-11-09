"""Resolve immutable sandbox profiles at session and host launch boundaries."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)


async def prepare_profile(
    request: Request,
    body: Any,
    *,
    user_id: str | None = None,
    inherited_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a selection, or inherit from an already-authorized session."""
    catalog = getattr(request.app.state, "sandbox_profiles", None)
    if body.sandbox_profile_id is not None:
        if catalog is None:
            raise OmnigentError("Sandbox profiles are unavailable", code=ErrorCode.INVALID_INPUT)
        return await asyncio.to_thread(
            catalog.resolve,
            body.sandbox_profile_id,
            expected_revision=body.sandbox_profile_revision,
            provider=body.sandbox_provider,
            user_id=user_id,
        )
    if catalog is None:
        deployment = getattr(request.app.state, "sandbox_config", None)
        if body.host_type == "managed" and deployment is not None:
            entry = (
                deployment.for_provider(body.sandbox_provider)
                if body.sandbox_provider
                else deployment.default
            )
            if entry is not None and entry.managed_launch_supported and entry.manages_profiles:
                raise OmnigentError(
                    "This provider requires sandbox profiles; enable the sandbox_profiles feature",
                    code=ErrorCode.INVALID_INPUT,
                )
        return None
    snapshot = (
        await asyncio.to_thread(catalog.get_session_snapshot, inherited_session_id)
        if inherited_session_id is not None
        and (
            getattr(body, "parent_session_id", None) is not None
            or getattr(body, "side_chat", False)
        )
        else None
    )
    if snapshot is not None:
        snapshot = await asyncio.to_thread(catalog.inherit_snapshot, snapshot, user_id)
        if body.sandbox_provider not in (None, snapshot["provider"]):
            raise OmnigentError(
                "Choose a new sandbox profile to change the inherited provider",
                code=ErrorCode.INVALID_INPUT,
            )
    elif body.host_type == "managed":
        await asyncio.to_thread(catalog.require_selection, body.sandbox_provider, user_id=user_id)
    return snapshot


async def record_profile(
    request: Request,
    session_id: str,
    snapshot: dict[str, Any] | None,
    conversation_store: ConversationStore,
) -> None:
    if snapshot is not None:
        try:
            await asyncio.to_thread(
                request.app.state.sandbox_profiles.bind_session, session_id, snapshot
            )
        except Exception:
            try:
                await conversation_store.delete_conversation(session_id)
            except Exception:
                _logger.exception("Failed to remove session after sandbox profile binding failed")
            raise


async def saved_profile(
    catalog: Any, session_id: str, *, user_id: str | None, host_id: str | None = None
) -> dict[str, Any] | None:
    """Recheck authorization without replacing the accepted provider reference."""
    if catalog is None:
        return None
    if host_id is not None:
        await asyncio.to_thread(catalog.require_session_host, session_id, host_id, user_id=user_id)
    snapshot = await asyncio.to_thread(catalog.get_session_snapshot, session_id)
    if snapshot is not None:
        await asyncio.to_thread(catalog.validate_snapshot, snapshot, user_id=user_id)
    return snapshot
