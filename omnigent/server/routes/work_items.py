"""REST routes for work-items (Tasks).

CRUD over the ``work_items`` table at ``/work-items[/{work_item_id}]``.
Work items are global to all signed-in users (the row carries
``created_by``/``assignee_user_id`` for future per-user scoping); in
multi-user mode every endpoint requires authentication, but none requires
admin. Creation is idempotent by ``dedup_key`` so external intake (Slack,
email, GitHub, Jira) can safely retry.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.datastructures import Headers

from omnigent.db.utils import generate_work_item_id
from omnigent.entities import WORK_ITEM_SOURCES, WORK_ITEM_STATUSES, WorkItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.webhook_verify import (
    verify_bearer,
    verify_github_signature,
    verify_slack_signature,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.work_item_store import WorkItemStore

# Intake sources accepted at /work-items/intake/{source}. "generic" maps to the
# "manual" work-item source; the rest map to themselves (all in
# WORK_ITEM_SOURCES). Each verifies a different way (see _verify_and_map).
_INTAKE_SOURCES = frozenset({"github", "slack", "jira", "email", "generic"})


class _Verified(BaseModel):
    """Normalized result of verifying + mapping an inbound intake payload."""

    work_item_source: str
    title: str
    dedup_key: str
    external_id: str | None = None
    body: str | None = None


def _verify_and_map(source: str, headers: Headers, raw: bytes) -> _Verified:
    """Authenticate an inbound webhook and map its payload to a work item.

    :param source: The intake source path segment (in :data:`_INTAKE_SOURCES`).
    :param headers: The request headers (for signatures / bearer).
    :param raw: The exact raw request body bytes (signatures cover these).
    :returns: A :class:`_Verified` with the fields for ``create``.
    :raises OmnigentError: 400 (bad source/payload), 401 (bad signature),
        404 (the source's secret isn't configured on this server).
    """
    if source not in _INTAKE_SOURCES:
        raise OmnigentError(f"unknown intake source {source!r}", code=ErrorCode.INVALID_INPUT)

    def _payload() -> dict[str, Any]:
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise OmnigentError(f"invalid JSON body: {exc}", code=ErrorCode.INVALID_INPUT) from exc
        if not isinstance(data, dict):
            raise OmnigentError("body must be a JSON object", code=ErrorCode.INVALID_INPUT)
        return data

    if source == "github":
        secret = os.environ.get("OMNIGENT_GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            raise OmnigentError("github intake not configured", code=ErrorCode.NOT_FOUND)
        if not verify_github_signature(secret, raw, headers.get("X-Hub-Signature-256")):
            raise OmnigentError("invalid github signature", code=ErrorCode.UNAUTHORIZED)
        data = _payload()
        item = data.get("issue") or data.get("pull_request") or {}
        repo = (data.get("repository") or {}).get("full_name") or "repo"
        number = item.get("number")
        if not isinstance(item, dict) or number is None:
            raise OmnigentError("no issue/pull_request in payload", code=ErrorCode.INVALID_INPUT)
        ext = f"{repo}#{number}"
        return _Verified(
            work_item_source="github",
            title=str(item.get("title") or ext),
            dedup_key=f"github:{ext}",
            external_id=ext,
            body=item.get("body"),
        )

    if source == "slack":
        secret = os.environ.get("OMNIGENT_SLACK_SIGNING_SECRET", "")
        if not secret:
            raise OmnigentError("slack intake not configured", code=ErrorCode.NOT_FOUND)
        ok = verify_slack_signature(
            secret,
            headers.get("X-Slack-Request-Timestamp"),
            raw,
            headers.get("X-Slack-Signature"),
        )
        if not ok:
            raise OmnigentError("invalid slack signature", code=ErrorCode.UNAUTHORIZED)
        payload = _payload()
        event = payload.get("event")
        if not isinstance(event, dict):
            event = {}
        text = str(event.get("text") or "").strip()
        channel = event.get("channel") or "?"
        ts = event.get("ts") or "?"
        ext = f"{channel}/{ts}"
        title = (text.splitlines()[0] if text else f"Slack message {ext}")[:200]
        return _Verified(
            work_item_source="slack",
            title=title,
            dedup_key=f"slack:{ext}",
            external_id=ext,
            body=text or None,
        )

    # generic / jira / email: shared bearer token + a simple JSON envelope.
    secret = os.environ.get("OMNIGENT_INTAKE_SECRET", "")
    if not secret:
        raise OmnigentError(f"{source} intake not configured", code=ErrorCode.NOT_FOUND)
    if not verify_bearer(secret, headers.get("Authorization")):
        raise OmnigentError("invalid intake bearer token", code=ErrorCode.UNAUTHORIZED)
    data = _payload()
    title = str(data.get("title") or "").strip()
    if not title:
        raise OmnigentError("title is required", code=ErrorCode.INVALID_INPUT)
    external_id = data.get("external_id")
    work_item_source = "manual" if source == "generic" else source
    dedup_key = data.get("dedup_key") or (
        f"{source}:{external_id}" if external_id else f"{source}:{generate_work_item_id()}"
    )
    return _Verified(
        work_item_source=work_item_source,
        title=title,
        dedup_key=dedup_key,
        external_id=external_id,
        body=data.get("body"),
    )


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

    @router.post("/work-items/intake/{source}")
    async def intake(request: Request, source: str) -> dict[str, Any]:
        """Inbound webhook → work item. Authenticated per-source (GitHub HMAC,
        Slack signing, or a shared bearer for generic/jira/email), and
        idempotent by dedup_key so a sender's retries don't duplicate."""
        raw = await request.body()
        verified = _verify_and_map(source, request.headers, raw)
        existing = store.get_by_dedup_key(verified.dedup_key)
        if existing is not None:
            return {"created": False, **_entity_to_response(existing)}
        item = store.create(
            generate_work_item_id(),
            verified.work_item_source,
            verified.title,
            dedup_key=verified.dedup_key,
            external_id=verified.external_id,
            body=verified.body,
        )
        return {"created": True, **_entity_to_response(item)}

    return router
