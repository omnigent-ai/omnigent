"""The caller's defaults for new sessions on each of their hosts.

``/v1/hosts/{host_id}/session-defaults`` reads and replaces them. They live in
the owner's ``preferences`` rows (see
:meth:`omnigent.stores.user_settings_store.UserSettingsStore.get_host_session_defaults`),
so a host keeps them while offline. Session create applies them through
:func:`omnigent.server.routes._session_create_validation.resolve_project_session_create`.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from pydantic import ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.proactive_agent import reject_proactive_agent_id
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.schemas import HostSessionDefaults
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.user_settings_store import UserSettingsStore

_logger = logging.getLogger(__name__)


def create_host_session_defaults_router(
    host_store: HostStore,
    settings_store: UserSettingsStore,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    *,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the owner-only host session-defaults router.

    :param host_store: Registered hosts, for the ownership check.
    :param settings_store: Where the defaults persist.
    :param agent_store: Resolves a default ``agent_id`` before it is saved.
    :param conversation_store: Authorizes a session-scoped default agent.
    :param permission_store: Authorizes a session-scoped default agent.
    :param auth_provider: Identifies the caller; ``None`` is single-user mode.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    async def owned_host_id(request: Request, host_id: str) -> tuple[str | None, str, bool]:
        """Authorize the caller and return ``(user_id, host_id, is_managed)``."""
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        return user_id, host.host_id, host.sandbox_provider is not None

    @router.get("/hosts/{host_id}/session-defaults", response_model=HostSessionDefaults)
    async def get_host_session_defaults(request: Request, host_id: str) -> HostSessionDefaults:
        """Read the caller's defaults for new sessions on one of their hosts."""
        user_id, resolved_host_id, _managed = await owned_host_id(request, host_id)
        stored = await asyncio.to_thread(
            settings_store.get_host_session_defaults, resolved_host_id, user_id=user_id
        )
        known = {
            field: stored[field] for field in HostSessionDefaults.model_fields if field in stored
        }
        try:
            return HostSessionDefaults.model_validate(known)
        except ValidationError:
            _logger.warning("stored host session defaults no longer validate — ignoring them")
            return HostSessionDefaults()

    @router.put("/hosts/{host_id}/session-defaults", response_model=HostSessionDefaults)
    async def save_host_session_defaults(
        request: Request, host_id: str, body: HostSessionDefaults
    ) -> HostSessionDefaults:
        """Replace the caller's defaults for one of their hosts; ``{}`` clears them."""
        user_id, resolved_host_id, managed = await owned_host_id(request, host_id)
        if managed:
            raise OmnigentError(
                "Session defaults apply to hosts you connect, not managed sandboxes",
                code=ErrorCode.INVALID_INPUT,
            )
        if body.agent_id is not None:
            reject_proactive_agent_id(body.agent_id)
            # Refuse an agent the caller could not start a session with.
            await validate_session_agent(
                user_id=user_id,
                agent_id=body.agent_id,
                agent_store=agent_store,
                permission_store=permission_store,
                conversation_store=conversation_store,
            )
        try:
            await asyncio.to_thread(
                settings_store.save_host_session_defaults,
                resolved_host_id,
                body.model_dump(exclude_none=True),
                user_id=user_id,
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        return body

    return router
