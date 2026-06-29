"""REST route for the usage dashboard.

``GET /v1/usage`` aggregates the ``session_usage`` of the conversations the
caller can access into one token/cost summary (totals + per-model). Read-only;
requires authentication in multi-user mode.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.usage_summary import aggregate_usage
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore

# Bound the scan so a large history can't make the endpoint unbounded. Each
# page is 200 conversations → up to 10k conversations aggregated.
_PAGE_SIZE = 200
_MAX_PAGES = 50


def _collect_usages(store: ConversationStore, user_id: str | None) -> list[dict[str, Any]]:
    """Page through accessible conversations, collecting non-empty usage blobs.

    :param store: The conversation store.
    :param user_id: Restrict to conversations accessible by this user, or
        ``None`` (single-user) for all.
    :returns: A list of ``session_usage`` dicts.
    """
    usages: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(_MAX_PAGES):
        page = store.list_conversations(
            limit=_PAGE_SIZE,
            after=after,
            kind=None,  # all kinds — include sub-agent conversations' usage
            accessible_by=user_id,
            include_archived=True,
        )
        for conv in page.data:
            if conv.session_usage:
                usages.append(conv.session_usage)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return usages


def create_usage_router(
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the usage router (mounted at ``/usage``).

    :param conversation_store: Source of per-conversation ``session_usage``.
    :param auth_provider: Auth provider identifying the caller, or ``None``.
    :param permission_store: When set, enables auth enforcement (multi-user).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get("/usage")
    async def get_usage(request: Request) -> dict[str, Any]:
        """Return aggregated token/cost usage across accessible conversations."""
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        usages = await asyncio.to_thread(_collect_usages, conversation_store, user_id)
        summary = aggregate_usage(usages)
        return {"object": "usage", "conversations": len(usages), **summary}

    return router
