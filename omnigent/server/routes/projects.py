"""Routes for first-class projects: CRUD + sharing.

A project is a shareable collection of conversations. Sessions filed under a
project (``conversations.project_id``) inherit the project's ACL, so sharing a
project grants access to every chat in it — current and future — without any
per-session fan-out (see :func:`omnigent.server.permissions.check_session_access`).

The grant surface mirrors the per-session
``PUT/DELETE/GET /v1/sessions/{id}/permissions`` endpoints, but keyed by
project id, and adds the ``__members__`` (all signed-in users) and
``__public__`` (anyone with the link) sentinels — both capped at read-only.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_MEMBERS,
    RESERVED_USER_PUBLIC,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.schemas import (
    GrantPermissionRequest,
    ProjectCreateRequest,
    ProjectMember,
    ProjectObject,
    ProjectPatchRequest,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore

_EVERYONE_SENTINELS = (RESERVED_USER_PUBLIC, RESERVED_USER_MEMBERS)


def create_projects_router(
    project_store: ProjectStore | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the ``/v1/projects`` router.

    :param project_store: Store for project records + grants. ``None``
        disables the whole surface (returns 500 on use).
    :param conversation_store: Needed to unfile a project's chats on delete.
    :param permission_store: Used only for the admin-bypass check.
    :param auth_provider: Auth provider for identity extraction.
    :returns: The configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _require_store() -> ProjectStore:
        if project_store is None:
            raise OmnigentError("Projects not enabled", code=ErrorCode.INTERNAL_ERROR)
        return project_store

    def _is_admin(user_id: str) -> bool:
        return permission_store is not None and permission_store.is_admin(user_id)

    async def _effective_level(user_id: str, project_id: str) -> int | None:
        """The caller's level on a project, applying the admin bypass."""
        if _is_admin(user_id):
            return LEVEL_OWNER
        return await asyncio.to_thread(_require_store().get_permission_level, user_id, project_id)

    async def _require_project(user_id: str, project_id: str, level: int) -> int:
        """Authorize *user_id* at *level*, returning their effective level.

        Raises 404 when the caller has no access at all (don't leak
        existence), 403 when they have some access but not enough.
        """
        effective = await _effective_level(user_id, project_id)
        if effective is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if effective < level:
            raise OmnigentError(
                f"{user_id!r} needs level {level} on project {project_id!r}",
                code=ErrorCode.FORBIDDEN,
            )
        return effective

    async def _to_object(project, level: int) -> ProjectObject:
        """Build a :class:`ProjectObject`, resolving the everyone-scope flags."""
        store = _require_store()
        members = await asyncio.to_thread(store.get, RESERVED_USER_MEMBERS, project.id)
        public = await asyncio.to_thread(store.get, RESERVED_USER_PUBLIC, project.id)
        return ProjectObject(
            id=project.id,
            name=project.name,
            created_by=project.created_by,
            created_at=project.created_at,
            updated_at=project.updated_at,
            permission_level=level,
            members=members is not None,
            public=public is not None,
        )

    @router.post(
        "/projects",
        status_code=201,
        response_model=None,
        responses={201: {"model": ProjectObject}},
    )
    async def create_project(request: Request, body: ProjectCreateRequest) -> ProjectObject:
        """Create a project owned by the caller."""
        user_id = _require_user(request, auth_provider)
        store = _require_store()
        if permission_store is not None and user_id is not None:
            await asyncio.to_thread(permission_store.ensure_user, user_id)
        try:
            project = await asyncio.to_thread(store.create_project, body.name, user_id)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        return await _to_object(project, LEVEL_OWNER)

    @router.get("/projects", response_model=None, responses={200: {"model": list[ProjectObject]}})
    async def list_projects(request: Request) -> list[ProjectObject]:
        """List every project the caller can access, with their level."""
        user_id = _require_user(request, auth_provider)
        store = _require_store()
        pairs = await asyncio.to_thread(store.list_projects_for_user, user_id)
        return [await _to_object(project, level) for project, level in pairs]

    @router.get(
        "/projects/{project_id}", response_model=None, responses={200: {"model": ProjectObject}}
    )
    async def get_project(request: Request, project_id: str) -> ProjectObject:
        """Fetch a single project (read access required)."""
        user_id = _require_user(request, auth_provider)
        level = await _require_project(user_id, project_id, LEVEL_READ)
        project = await asyncio.to_thread(_require_store().get_project, project_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return await _to_object(project, level)

    @router.patch(
        "/projects/{project_id}", response_model=None, responses={200: {"model": ProjectObject}}
    )
    async def rename_project(
        request: Request, project_id: str, body: ProjectPatchRequest
    ) -> ProjectObject:
        """Rename a project (manage access required)."""
        user_id = _require_user(request, auth_provider)
        level = await _require_project(user_id, project_id, LEVEL_MANAGE)
        store = _require_store()
        try:
            project = await asyncio.to_thread(store.rename_project, project_id, body.name)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return await _to_object(project, level)

    @router.delete("/projects/{project_id}", status_code=204, response_model=None)
    async def delete_project(request: Request, project_id: str) -> Response:
        """Delete a project (owner only); its chats survive as unfiled."""
        user_id = _require_user(request, auth_provider)
        await _require_project(user_id, project_id, LEVEL_OWNER)
        store = _require_store()
        # Unfile the chats first (project_id FK is ORM-only on migrated DBs).
        await asyncio.to_thread(conversation_store.clear_project, project_id)
        await asyncio.to_thread(store.delete_project, project_id)
        return Response(status_code=204)

    # ── Sharing ───────────────────────────────────────────────────

    @router.put(
        "/projects/{project_id}/permissions",
        response_model=None,
        responses={200: {"model": ProjectObject}},
    )
    async def grant_project_permission(
        request: Request, project_id: str, body: GrantPermissionRequest
    ) -> ProjectObject:
        """Share a project with a user, or all members / anyone with the link."""
        user_id = _require_user(request, auth_provider)
        level = await _require_project(user_id, project_id, LEVEL_MANAGE)
        store = _require_store()
        if body.user_id == user_id:
            raise OmnigentError("Cannot modify your own permissions", code=ErrorCode.FORBIDDEN)
        if body.user_id in _EVERYONE_SENTINELS and body.level > LEVEL_READ:
            raise OmnigentError(
                "Shared-with-everyone access is limited to read-only (level 1)",
                code=ErrorCode.INVALID_INPUT,
            )
        existing = await asyncio.to_thread(store.get, body.user_id, project_id)
        if existing is not None and existing.level == LEVEL_OWNER:
            raise OmnigentError("Cannot modify owner permissions", code=ErrorCode.FORBIDDEN)
        if permission_store is not None:
            await asyncio.to_thread(permission_store.ensure_user, body.user_id)
        await asyncio.to_thread(store.grant, body.user_id, project_id, body.level)
        project = await asyncio.to_thread(store.get_project, project_id)
        return await _to_object(project, level)

    @router.delete(
        "/projects/{project_id}/permissions/{target_user_id}",
        status_code=204,
        response_model=None,
    )
    async def revoke_project_permission(
        request: Request, project_id: str, target_user_id: str
    ) -> Response:
        """Revoke a grantee from a project (manage access required)."""
        user_id = _require_user(request, auth_provider)
        await _require_project(user_id, project_id, LEVEL_MANAGE)
        store = _require_store()
        if target_user_id == user_id:
            raise OmnigentError(
                "Cannot modify your own permissions (use leave)", code=ErrorCode.FORBIDDEN
            )
        existing = await asyncio.to_thread(store.get, target_user_id, project_id)
        if existing is not None and existing.level == LEVEL_OWNER:
            raise OmnigentError("Cannot revoke owner permissions", code=ErrorCode.FORBIDDEN)
        await asyncio.to_thread(store.revoke, target_user_id, project_id)
        return Response(status_code=204)

    @router.get(
        "/projects/{project_id}/permissions",
        response_model=None,
        responses={200: {"model": list[ProjectMember]}},
    )
    async def list_project_permissions(request: Request, project_id: str) -> list[ProjectMember]:
        """List a project's grantees (manage access required)."""
        user_id = _require_user(request, auth_provider)
        await _require_project(user_id, project_id, LEVEL_MANAGE)
        store = _require_store()
        grants = await asyncio.to_thread(store.list_for_project, project_id)
        grants.sort(key=lambda g: (-g.level, g.user_id))
        return [ProjectMember(user_id=g.user_id, level=g.level) for g in grants]

    @router.delete("/projects/{project_id}/membership", status_code=204, response_model=None)
    async def leave_project(request: Request, project_id: str) -> Response:
        """Leave a project: drop the caller's own non-owner grant. Idempotent."""
        user_id = _require_user(request, auth_provider)
        store = _require_store()
        if user_id is not None:
            existing = await asyncio.to_thread(store.get, user_id, project_id)
            if existing is not None and existing.level < LEVEL_OWNER:
                await asyncio.to_thread(store.revoke, user_id, project_id)
        return Response(status_code=204)

    return router
