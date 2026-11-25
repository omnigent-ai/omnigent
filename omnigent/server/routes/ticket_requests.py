"""``GET /v1/inbox/ticket-requests``: project tickets waiting on the viewer's response.

``GET /v1/inbox/ticket-requests/sessions`` maps Inbox sessions to the ticket each
works on, so a card opens the ticket rather than the worker's chat.

A ticket's open ``needs-human`` ask goes to the people who must answer it (see
:func:`omnigent.server.product_approval.human_request_routing`) and leaves every
one of their Inboxes once any of them acts on the ticket or the label is removed.
A person cc'd with ``@name`` in a comment gets a ``reason: "cc"`` entry until they
read the ticket past that comment.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.project_ticket_store import HUMAN_REQUEST_MAX, ProjectTicketStore


def create_ticket_requests_router(
    store: ProjectTicketStore, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    router = APIRouter(prefix="/inbox/ticket-requests")

    @router.get("")
    async def list_ticket_requests(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        asks, cc, answered = await asyncio.gather(
            asyncio.to_thread(store.human_requests, user_id=user_id),
            asyncio.to_thread(store.ticket_cc, user_id=user_id),
            asyncio.to_thread(store.recent_answers, user_id=user_id),
        )
        # A cc is informational (reason "cc") and sorts in with the asks by time; a
        # ticket that also asks the viewer shows once, as the ask. So does one the viewer
        # answered and that asks them again.
        asked = {(item["project_id"], item["ticket_id"]) for item in asks["data"]}
        cc = [item for item in cc if (item["project_id"], item["ticket_id"]) not in asked]
        answered = [
            item for item in answered if (item["project_id"], item["ticket_id"]) not in asked
        ]
        # Open asks and cc's take the cap first, so answered entries never push them out.
        open_items = sorted([*asks["data"], *cc], key=lambda item: item["raised_at"], reverse=True)
        data = [*open_items[:HUMAN_REQUEST_MAX], *answered]
        return {
            **asks,
            "data": data,
            "has_more": asks["has_more"] or len(open_items) > HUMAN_REQUEST_MAX,
        }

    @router.get("/sessions")
    async def list_session_tickets(
        request: Request,
        session_id: Annotated[list[str], Query()],
    ) -> dict[str, Any]:
        """Map Inbox sessions to the one ticket each works on, so cards can open it."""
        user_id = require_user(request, auth_provider)
        tickets = await asyncio.to_thread(store.session_tickets, session_id, user_id=user_id)
        return {"data": tickets}

    return router
