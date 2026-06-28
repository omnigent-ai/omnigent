"""Routes for Entities CRUD.

An *entity* is a reusable named instruction authored in the web UI (e.g. the
Jira actions). It can be wired into a flow (job) as a step, where its
``instruction`` text is folded into the flow's rendered narrative.

Endpoints (all under ``/v1``):

- ``POST/GET/GET{id}/PATCH{id}/DELETE{id}`` ``/entities`` — entity CRUD, scoped
  per user.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from omnigent.entities import Entity
from omnigent.entities.builtins import (
    builtin_entities,
    get_builtin_entity,
    is_builtin_entity_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, require_user
from omnigent.server.schemas import (
    EntityCreateRequest,
    EntityResponse,
    EntityUpdateRequest,
)
from omnigent.stores.entity_store import EntityStore
from omnigent.stores.permission_store import PermissionStore


def _entity_to_response(entity: Entity, *, is_builtin: bool = False) -> EntityResponse:
    """Convert an :class:`Entity` to its API response model.

    :param entity: The entity to convert.
    :param is_builtin: Whether this is a read-only code-owned built-in.
    :returns: The :class:`EntityResponse`.
    """
    return EntityResponse(
        id=entity.id,
        title=entity.title,
        instruction=entity.instruction,
        group_id=entity.group_id,
        is_builtin=is_builtin,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


def create_entities_router(
    entity_store: EntityStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the entities router.

    :param entity_store: Store for entity persistence.
    :param auth_provider: Auth provider used to identify the caller.
    :param permission_store: When present, entities are scoped per owner.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner_scope(user_id: str | None) -> str | None:
        """The ``created_by`` filter for list/ownership checks.

        ``None`` in single-user mode (no scoping); the attribution actor
        otherwise.
        """
        return attribution_user(user_id) if permission_store is not None else None

    async def _load_owned_entity(entity_id: str, user_id: str | None) -> Entity:
        """Fetch an entity and enforce ownership, or raise 404.

        Returns 404 (not 403) for another user's entity so existence isn't
        leaked across tenants.
        """
        entity = await asyncio.to_thread(entity_store.get_entity, entity_id)
        scope = _owner_scope(user_id)
        if entity is None or (scope is not None and entity.created_by != scope):
            raise OmnigentError(f"Entity not found: {entity_id!r}", code=ErrorCode.NOT_FOUND)
        return entity

    def _reject_builtin(entity_id: str) -> None:
        """Raise 403 if ``entity_id`` names a read-only built-in entity."""
        if is_builtin_entity_id(entity_id):
            raise OmnigentError(
                f"Entity {entity_id!r} is a read-only built-in and cannot be modified.",
                code=ErrorCode.FORBIDDEN,
            )

    @router.post("/entities", status_code=201, response_model=EntityResponse)
    async def create_entity(request: Request, body: EntityCreateRequest) -> EntityResponse:
        """Create an entity."""
        user_id = require_user(request, auth_provider)
        entity = await asyncio.to_thread(
            entity_store.create_entity,
            title=body.title,
            instruction=body.instruction,
            created_by=attribution_user(user_id),
            group_id=body.group_id,
        )
        return _entity_to_response(entity)

    @router.get("/entities", response_model=list[EntityResponse])
    async def list_entities(request: Request) -> list[EntityResponse]:
        """List entities, code-owned built-ins first then the caller's, newest-updated."""
        user_id = require_user(request, auth_provider)
        entities = await asyncio.to_thread(
            entity_store.list_entities, created_by=_owner_scope(user_id)
        )
        builtins = [_entity_to_response(e, is_builtin=True) for e in builtin_entities()]
        return builtins + [_entity_to_response(e) for e in entities]

    @router.get("/entities/{entity_id}", response_model=EntityResponse)
    async def get_entity(request: Request, entity_id: str) -> EntityResponse:
        """Fetch one entity (built-in or owned)."""
        user_id = require_user(request, auth_provider)
        builtin = get_builtin_entity(entity_id)
        if builtin is not None:
            return _entity_to_response(builtin, is_builtin=True)
        return _entity_to_response(await _load_owned_entity(entity_id, user_id))

    @router.patch("/entities/{entity_id}", response_model=EntityResponse)
    async def update_entity(
        request: Request, entity_id: str, body: EntityUpdateRequest
    ) -> EntityResponse:
        """Patch an entity's fields."""
        user_id = require_user(request, auth_provider)
        _reject_builtin(entity_id)
        await _load_owned_entity(entity_id, user_id)
        updated = await asyncio.to_thread(
            entity_store.update_entity,
            entity_id,
            title=body.title,
            instruction=body.instruction,
            group_id=body.group_id,
        )
        if updated is None:
            raise OmnigentError(f"Entity not found: {entity_id!r}", code=ErrorCode.NOT_FOUND)
        return _entity_to_response(updated)

    @router.delete("/entities/{entity_id}", status_code=204)
    async def delete_entity(request: Request, entity_id: str) -> None:
        """Delete an entity."""
        user_id = require_user(request, auth_provider)
        _reject_builtin(entity_id)
        await _load_owned_entity(entity_id, user_id)
        await asyncio.to_thread(entity_store.delete_entity, entity_id)

    return router
