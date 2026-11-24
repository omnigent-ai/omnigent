"""Share a gadget with people and groups (``/v1/gadgets/{id}/permissions``).

Modeled on the workflow share routes. Whoever manages the gadget (its owner, or
someone granted Can manage) may list, grant or revoke, and agents may not call
these routes. Grants are Can view (read), Can edit (records and versions) or Can
manage, and never public.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, StrictBool, StrictInt, StrictStr

from omnigent.db.account_authority import target_account_scope
from omnigent.debug_logging import add_audit_attrs
from omnigent.entities import GadgetPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.gadgets.access import GadgetAccess
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.scheduled_task_permissions import (
    display_names_or_ids,
    request_from_agent,
)
from omnigent.server.session_sharing import (
    GADGET_GRANT_LEVELS,
    is_reserved_grantee,
    require_sharing_mode_allows_grant,
)
from omnigent.stores.gadget_store import Gadget, GadgetStore, NotFound, is_gadget_id
from omnigent.stores.permission_store import (
    MAX_GRANTS_PAGE_SIZE,
    PermissionStore,
    gadget_sharing_unsupported,
    require_grantee_id_fits,
)

_logger = logging.getLogger(__name__)


class GadgetPermissionObject(BaseModel):
    """
    One grant on a gadget.

    :param user_id: The grantee's canonical ID, e.g. ``"alice@example.com"``.
    :param gadget_id: The gadget.
    :param level: ``1`` (Can view), ``2`` (Can edit) or ``3`` (Can manage).
    :param display_name: The grantee's name for display; the ID when the server
        has no other name.
    """

    user_id: str
    gadget_id: str
    level: int
    display_name: str


class GadgetGrantRequest(BaseModel):
    """
    Body of ``PUT /v1/gadgets/{id}/permissions``.

    :param user_id: The user or group to share with, e.g. ``"alice@example.com"``;
        surrounding whitespace is ignored.
    :param level: ``1`` (Can view), ``2`` (Can edit) or ``3`` (Can manage), as an
        integer.
    :param ensure_minimum: Keep a higher existing grant instead of lowering it.
    """

    user_id: StrictStr
    level: StrictInt
    ensure_minimum: StrictBool = False


class GadgetPermissionList(BaseModel):
    """One page of a gadget's grants, ordered by ``user_id``."""

    permissions: list[GadgetPermissionObject]
    next_cursor: str | None


def stores_gadget_grants(permission_store: PermissionStore | None) -> bool:
    """Whether the server keeps gadget grants, so gadgets can be shared."""
    return permission_store is not None and permission_store.supports_gadget_grants


def _grantee_to_remove(store: PermissionStore, gadget_id: str, user_id: str) -> str:
    """The spelling under which the gadget holds *user_id*'s grant, as for workflows.

    As given when that holds a grant, as a listed ID does, so a store that renames or
    refuses the principal since can't block the removal. Otherwise the store's
    canonical form, or as given when the lookup fails. IDs are matched exactly, as
    authorization matches them, so ``UserA`` and ``usera`` stay separate principals.
    """
    if store.resolve_gadget_access([user_id], gadget_id) is not None:
        return user_id
    try:
        [(grantee, _)] = store.validate_grants([(user_id, LEVEL_READ)])
    except Exception:
        _logger.warning(
            "could not resolve %r to remove it; using it as given", user_id, exc_info=True
        )
        return user_id
    return grantee


def _permission_object(grant: GadgetPermission, names: dict[str, str]) -> GadgetPermissionObject:
    return GadgetPermissionObject(
        user_id=grant.user_id,
        gadget_id=grant.gadget_id,
        level=grant.level,
        display_name=names.get(grant.user_id, grant.user_id),
    )


def register_gadget_permissions_routes(
    router: APIRouter,
    *,
    gadget_store: GadgetStore,
    gadget_access: GadgetAccess,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Register the gadget share routes on the gadgets *router*."""

    def require_sharing(request: Request) -> PermissionStore:
        """Refuse an agent, then a server that cannot keep gadget grants."""
        if request_from_agent(request):
            raise OmnigentError(
                "Agents cannot view or change who a gadget is shared with",
                code=ErrorCode.FORBIDDEN,
            )
        if not stores_gadget_grants(permission_store):
            raise gadget_sharing_unsupported()
        assert permission_store is not None
        return permission_store

    def load_manageable(request: Request, gadget_id: str) -> Gadget:
        """The gadget, once the caller may manage it. Blocking.

        :raises NotFound: When the caller cannot read it.
        :raises OmnigentError: ``FORBIDDEN`` when they can read but not manage it.
        """
        viewer = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        gadget = gadget_store.get(gadget_id) if is_gadget_id(gadget_id) else None
        permissions = gadget_access.permissions(viewer, gadget) if gadget else None
        if gadget is None or permissions is None or not permissions.read:
            raise NotFound("Gadget not found")
        if not permissions.manage:
            raise OmnigentError(
                "Only someone who manages this gadget can share it", code=ErrorCode.FORBIDDEN
            )
        return gadget

    @router.get("/gadgets/{gadget_id}/permissions", response_model=GadgetPermissionList)
    async def list_gadget_permissions(
        request: Request,
        gadget_id: str,
        limit: int = Query(default=100, ge=1, le=MAX_GRANTS_PAGE_SIZE),
        after: str | None = Query(default=None, description="Cursor: user_id to start after"),
    ) -> GadgetPermissionList:
        """List the grants on a gadget the caller manages. The owner holds no grant.

        :raises OmnigentError: 403 for an agent, a server that cannot keep gadget
            grants or a caller who cannot manage it; the gadget 404 otherwise.
        """
        store = require_sharing(request)
        gadget = await asyncio.to_thread(load_manageable, request, gadget_id)
        grants, next_cursor = await asyncio.to_thread(
            store.list_for_gadget, gadget.id, limit=limit, after_user_id=after
        )
        names = await asyncio.to_thread(display_names_or_ids, store, [g.user_id for g in grants])
        return GadgetPermissionList(
            permissions=[_permission_object(grant, names) for grant in grants],
            next_cursor=next_cursor,
        )

    @router.put("/gadgets/{gadget_id}/permissions", response_model=GadgetPermissionObject)
    async def grant_gadget_permission(
        request: Request, gadget_id: str, body: GadgetGrantRequest
    ) -> GadgetPermissionObject:
        """Share a gadget with a user or group at Can view, Can edit or Can manage.

        Replaces an existing grant, or keeps a higher one with ``ensure_minimum``.
        Auto-creates the grantee user if they don't exist yet.

        :raises OmnigentError: 403 for an agent, the sharing policy, a server that
            cannot share gadgets or a caller who cannot manage it; 400 for an empty
            or over-long ID, the owner, ``__public__`` or the local user.
        """
        store = require_sharing(request)
        gadget = await asyncio.to_thread(load_manageable, request, gadget_id)
        # IDs come from people typing them; whitespace around one is never part of it.
        user_id = body.user_id.strip()
        if not user_id:
            raise OmnigentError(
                "Name a user or group to share the gadget with", code=ErrorCode.INVALID_INPUT
            )
        # Before any write: ensure_user would otherwise fail or leave a user row.
        require_grantee_id_fits(user_id)
        if body.level not in GADGET_GRANT_LEVELS:
            raise OmnigentError(
                "Gadgets are shared at Can view (1), Can edit (2) or Can manage (3)",
                code=ErrorCode.INVALID_INPUT,
            )
        # A gadget has no working directory, so a restricted mode only limits the level.
        await asyncio.to_thread(
            require_sharing_mode_allows_grant,
            request.app.state,
            level=body.level,
            load_workspace=lambda: None,
        )
        [(grantee, level)] = await asyncio.to_thread(
            store.validate_grants, [(user_id, body.level)]
        )
        grantee = grantee.strip()
        # On the canonical ID, which is how the store names the owner and the public.
        if grantee == gadget.owner_id:
            raise OmnigentError(
                "A gadget's owner cannot be granted access to it", code=ErrorCode.INVALID_INPUT
            )
        # Variants too, before ensure_user could add a user row for one.
        if is_reserved_grantee(grantee) or grantee in store.public_grantees():
            raise OmnigentError(
                "Gadgets cannot be shared publicly or with the local user; name a user or group",
                code=ErrorCode.INVALID_INPUT,
            )
        target = await asyncio.to_thread(store.get_user, grantee)
        with target_account_scope(grantee, target.account_generation if target else None):
            await asyncio.to_thread(store.ensure_user, grantee)
            grant = await asyncio.to_thread(
                lambda: store.grant_for_gadget(
                    grantee, gadget.id, level, ensure_minimum=body.ensure_minimum
                )
            )
        # A deletion that ran meanwhile already forgot the gadget's grants; drop this one
        # too, so no grant outlives its gadget.
        if await asyncio.to_thread(gadget_store.get, gadget.id) is None:
            await asyncio.to_thread(store.delete_for_gadget, gadget.id)
            raise NotFound("Gadget not found")
        add_audit_attrs(target_user_id=grantee, level=grant.level)
        # The grant is committed, so a failed name lookup must not fail the request.
        names = await asyncio.to_thread(display_names_or_ids, store, [grant.user_id])
        return _permission_object(grant, names)

    @router.delete(
        # The rest of the path, since grantee IDs may contain slashes.
        "/gadgets/{gadget_id}/permissions/{target_user_id:path}",
        status_code=204,
        response_model=None,
    )
    async def revoke_gadget_permission(
        request: Request, gadget_id: str, target_user_id: str
    ) -> Response:
        """Stop sharing a gadget with a user or group. Idempotent: 204 when they hold none.

        :raises OmnigentError: 403 for an agent, a server that cannot keep gadget
            grants or a caller who cannot manage it; the gadget 404 otherwise.
        """
        store = require_sharing(request)
        gadget = await asyncio.to_thread(load_manageable, request, gadget_id)
        add_audit_attrs(target_user_id=target_user_id)
        grantee = await asyncio.to_thread(_grantee_to_remove, store, gadget.id, target_user_id)
        add_audit_attrs(target_user_id=grantee)
        await asyncio.to_thread(store.revoke_for_gadget, grantee, gadget.id)
        return Response(status_code=204)
