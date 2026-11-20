"""Share a thingy with people and groups (``/v1/thingies/{id}/permissions``).

Modeled on the workflow share routes. Whoever manages the thingy (its owner, or
someone granted Can manage) may list, grant or revoke, and agents may not call
these routes. Grants are Can view (read), Can edit (records and versions) or Can
manage, and never public.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, StrictBool, StrictInt, StrictStr

from omnigent.db.account_authority import target_account_scope
from omnigent.debug_logging import add_audit_attrs
from omnigent.entities import ThingyPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.scheduled_task_permissions import (
    display_names_or_ids,
    request_from_agent,
)
from omnigent.server.session_sharing import (
    THINGY_GRANT_LEVELS,
    is_reserved_grantee,
    require_sharing_mode_allows_grant,
)
from omnigent.stores.permission_store import (
    MAX_GRANTS_PAGE_SIZE,
    PermissionStore,
    require_grantee_id_fits,
    thingy_sharing_unsupported,
)
from omnigent.stores.thingy_store import NotFound, Thingy, ThingyStore
from omnigent.thingies.access import ThingyAccess

_logger = logging.getLogger(__name__)
_THINGY_ID = re.compile(r"[0-9a-f]{32}")


class ThingyPermissionObject(BaseModel):
    """
    One grant on a thingy.

    :param user_id: The grantee's canonical ID, e.g. ``"alice@example.com"``.
    :param thingy_id: The thingy.
    :param level: ``1`` (Can view), ``2`` (Can edit) or ``3`` (Can manage).
    :param display_name: The grantee's name for display; the ID when the server
        has no other name.
    """

    user_id: str
    thingy_id: str
    level: int
    display_name: str


class ThingyGrantRequest(BaseModel):
    """
    Body of ``PUT /v1/thingies/{id}/permissions``.

    :param user_id: The user or group to share with, e.g. ``"alice@example.com"``;
        surrounding whitespace is ignored.
    :param level: ``1`` (Can view), ``2`` (Can edit) or ``3`` (Can manage), as an
        integer.
    :param ensure_minimum: Keep a higher existing grant instead of lowering it.
    """

    user_id: StrictStr
    level: StrictInt
    ensure_minimum: StrictBool = False


class ThingyPermissionList(BaseModel):
    """One page of a thingy's grants, ordered by ``user_id``."""

    permissions: list[ThingyPermissionObject]
    next_cursor: str | None


def stores_thingy_grants(permission_store: PermissionStore | None) -> bool:
    """Whether the server keeps thingy grants, so thingies can be shared."""
    return permission_store is not None and permission_store.supports_thingy_grants


def _grantee_to_remove(store: PermissionStore, thingy_id: str, user_id: str) -> str:
    """The spelling under which the thingy holds *user_id*'s grant (see workflows)."""
    if store.resolve_thingy_access([user_id], thingy_id) is not None:
        return user_id
    try:
        [(grantee, _)] = store.validate_grants([(user_id, LEVEL_READ)])
    except Exception:
        _logger.warning(
            "could not resolve %r to remove it; using it as given", user_id, exc_info=True
        )
        return user_id
    return grantee


def _existing_spelling(store: PermissionStore, thingy_id: str, grantee: str) -> str:
    """The ID the thingy already holds a grant under for *grantee* in another letter case.

    So re-sharing with ``Bob@Example.com`` changes ``bob@example.com``'s grant rather
    than add a second row for the same person.
    """
    folded = grantee.casefold()
    cursor: str | None = None
    while True:
        grants, cursor = store.list_for_thingy(
            thingy_id, limit=MAX_GRANTS_PAGE_SIZE, after_user_id=cursor
        )
        for grant in grants:
            if grant.user_id == grantee:
                return grantee
            if grant.user_id.casefold() == folded:
                return grant.user_id
        if cursor is None:
            return grantee


def _permission_object(grant: ThingyPermission, names: dict[str, str]) -> ThingyPermissionObject:
    return ThingyPermissionObject(
        user_id=grant.user_id,
        thingy_id=grant.thingy_id,
        level=grant.level,
        display_name=names.get(grant.user_id, grant.user_id),
    )


def register_thingy_permissions_routes(
    router: APIRouter,
    *,
    thingy_store: ThingyStore,
    thingy_access: ThingyAccess,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Register the thingy share routes on the thingies *router*."""

    def require_sharing(request: Request) -> PermissionStore:
        """Refuse an agent, then a server that cannot keep thingy grants."""
        if request_from_agent(request):
            raise OmnigentError(
                "Agents cannot view or change who a thingy is shared with",
                code=ErrorCode.FORBIDDEN,
            )
        if not stores_thingy_grants(permission_store):
            raise thingy_sharing_unsupported()
        assert permission_store is not None
        return permission_store

    def load_manageable(request: Request, thingy_id: str) -> Thingy:
        """The thingy, once the caller may manage it. Blocking.

        :raises NotFound: When the caller cannot read it.
        :raises OmnigentError: ``FORBIDDEN`` when they can read but not manage it.
        """
        viewer = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        thingy = thingy_store.get(thingy_id) if _THINGY_ID.fullmatch(thingy_id) else None
        permissions = thingy_access.permissions(viewer, thingy) if thingy else None
        if thingy is None or permissions is None or not permissions.read:
            raise NotFound("Thingy not found")
        if not permissions.manage:
            raise OmnigentError(
                "Only someone who manages this thingy can share it", code=ErrorCode.FORBIDDEN
            )
        return thingy

    @router.get("/thingies/{thingy_id}/permissions", response_model=ThingyPermissionList)
    async def list_thingy_permissions(
        request: Request,
        thingy_id: str,
        limit: int = Query(default=100, ge=1, le=MAX_GRANTS_PAGE_SIZE),
        after: str | None = Query(default=None, description="Cursor: user_id to start after"),
    ) -> ThingyPermissionList:
        """List the grants on a thingy the caller manages. The owner holds no grant.

        :raises OmnigentError: 403 for an agent, a server that cannot keep thingy
            grants or a caller who cannot manage it; the thingy 404 otherwise.
        """
        store = require_sharing(request)
        thingy = await asyncio.to_thread(load_manageable, request, thingy_id)
        grants, next_cursor = await asyncio.to_thread(
            store.list_for_thingy, thingy.id, limit=limit, after_user_id=after
        )
        names = await asyncio.to_thread(display_names_or_ids, store, [g.user_id for g in grants])
        return ThingyPermissionList(
            permissions=[_permission_object(grant, names) for grant in grants],
            next_cursor=next_cursor,
        )

    @router.put("/thingies/{thingy_id}/permissions", response_model=ThingyPermissionObject)
    async def grant_thingy_permission(
        request: Request, thingy_id: str, body: ThingyGrantRequest
    ) -> ThingyPermissionObject:
        """Share a thingy with a user or group at Can view, Can edit or Can manage.

        Replaces an existing grant, or keeps a higher one with ``ensure_minimum``.
        Auto-creates the grantee user if they don't exist yet.

        :raises OmnigentError: 403 for an agent, the sharing policy, a server that
            cannot share thingies or a caller who cannot manage it; 400 for an empty
            or over-long ID, the owner, ``__public__`` or the local user.
        """
        store = require_sharing(request)
        thingy = await asyncio.to_thread(load_manageable, request, thingy_id)
        # IDs come from people typing them; whitespace around one is never part of it.
        user_id = body.user_id.strip()
        if not user_id:
            raise OmnigentError(
                "Name a user or group to share the thingy with", code=ErrorCode.INVALID_INPUT
            )
        # Before any write: ensure_user would otherwise fail or leave a user row.
        require_grantee_id_fits(user_id)
        if body.level not in THINGY_GRANT_LEVELS:
            raise OmnigentError(
                "Thingies are shared at Can view (1), Can edit (2) or Can manage (3)",
                code=ErrorCode.INVALID_INPUT,
            )
        # A thingy has no working directory, so a restricted mode only limits the level.
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
        # In any letter case: the owner already has every permission.
        if grantee.casefold() == thingy.owner_id.casefold():
            raise OmnigentError(
                "A thingy's owner cannot be granted access to it", code=ErrorCode.INVALID_INPUT
            )
        # Variants too, before ensure_user could add a user row for one.
        if is_reserved_grantee(grantee) or grantee in store.public_grantees():
            raise OmnigentError(
                "Thingies cannot be shared publicly or with the local user; name a user or group",
                code=ErrorCode.INVALID_INPUT,
            )
        grantee = await asyncio.to_thread(_existing_spelling, store, thingy.id, grantee)
        target = await asyncio.to_thread(store.get_user, grantee)
        with target_account_scope(grantee, target.account_generation if target else None):
            await asyncio.to_thread(store.ensure_user, grantee)
            grant = await asyncio.to_thread(
                lambda: store.grant_for_thingy(
                    grantee, thingy.id, level, ensure_minimum=body.ensure_minimum
                )
            )
        # A deletion that ran meanwhile already forgot the thingy's grants; drop this one
        # too, so no grant outlives its thingy.
        if await asyncio.to_thread(thingy_store.get, thingy.id) is None:
            await asyncio.to_thread(store.delete_for_thingy, thingy.id)
            raise NotFound("Thingy not found")
        add_audit_attrs(target_user_id=grantee, level=grant.level)
        # The grant is committed, so a failed name lookup must not fail the request.
        names = await asyncio.to_thread(display_names_or_ids, store, [grant.user_id])
        return _permission_object(grant, names)

    @router.delete(
        # The rest of the path, since grantee IDs may contain slashes.
        "/thingies/{thingy_id}/permissions/{target_user_id:path}",
        status_code=204,
        response_model=None,
    )
    async def revoke_thingy_permission(
        request: Request, thingy_id: str, target_user_id: str
    ) -> Response:
        """Stop sharing a thingy with a user or group. Idempotent: 204 when they hold none.

        :raises OmnigentError: 403 for an agent, a server that cannot keep thingy
            grants or a caller who cannot manage it; the thingy 404 otherwise.
        """
        store = require_sharing(request)
        thingy = await asyncio.to_thread(load_manageable, request, thingy_id)
        add_audit_attrs(target_user_id=target_user_id)
        grantee = await asyncio.to_thread(_grantee_to_remove, store, thingy.id, target_user_id)
        add_audit_attrs(target_user_id=grantee)
        await asyncio.to_thread(store.revoke_for_thingy, grantee, thingy.id)
        return Response(status_code=204)
