"""``GET /v1/inbox/ticket-requests``: project tickets waiting on the viewer's response.

A ticket's open ``needs-human`` ask goes to the people who must answer it (see
:func:`omnigent.server.product_approval.human_request_routing`) and leaves every
one of their Inboxes once any of them acts on the ticket or the label is removed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.project_ticket_store import ProjectTicketStore


def create_ticket_requests_router(
    store: ProjectTicketStore, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    router = APIRouter(prefix="/inbox/ticket-requests")

    @router.get("")
    async def list_ticket_requests(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(store.human_requests, user_id=user_id)

    return router
