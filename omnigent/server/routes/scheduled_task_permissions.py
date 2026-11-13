"""Share an automation with people and groups (``/v1/scheduled-tasks/{id}/permissions``).

Mirrors the session share routes. Only the automation's owner may list, grant or
revoke, and agents may not call these routes. Grants are Read only and never public.
A grant syncs a continue-session automation's session right away, best-effort; the
next run's sync is the backstop. A revoke removes the person from that session
before it deletes their automation grant, so a failed revoke is retried by repeating
the DELETE.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from omnigent.db.account_authority import (
    account_generation,
    current_account_user,
    target_account_scope,
)
from omnigent.debug_logging import add_audit_attrs
from omnigent.entities import ScheduledTask
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.feature_flags import Feature
from omnigent.server.scheduled.run_sharing import (
    is_managed_run_session,
    revoke_from_task_session,
    sync_task_session,
)
from omnigent.server.schemas import GrantPermissionRequest
from omnigent.server.session_sharing import (
    SCHEDULED_TASK_GRANT_LEVELS,
    require_sharing_mode_allows_grant,
)
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SCHEDULED_TASK_ORIGIN_LABEL_KEY
from omnigent.stores.permission_store import (
    MAX_GRANTS_PAGE_SIZE,
    PermissionStore,
    require_grantee_id_fits,
    scheduled_task_sharing_unsupported,
)
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# Fields that decide where an automation's runs happen, and so what its grantees see.
_TARGET_FIELDS = ("session_id", "host_id", "workspace", "execution_target")


class ScheduledTaskPermissionObject(BaseModel):
    """
    One grant on an automation.

    :param user_id: The grantee's canonical ID, e.g. ``"alice@example.com"``.
    :param scheduled_task_id: The automation.
    :param level: Numeric permission level; automations grant ``1`` (read) only.
    :param display_name: The grantee's name for display; the ID when the server
        has no other name.
    """

    user_id: str
    scheduled_task_id: str
    level: int
    display_name: str


class ScheduledTaskPermissionList(BaseModel):
    """One page of an automation's grants, ordered by ``user_id``."""

    permissions: list[ScheduledTaskPermissionObject]
    next_cursor: str | None


def request_from_agent(request: Request) -> bool:
    """Whether a runner sent this request for an agent tool, not a person."""
    return any(
        header in request.headers
        for header in (ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER)
    )


def owns_scheduled_task(task: ScheduledTask, owner_id: str | None) -> bool:
    """Whether *owner_id* (``None`` for the local user) owns the live *task*.

    In accounts mode, a re-registered name does not own the old account's tasks.
    """
    return (
        task.state != "deleted"
        and task.user_id == owner_id
        and (
            current_account_user() is None
            or task.account_generation == account_generation(owner_id or RESERVED_USER_LOCAL)
        )
    )


def scheduled_task_sharing_supported(state: Any, permission_store: PermissionStore | None) -> bool:
    """Whether automations can be shared: ``automations_v2`` and a store that keeps grants."""
    return (
        permission_store is not None
        and permission_store.supports_scheduled_task_grants
        and state.feature_flags.enabled(Feature.AUTOMATIONS_V2)
    )


def has_scheduled_task_grants(permission_store: PermissionStore | None, task_id: str) -> bool:
    """Whether anyone holds a grant on the automation, in one bounded read."""
    if permission_store is None or not permission_store.supports_scheduled_task_grants:
        return False
    grants, _ = permission_store.list_for_scheduled_task(task_id, limit=1)
    return bool(grants)


def _retargets(fields: Mapping[str, object], task: ScheduledTask) -> bool:
    """Whether saving *fields* changes where *task*'s runs happen."""

    def changed(name: str) -> bool:
        value = fields.get(name, getattr(task, name))
        # The update keeps the stored target for a null one.
        if name == "execution_target" and value is None:
            return False
        return value != getattr(task, name)

    return any(changed(name) for name in _TARGET_FIELDS)


def _refuse_shared_retarget(
    fields: Mapping[str, object], task: ScheduledTask, permission_store: PermissionStore | None
) -> None:
    if _retargets(fields, task) and has_scheduled_task_grants(permission_store, task.id):
        raise OmnigentError(
            "Agents cannot change the session, host, workspace or execution target of "
            "a shared automation",
            code=ErrorCode.FORBIDDEN,
        )


async def require_agent_keeps_shared_target(
    request: Request,
    fields: Mapping[str, object],
    task: ScheduledTask,
    permission_store: PermissionStore | None,
) -> None:
    """Refuse an agent's update that moves a shared automation's runs somewhere else.

    Its grantees would see the new session, host or workspace, and agents act on
    untrusted input. A person can still make the change. A fast rejection before the
    update; :func:`agent_shared_target_guard` repeats it under the task's lock.

    :param fields: The fields the update writes, after the route derives them, so a
        session cleared by a mode, agent or service change counts.
    :param task: The automation before the update.
    :raises OmnigentError: ``FORBIDDEN`` for such an update.
    """
    if request_from_agent(request):
        await asyncio.to_thread(_refuse_shared_retarget, fields, task, permission_store)


def agent_shared_target_guard(
    request: Request, fields: Mapping[str, object], permission_store: PermissionStore | None
) -> Callable[[ScheduledTask], None] | None:
    """The same refusal as a ``ScheduledTaskStore.update`` guard, for an agent's update.

    It checks the task as locked for the update. Granting locks that row too, so a
    grant either commits before the check and is seen, or waits until the new target
    is saved. On Databricks, grants live in WHS and take no such lock, so there it
    only narrows the race.

    :returns: The guard, or ``None`` when a person sent the update.
    """
    if not request_from_agent(request):
        return None
    return lambda locked: _refuse_shared_retarget(fields, locked, permission_store)


async def automation_managing_session(
    session_id: str,
    viewer_id: str | None,
    *,
    conversation_store: ConversationStore,
    scheduled_task_store: ScheduledTaskStore | None,
    permission_store: PermissionStore | None,
) -> ScheduledTask | None:
    """The viewer's continue-session automation that manages this session's sharing.

    An automation manages its session only while it has a grant. Found through the
    server-owned origin label, which a session gets when an automation's run creates
    it; a session picked for an automation carries none, so this returns ``None``.
    Every read is by primary key.

    :param viewer_id: The caller, or ``None`` for the local user.
    :returns: The automation, or ``None``.
    """
    if (
        scheduled_task_store is None
        or permission_store is None
        or not permission_store.supports_scheduled_task_grants
    ):
        return None
    session = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    task_id = session.labels.get(SCHEDULED_TASK_ORIGIN_LABEL_KEY) if session is not None else None
    if session is None or not task_id:
        return None
    task = await asyncio.to_thread(scheduled_task_store.get, task_id)
    owner_id = None if viewer_id in (None, RESERVED_USER_LOCAL) else viewer_id
    if (
        task is None
        or not owns_scheduled_task(task, owner_id)
        or not task.continues_session
        or task.session_id != session.id
    ):
        return None
    # A run syncs only a session its automation's owner owns.
    owned = await asyncio.to_thread(
        permission_store.check_access,
        task.user_id or RESERVED_USER_LOCAL,
        session.id,
        LEVEL_OWNER,
    )
    if not owned or not await asyncio.to_thread(
        has_scheduled_task_grants, permission_store, task.id
    ):
        return None
    return task


def _display_names(store: PermissionStore, user_ids: Sequence[str]) -> dict[str, str]:
    """The store's display names, or the IDs themselves when the lookup fails."""
    try:
        return store.display_names(user_ids)
    except Exception:
        _logger.exception("could not look up display names for %d grantees", len(user_ids))
        return {user_id: user_id for user_id in user_ids}


def register_scheduled_task_permissions_routes(
    router: APIRouter,
    *,
    load_owned_task: Callable[[Request, str], ScheduledTask],
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
) -> None:
    """Register the automation share routes on the scheduled-tasks *router*.

    :param load_owned_task: Returns the caller's live automation, or raises the
        not-found error the other automation routes give.
    """

    def require_sharing(request: Request) -> PermissionStore:
        """Refuse an agent, then a server that cannot share automations."""
        if request_from_agent(request):
            raise OmnigentError(
                "Agents cannot view or change who an automation is shared with",
                code=ErrorCode.FORBIDDEN,
            )
        if not scheduled_task_sharing_supported(request.app.state, permission_store):
            raise scheduled_task_sharing_unsupported()
        assert permission_store is not None
        return permission_store

    async def owned_task(request: Request, scheduled_task_id: str) -> ScheduledTask:
        return await asyncio.to_thread(load_owned_task, request, scheduled_task_id)

    async def sync_session(request: Request, task: ScheduledTask) -> None:
        """Bring a continue-session automation's session in line with its grants now."""
        await sync_task_session(
            task,
            conversation_store=conversation_store,
            permission_store=permission_store,
            app_state=request.app.state,
            host_registry=getattr(request.app.state, "host_registry", None),
        )

    def automation_workspace(task: ScheduledTask) -> str | None:
        """Where the automation's runs work: its continued session's directory, if any."""
        if task.continues_session:
            assert task.session_id is not None
            session = conversation_store.get_conversation(task.session_id)
            if session is not None:
                return session.workspace
        return task.workspace

    @router.get(
        "/scheduled-tasks/{scheduled_task_id}/permissions",
        response_model=ScheduledTaskPermissionList,
    )
    async def list_scheduled_task_permissions(
        request: Request,
        scheduled_task_id: str,
        limit: int = Query(default=100, ge=1, le=MAX_GRANTS_PAGE_SIZE),
        after: str | None = Query(default=None, description="Cursor: user_id to start after"),
    ) -> ScheduledTaskPermissionList:
        """List the grants on one of the caller's automations with cursor pagination.

        The owner holds no grant and is not listed.

        :param scheduled_task_id: The automation.
        :param limit: Max grants to return (1–1000, default 100).
        :param after: Cursor — user_id to start after (exclusive).
        :returns: ``{"permissions": [...], "next_cursor": str|null}``.
        :raises OmnigentError: 404 when the caller does not own the automation, 403
            for an agent or a server that cannot share automations.
        """
        store = require_sharing(request)
        task = await owned_task(request, scheduled_task_id)
        grants, next_cursor = await asyncio.to_thread(
            store.list_for_scheduled_task, task.id, limit=limit, after_user_id=after
        )
        names = await asyncio.to_thread(_display_names, store, [g.user_id for g in grants])
        return ScheduledTaskPermissionList(
            permissions=[
                ScheduledTaskPermissionObject(
                    user_id=g.user_id,
                    scheduled_task_id=g.scheduled_task_id,
                    level=g.level,
                    display_name=names.get(g.user_id, g.user_id),
                )
                for g in grants
            ],
            next_cursor=next_cursor,
        )

    @router.put(
        "/scheduled-tasks/{scheduled_task_id}/permissions",
        response_model=ScheduledTaskPermissionObject,
    )
    async def grant_scheduled_task_permission(
        request: Request,
        scheduled_task_id: str,
        body: GrantPermissionRequest,
    ) -> ScheduledTaskPermissionObject:
        """Share one of the caller's automations with a user or group.

        Read is the only level, and an automation is never public. A
        continue-session automation's session is shared with them too. Auto-creates
        the grantee user if they don't exist yet.

        :param scheduled_task_id: The automation.
        :param body: The grant request with ``user_id`` and ``level``.
        :returns: The resulting grant.
        :raises OmnigentError: 404 when the caller does not own the automation, 403
            for an agent, a server that cannot share automations or the sharing
            policy, 400 for an empty or over-long ID, the owner, ``__public__`` or a
            level other than read.
        """
        store = require_sharing(request)
        task = await owned_task(request, scheduled_task_id)
        if not body.user_id.strip():
            raise OmnigentError(
                "Name a user or group to share the automation with",
                code=ErrorCode.INVALID_INPUT,
            )
        # Before any write: ensure_user would otherwise fail or leave a user row.
        require_grantee_id_fits(body.user_id)
        if body.level not in SCHEDULED_TASK_GRANT_LEVELS:
            raise OmnigentError(
                "Automations can only be shared with read access",
                code=ErrorCode.INVALID_INPUT,
            )
        # Off the event loop: the mode callable can read a file, and the restricted
        # mode loads the continued session's workspace.
        await asyncio.to_thread(
            require_sharing_mode_allows_grant,
            request.app.state,
            level=body.level,
            load_workspace=lambda: automation_workspace(task),
            # A sandbox's directory is the server's choice, in a disposable container.
            allow_unknown_workspace=is_managed_run_session(
                task, task.host_id, getattr(request.app.state, "host_registry", None)
            ),
        )
        [(grantee, level)] = await asyncio.to_thread(
            store.validate_grants, [(body.user_id, body.level)]
        )
        # On the canonical ID, which is how the store names the owner and the public.
        if grantee == (task.user_id or RESERVED_USER_LOCAL):
            raise OmnigentError(
                "An automation's owner cannot be granted access to it",
                code=ErrorCode.INVALID_INPUT,
            )
        if grantee == RESERVED_USER_PUBLIC:
            raise OmnigentError(
                "Automations cannot be shared publicly; name a user or group",
                code=ErrorCode.INVALID_INPUT,
            )
        target = await asyncio.to_thread(store.get_user, grantee)
        with target_account_scope(grantee, target.account_generation if target else None):
            await asyncio.to_thread(store.ensure_user, grantee)
            perm = await asyncio.to_thread(store.grant_for_scheduled_task, grantee, task.id, level)
        await sync_session(request, task)
        add_audit_attrs(target_user_id=grantee, level=level)
        # The grant is committed, so a failed name lookup must not fail the request.
        names = await asyncio.to_thread(_display_names, store, [perm.user_id])
        return ScheduledTaskPermissionObject(
            user_id=perm.user_id,
            scheduled_task_id=perm.scheduled_task_id,
            level=perm.level,
            display_name=names.get(perm.user_id, perm.user_id),
        )

    @router.delete(
        "/scheduled-tasks/{scheduled_task_id}/permissions/{target_user_id}",
        status_code=204,
        response_model=None,
    )
    async def revoke_scheduled_task_permission(
        request: Request,
        scheduled_task_id: str,
        target_user_id: str,
    ) -> Response:
        """Stop sharing one of the caller's automations with a user or group.

        They lose a continue-session automation's session first, then the automation
        grant. If the session revoke fails, the request fails and the grant stays, so
        repeating the DELETE retries it. Sessions of earlier runs of a new-session
        automation keep their grants. Idempotent: 204 without touching anything when
        they hold no automation grant.

        :param scheduled_task_id: The automation.
        :param target_user_id: The grantee to remove, e.g. ``"alice@example.com"``.
        :returns: 204 No Content.
        :raises OmnigentError: 404 when the caller does not own the automation, 403
            for an agent or a server that cannot share automations, 500 when the
            session revoke fails.
        """
        store = require_sharing(request)
        task = await owned_task(request, scheduled_task_id)
        add_audit_attrs(target_user_id=target_user_id)
        # Access they never had from the automation, such as a hand-made session
        # share, stays.
        if (
            await asyncio.to_thread(store.resolve_scheduled_task_access, [target_user_id], task.id)
            is None
        ):
            return Response(status_code=204)
        await revoke_from_task_session(task, target_user_id, permission_store=store)
        revoked = await asyncio.to_thread(store.revoke_for_scheduled_task, target_user_id, task.id)
        # A full sync only after a change, so an automation nobody shares never
        # overwrites its session's other shares.
        if revoked:
            await sync_session(request, task)
        return Response(status_code=204)
