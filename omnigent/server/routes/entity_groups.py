"""Routes for Entity Groups CRUD + custom icon upload/serve.

An *entity group* is a named, icon-bearing category for entities, shown in the
flow builder's step picker. Built-in groups (Jira/GitHub) are code-owned and
read-only (see :mod:`omnigent.entities.builtins`); users can also create their
own groups and upload a custom icon image.

Endpoints (all under ``/v1``):

- ``POST/GET/GET{id}/PATCH{id}/DELETE{id}`` ``/entity-groups`` — group CRUD,
  scoped per user; built-ins merged in (read-only).
- ``POST /entity-groups/{id}/icon`` — upload a custom icon image.
- ``GET /entity-groups/{id}/icon`` — serve the uploaded icon bytes.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response

from omnigent.entities import EntityGroup
from omnigent.entities.builtins import (
    builtin_groups,
    get_builtin_group,
    is_builtin_group_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, require_user
from omnigent.server.schemas import (
    EntityGroupCreateRequest,
    EntityGroupResponse,
    EntityGroupUpdateRequest,
)
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.entity_group_store import EntityGroupStore
from omnigent.stores.permission_store import PermissionStore

# Custom group icons are small; cap uploads to keep them in memory cheaply.
_ICON_MAX_BYTES = 1024 * 1024
_ICON_READ_CHUNK_BYTES = 64 * 1024
# Allowed image content types for an uploaded group icon.
_ICON_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/svg+xml", "image/gif"}
)


def _icon_artifact_key(group_id: str) -> str:
    """Artifact-store key for a group's uploaded icon."""
    return f"entity-group-icons/{group_id}"


def _group_to_response(group: EntityGroup, *, is_builtin: bool = False) -> EntityGroupResponse:
    """Convert an :class:`EntityGroup` to its API response model.

    :param group: The group to convert.
    :param is_builtin: Whether this is a read-only code-owned built-in.
    :returns: The :class:`EntityGroupResponse`.
    """
    icon_url = (
        f"/v1/entity-groups/{group.id}/icon" if group.icon_artifact_key is not None else None
    )
    return EntityGroupResponse(
        id=group.id,
        name=group.name,
        icon_key=group.icon_key,
        icon_url=icon_url,
        is_builtin=is_builtin,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


def create_entity_groups_router(
    entity_group_store: EntityGroupStore,
    artifact_store: ArtifactStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the entity-groups router.

    :param entity_group_store: Store for group persistence.
    :param artifact_store: Blob store for uploaded icon images.
    :param auth_provider: Auth provider used to identify the caller.
    :param permission_store: When present, groups are scoped per owner.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner_scope(user_id: str | None) -> str | None:
        return attribution_user(user_id) if permission_store is not None else None

    def _reject_builtin(group_id: str) -> None:
        """Raise 403 if ``group_id`` names a read-only built-in group."""
        if is_builtin_group_id(group_id):
            raise OmnigentError(
                f"Entity group {group_id!r} is a read-only built-in and cannot be modified.",
                code=ErrorCode.FORBIDDEN,
            )

    async def _load_owned_group(group_id: str, user_id: str | None) -> EntityGroup:
        """Fetch a user group and enforce ownership, or raise 404.

        Returns 404 (not 403) for another user's group so existence isn't
        leaked across tenants.
        """
        group = await asyncio.to_thread(entity_group_store.get_group, group_id)
        scope = _owner_scope(user_id)
        if group is None or (scope is not None and group.created_by != scope):
            raise OmnigentError(
                f"Entity group not found: {group_id!r}", code=ErrorCode.NOT_FOUND
            )
        return group

    @router.post("/entity-groups", status_code=201, response_model=EntityGroupResponse)
    async def create_group(
        request: Request, body: EntityGroupCreateRequest
    ) -> EntityGroupResponse:
        """Create an entity group."""
        user_id = require_user(request, auth_provider)
        group = await asyncio.to_thread(
            entity_group_store.create_group,
            name=body.name,
            icon_key=body.icon_key,
            created_by=attribution_user(user_id),
        )
        return _group_to_response(group)

    @router.get("/entity-groups", response_model=list[EntityGroupResponse])
    async def list_groups(request: Request) -> list[EntityGroupResponse]:
        """List groups, code-owned built-ins first then the caller's, newest-updated."""
        user_id = require_user(request, auth_provider)
        groups = await asyncio.to_thread(
            entity_group_store.list_groups, created_by=_owner_scope(user_id)
        )
        builtins = [_group_to_response(g, is_builtin=True) for g in builtin_groups()]
        return builtins + [_group_to_response(g) for g in groups]

    @router.get("/entity-groups/{group_id}", response_model=EntityGroupResponse)
    async def get_group(request: Request, group_id: str) -> EntityGroupResponse:
        """Fetch one group (built-in or owned)."""
        user_id = require_user(request, auth_provider)
        builtin = get_builtin_group(group_id)
        if builtin is not None:
            return _group_to_response(builtin, is_builtin=True)
        return _group_to_response(await _load_owned_group(group_id, user_id))

    @router.patch("/entity-groups/{group_id}", response_model=EntityGroupResponse)
    async def update_group(
        request: Request, group_id: str, body: EntityGroupUpdateRequest
    ) -> EntityGroupResponse:
        """Patch a group's fields."""
        user_id = require_user(request, auth_provider)
        _reject_builtin(group_id)
        await _load_owned_group(group_id, user_id)
        updated = await asyncio.to_thread(
            entity_group_store.update_group,
            group_id,
            name=body.name,
            icon_key=body.icon_key,
        )
        if updated is None:
            raise OmnigentError(
                f"Entity group not found: {group_id!r}", code=ErrorCode.NOT_FOUND
            )
        return _group_to_response(updated)

    @router.delete("/entity-groups/{group_id}", status_code=204)
    async def delete_group(request: Request, group_id: str) -> None:
        """Delete a group (ungrouping its entities)."""
        user_id = require_user(request, auth_provider)
        _reject_builtin(group_id)
        await _load_owned_group(group_id, user_id)
        await asyncio.to_thread(entity_group_store.delete_group, group_id)

    @router.post("/entity-groups/{group_id}/icon", response_model=EntityGroupResponse)
    async def upload_icon(
        request: Request,
        group_id: str,
        file: Annotated[UploadFile, File(...)],
    ) -> EntityGroupResponse:
        """Upload (or replace) a group's custom icon image."""
        user_id = require_user(request, auth_provider)
        _reject_builtin(group_id)
        await _load_owned_group(group_id, user_id)

        content_type = (file.content_type or "").split(";")[0].strip().lower()
        if content_type not in _ICON_CONTENT_TYPES:
            raise OmnigentError(
                f"Unsupported icon type {content_type!r}; allowed: "
                f"{', '.join(sorted(_ICON_CONTENT_TYPES))}.",
                code=ErrorCode.INVALID_INPUT,
            )

        data = await _read_capped(file, _ICON_MAX_BYTES)
        key = _icon_artifact_key(group_id)
        await asyncio.to_thread(artifact_store.put, key, data)
        updated = await asyncio.to_thread(
            entity_group_store.update_group,
            group_id,
            icon_artifact_key=key,
            icon_content_type=content_type,
        )
        if updated is None:
            raise OmnigentError(
                f"Entity group not found: {group_id!r}", code=ErrorCode.NOT_FOUND
            )
        return _group_to_response(updated)

    @router.get("/entity-groups/{group_id}/icon")
    async def get_icon(request: Request, group_id: str) -> Response:
        """Serve a group's uploaded icon bytes (404 if none)."""
        user_id = require_user(request, auth_provider)
        group = await _load_owned_group(group_id, user_id)
        if group.icon_artifact_key is None:
            raise OmnigentError(
                f"Entity group {group_id!r} has no icon.", code=ErrorCode.NOT_FOUND
            )
        try:
            data = await asyncio.to_thread(artifact_store.get, group.icon_artifact_key)
        except KeyError as exc:
            raise OmnigentError(
                f"Entity group {group_id!r} has no icon.", code=ErrorCode.NOT_FOUND
            ) from exc
        return Response(
            content=data,
            media_type=group.icon_content_type or "application/octet-stream",
            headers={
                # Neutralize any active content in an uploaded SVG.
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=3600",
            },
        )

    return router


async def _read_capped(file: UploadFile, limit_bytes: int) -> bytes:
    """Read an upload into memory, raising 400 once it exceeds ``limit_bytes``.

    :param file: The multipart upload.
    :param limit_bytes: Maximum allowed size in bytes.
    :returns: The full file content.
    :raises OmnigentError: 400 when the upload exceeds ``limit_bytes``.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_ICON_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise OmnigentError(
                f"Icon exceeds the {limit_bytes // (1024 * 1024)} MB limit.",
                code=ErrorCode.INVALID_INPUT,
            )
        chunks.append(chunk)
    return b"".join(chunks)
