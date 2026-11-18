"""Share a saved workflow with people and groups (``/v1/workflows/{id}/permissions``).

Mirrors the automation share routes. Only the workflow's owner may list, grant or
revoke, and agents may not call these routes. Grants are Read only and never public:
people a workflow is shared with can view, run and copy it, but only its owner can
change, share or delete it. A run is the runner's own session, so runs stay private.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from omnigent.db.account_authority import target_account_scope
from omnigent.debug_logging import add_audit_attrs
from omnigent.entities import WorkflowPermission
from omnigent.entities.workflow import Workflow
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.server.routes.scheduled_task_permissions import (
    display_names_or_ids,
    request_from_agent,
)
from omnigent.server.schemas import GrantPermissionRequest
from omnigent.server.session_sharing import (
    WORKFLOW_GRANT_LEVELS,
    is_reserved_grantee,
    require_sharing_mode_allows_grant,
)
from omnigent.stores.permission_store import (
    MAX_GRANTS_PAGE_SIZE,
    PermissionStore,
    require_grantee_id_fits,
    workflow_sharing_unsupported,
)
from omnigent.stores.workflow_store import WorkflowStore
from omnigent.util.uuid import normalize_uuid

_logger = logging.getLogger(__name__)

# Why a workflow its store cannot keep grants for can't be shared; the web app says the same.
UNSHAREABLE_WORKFLOW_MESSAGE = (
    "This workflow was saved before sharing was available. Clone it, then share the copy."
)

WorkflowViewerRole = Literal["owner", "viewer"]


class WorkflowPermissionObject(BaseModel):
    """
    One grant on a workflow.

    :param user_id: The grantee's canonical ID, e.g. ``"alice@example.com"``.
    :param workflow_id: The workflow.
    :param level: Numeric permission level; workflows grant ``1`` (read) only.
    :param display_name: The grantee's name for display; the ID when the server
        has no other name.
    """

    user_id: str
    workflow_id: str
    level: int
    display_name: str


class WorkflowPermissionList(BaseModel):
    """One page of a workflow's grants, ordered by ``user_id``."""

    permissions: list[WorkflowPermissionObject]
    next_cursor: str | None


def stores_workflow_grants(permission_store: PermissionStore | None) -> bool:
    """Whether the server keeps workflow grants, so workflows can be shared."""
    return permission_store is not None and permission_store.supports_workflow_grants


def workflow_shareable(permission_store: PermissionStore | None, workflow_id: str) -> bool:
    """Whether the workflow itself can be shared, apart from server-wide support.

    ``/v1/info`` reports server-wide support (:func:`stores_workflow_grants`), so this
    is only the store's answer, and ``True`` without a store. It does no I/O, so a
    list asks for every row.
    """
    return permission_store is None or permission_store.workflow_shareable(workflow_id)


def readable_workflow(
    store: WorkflowStore,
    workflow_id: str,
    user_id: str | None,
    *,
    permission_store: PermissionStore | None,
) -> tuple[Workflow, WorkflowViewerRole] | None:
    """The workflow the caller owns or that is shared with them, and their role.

    A grant through any of the caller's principals makes them a ``"viewer"``, who may
    only read and run it, while its owner's account is live: a deleted owner's
    workflows stop being shared at once, though their cleanup runs later. Unlike
    automations, a viewer's agents read it too: a run of a shared workflow loads its
    description. Blocking: call off the loop.

    :param user_id: The caller, or ``None`` in single-user mode, where nothing is shared.
    :returns: ``(workflow, role)``, or ``None`` when the caller may not read it.
    """
    owned = store.get(workflow_id, user_id=user_id)
    if owned is not None:
        return owned, "owner"
    if user_id in (None, RESERVED_USER_LOCAL) or not stores_workflow_grants(permission_store):
        return None
    assert permission_store is not None and user_id is not None
    principals = permission_store.caller_principals(user_id)
    if permission_store.resolve_workflow_access(principals, workflow_id) is None:
        return None
    shared = store.get_many([workflow_id]).get(normalize_uuid(workflow_id) or workflow_id)
    if (
        shared is None
        or shared.user_id == user_id
        or permission_store.deleted_accounts([shared.user_id])
    ):
        return None
    return shared, "viewer"


def _grantee_to_remove(store: PermissionStore, workflow_id: str, user_id: str) -> str:
    """The spelling under which the workflow holds *user_id*'s grant.

    As given when that holds a grant, as a listed ID does, so a store that renames or
    refuses the principal since can't block the removal. Otherwise the store's
    canonical form, or as given when the lookup fails.
    """
    if store.resolve_workflow_access([user_id], workflow_id) is not None:
        return user_id
    try:
        [(grantee, _)] = store.validate_grants([(user_id, LEVEL_READ)])
    except Exception:
        _logger.warning(
            "could not resolve %r to remove it; using it as given", user_id, exc_info=True
        )
        return user_id
    return grantee


def _permission_object(
    grant: WorkflowPermission, names: dict[str, str]
) -> WorkflowPermissionObject:
    return WorkflowPermissionObject(
        user_id=grant.user_id,
        workflow_id=grant.workflow_id,
        level=grant.level,
        display_name=names.get(grant.user_id, grant.user_id),
    )


def register_workflow_permissions_routes(
    router: APIRouter,
    *,
    load_owned_workflow: Callable[[Request, str], Workflow],
    permission_store: PermissionStore | None,
) -> None:
    """Register the workflow share routes on the workflows *router*.

    :param load_owned_workflow: Returns the caller's workflow, or raises the
        not-found error the other workflow routes give. Blocking.
    """

    def require_sharing(request: Request) -> PermissionStore:
        """Refuse an agent, then a server that cannot keep workflow grants."""
        if request_from_agent(request):
            raise OmnigentError(
                "Agents cannot view or change who a workflow is shared with",
                code=ErrorCode.FORBIDDEN,
            )
        if not stores_workflow_grants(permission_store):
            raise workflow_sharing_unsupported()
        assert permission_store is not None
        return permission_store

    async def owned_workflow(request: Request, workflow_id: str) -> Workflow:
        return await asyncio.to_thread(load_owned_workflow, request, workflow_id)

    @router.get("/workflows/{workflow_id}/permissions", response_model=WorkflowPermissionList)
    async def list_workflow_permissions(
        request: Request,
        workflow_id: str,
        limit: int = Query(default=100, ge=1, le=MAX_GRANTS_PAGE_SIZE),
        after: str | None = Query(default=None, description="Cursor: user_id to start after"),
    ) -> WorkflowPermissionList:
        """List the grants on one of the caller's workflows with cursor pagination.

        The owner holds no grant and is not listed.

        :param workflow_id: The workflow.
        :param limit: Max grants to return (1–1000, default 100).
        :param after: Cursor — user_id to start after (exclusive).
        :returns: ``{"permissions": [...], "next_cursor": str|null}``.
        :raises OmnigentError: 404 when the caller does not own the workflow, 403 for
            an agent or a server that cannot keep workflow grants.
        """
        store = require_sharing(request)
        workflow = await owned_workflow(request, workflow_id)
        grants, next_cursor = await asyncio.to_thread(
            store.list_for_workflow, workflow.id, limit=limit, after_user_id=after
        )
        names = await asyncio.to_thread(display_names_or_ids, store, [g.user_id for g in grants])
        return WorkflowPermissionList(
            permissions=[_permission_object(grant, names) for grant in grants],
            next_cursor=next_cursor,
        )

    @router.put("/workflows/{workflow_id}/permissions", response_model=WorkflowPermissionObject)
    async def grant_workflow_permission(
        request: Request,
        workflow_id: str,
        body: GrantPermissionRequest,
    ) -> WorkflowPermissionObject:
        """Share one of the caller's workflows with a user or group.

        Read is the only level, and a workflow is never public. They can view, run and
        copy it; their runs stay their own. Auto-creates the grantee user if they don't
        exist yet.

        :param workflow_id: The workflow.
        :param body: The grant request with ``user_id`` and ``level``.
        :returns: The resulting grant.
        :raises OmnigentError: 404 when the caller does not own the workflow, 403 for
            an agent, a server that cannot share workflows, the sharing policy or a
            workflow its store cannot keep grants for, 400 for an empty or over-long
            ID, the owner, ``__public__`` or a level other than read.
        """
        store = require_sharing(request)
        workflow = await owned_workflow(request, workflow_id)
        if not body.user_id.strip():
            raise OmnigentError(
                "Name a user or group to share the workflow with",
                code=ErrorCode.INVALID_INPUT,
            )
        # Before any write: ensure_user would otherwise fail or leave a user row.
        require_grantee_id_fits(body.user_id)
        if body.level not in WORKFLOW_GRANT_LEVELS:
            raise OmnigentError(
                "Workflows can only be shared with read access",
                code=ErrorCode.INVALID_INPUT,
            )
        # Off the event loop: the mode callable can read a file. A workflow has no
        # working directory, so a restricted mode allows it at read.
        await asyncio.to_thread(
            require_sharing_mode_allows_grant,
            request.app.state,
            level=body.level,
            load_workspace=lambda: None,
        )
        # After the sharing policy, so sharing off is the reason given, as in the web app.
        if not store.workflow_shareable(workflow.id):
            raise OmnigentError(UNSHAREABLE_WORKFLOW_MESSAGE, code=ErrorCode.FORBIDDEN)
        [(grantee, level)] = await asyncio.to_thread(
            store.validate_grants, [(body.user_id, body.level)]
        )
        # On the canonical ID, which is how the store names the owner and the public.
        if grantee == workflow.user_id:
            raise OmnigentError(
                "A workflow's owner cannot be granted access to it",
                code=ErrorCode.INVALID_INPUT,
            )
        # Variants too, before ensure_user could add a user row for one.
        if is_reserved_grantee(grantee) or grantee in store.public_grantees():
            raise OmnigentError(
                "Workflows cannot be shared publicly or with the local user; name a user or group",
                code=ErrorCode.INVALID_INPUT,
            )
        target = await asyncio.to_thread(store.get_user, grantee)
        with target_account_scope(grantee, target.account_generation if target else None):
            await asyncio.to_thread(store.ensure_user, grantee)
            grant = await asyncio.to_thread(store.grant_for_workflow, grantee, workflow.id, level)
        add_audit_attrs(target_user_id=grantee, level=level)
        # The grant is committed, so a failed name lookup must not fail the request.
        names = await asyncio.to_thread(display_names_or_ids, store, [grant.user_id])
        return _permission_object(grant, names)

    @router.delete(
        # The rest of the path, since grantee IDs may contain slashes.
        "/workflows/{workflow_id}/permissions/{target_user_id:path}",
        status_code=204,
        response_model=None,
    )
    async def revoke_workflow_permission(
        request: Request, workflow_id: str, target_user_id: str
    ) -> Response:
        """Stop sharing one of the caller's workflows with a user or group.

        Their runs are their own sessions, so they keep those. Idempotent: 204 when they
        hold no grant.

        :param workflow_id: The workflow.
        :param target_user_id: The grantee to remove, e.g. ``"alice@example.com"``, in
            any spelling :meth:`PermissionStore.validate_grants` resolves.
        :returns: 204 No Content.
        :raises OmnigentError: 404 when the caller does not own the workflow, 403 for
            an agent or a server that cannot keep workflow grants.
        """
        store = require_sharing(request)
        workflow = await owned_workflow(request, workflow_id)
        # Audit the requested ID first, in case resolving it fails.
        add_audit_attrs(target_user_id=target_user_id)
        # The stored spelling, so the revoke finds the grant the list showed.
        grantee = await asyncio.to_thread(_grantee_to_remove, store, workflow.id, target_user_id)
        add_audit_attrs(target_user_id=grantee)
        await asyncio.to_thread(store.revoke_for_workflow, grantee, workflow.id)
        return Response(status_code=204)
