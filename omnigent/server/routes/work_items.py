"""REST routes for work-items (Tasks).

CRUD over the ``work_items`` table at ``/work-items[/{work_item_id}]``.
Work items are global to all signed-in users (the row carries
``created_by``/``assignee_user_id`` for future per-user scoping); in
multi-user mode every endpoint requires authentication, but none requires
admin. Creation is idempotent by ``dedup_key`` so external intake (Slack,
email, GitHub, Jira) can safely retry.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.db.utils import generate_work_item_id
from omnigent.entities import WORK_ITEM_SOURCES, WORK_ITEM_STATUSES, WorkItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.work_item_store import WorkItemStore

_SOURCES = sorted(WORK_ITEM_SOURCES)
_STATUSES = sorted(WORK_ITEM_STATUSES)


class CreateWorkItemBody(BaseModel):
    """Request body for ``POST /work-items``."""

    title: str
    source: str
    body: str | None = None
    dedup_key: str | None = None
    external_id: str | None = None
    status: str = "new"
    conversation_id: str | None = None
    assignee_user_id: str | None = None
    plan: str | None = None


class UpdateWorkItemBody(BaseModel):
    """Request body for ``PATCH /work-items/{id}``. Unset fields are unchanged."""

    title: str | None = None
    body: str | None = None
    status: str | None = None
    pr_url: str | None = None
    conversation_id: str | None = None
    assignee_user_id: str | None = None
    plan: str | None = None


def _entity_to_response(item: WorkItem) -> dict[str, Any]:
    """Serialize a :class:`WorkItem` to a response dict.

    :param item: The entity to serialize.
    :returns: A JSON-friendly dict with ``object="work_item"``.
    """
    return {
        "id": item.id,
        "object": "work_item",
        "source": item.source,
        "external_id": item.external_id,
        "dedup_key": item.dedup_key,
        "title": item.title,
        "body": item.body,
        "status": item.status,
        "pr_url": item.pr_url,
        "conversation_id": item.conversation_id,
        "assignee_user_id": item.assignee_user_id,
        "created_by": item.created_by,
        "plan": item.plan,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _require_auth(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    """Identify the caller; require authentication in multi-user mode.

    :param request: The incoming request.
    :param auth_provider: Auth provider, or ``None`` in single-user mode.
    :param permission_store: When set, marks multi-user mode (auth required).
    :returns: The user id, or ``None`` in single-user mode.
    :raises OmnigentError: 401 if unauthenticated in multi-user mode.
    """
    user_id = get_user_id(request, auth_provider)
    if permission_store is not None and user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    return user_id


def create_work_items_router(
    store: WorkItemStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the work-items router (mounted at ``/work-items``).

    :param store: The shared :class:`WorkItemStore`.
    :param auth_provider: Auth provider identifying the caller, or ``None``.
    :param permission_store: When set, enables auth enforcement (multi-user).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/work-items")
    async def create_work_item(request: Request, body: CreateWorkItemBody) -> dict[str, Any]:
        """Create a work item (idempotent by ``dedup_key``)."""
        user_id = _require_auth(request, auth_provider, permission_store)
        if not body.title.strip():
            raise OmnigentError("title is required", code=ErrorCode.INVALID_INPUT)
        if body.source not in WORK_ITEM_SOURCES:
            raise OmnigentError(f"source must be one of {_SOURCES}", code=ErrorCode.INVALID_INPUT)
        if body.status not in WORK_ITEM_STATUSES:
            raise OmnigentError(f"status must be one of {_STATUSES}", code=ErrorCode.INVALID_INPUT)

        work_item_id = generate_work_item_id()
        dedup_key = body.dedup_key or (
            f"{body.source}:{body.external_id}" if body.external_id else f"manual:{work_item_id}"
        )
        existing = store.get_by_dedup_key(dedup_key)
        if existing is not None:
            return {"created": False, **_entity_to_response(existing)}

        item = store.create(
            work_item_id,
            body.source,
            body.title.strip(),
            dedup_key=dedup_key,
            external_id=body.external_id,
            body=body.body,
            status=body.status,
            conversation_id=body.conversation_id,
            assignee_user_id=body.assignee_user_id,
            created_by=user_id,
            plan=body.plan,
        )
        return {"created": True, **_entity_to_response(item)}

    @router.get("/work-items")
    async def list_work_items(
        request: Request,
        status: str | None = None,
        conversation_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List work items, newest first, optionally filtered."""
        _require_auth(request, auth_provider, permission_store)
        if status is not None and status not in WORK_ITEM_STATUSES:
            raise OmnigentError(f"status must be one of {_STATUSES}", code=ErrorCode.INVALID_INPUT)
        items = store.list(
            status=status,
            conversation_id=conversation_id,
            limit=max(1, min(limit, 1000)),
        )
        return {"object": "list", "data": [_entity_to_response(i) for i in items]}

    @router.get("/work-items/{work_item_id}")
    async def get_work_item(request: Request, work_item_id: str) -> dict[str, Any]:
        """Get a single work item, or 404."""
        _require_auth(request, auth_provider, permission_store)
        item = store.get(work_item_id)
        if item is None:
            raise OmnigentError("Work item not found", code=ErrorCode.NOT_FOUND)
        return _entity_to_response(item)

    @router.patch("/work-items/{work_item_id}")
    async def update_work_item(
        request: Request, work_item_id: str, body: UpdateWorkItemBody
    ) -> dict[str, Any]:
        """Update a work item's mutable fields."""
        _require_auth(request, auth_provider, permission_store)
        if body.status is not None and body.status not in WORK_ITEM_STATUSES:
            raise OmnigentError(f"status must be one of {_STATUSES}", code=ErrorCode.INVALID_INPUT)
        item = store.update(
            work_item_id,
            title=body.title,
            body=body.body,
            status=body.status,
            pr_url=body.pr_url,
            conversation_id=body.conversation_id,
            assignee_user_id=body.assignee_user_id,
            plan=body.plan,
        )
        if item is None:
            raise OmnigentError("Work item not found", code=ErrorCode.NOT_FOUND)
        return _entity_to_response(item)

    @router.delete("/work-items/{work_item_id}")
    async def delete_work_item(request: Request, work_item_id: str) -> dict[str, Any]:
        """Delete a work item. Idempotent."""
        _require_auth(request, auth_provider, permission_store)
        deleted = store.delete(work_item_id)
        return {"deleted": deleted}

    return router
