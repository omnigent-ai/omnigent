"""Share an automation's run sessions with the people the automation is shared with.

The automation's grants live in the permission store, which is the only place they
are read from: a claimed task is a scheduler read. Every run applies the current
list at Read, with the owner's authority:

* A run that creates a session grants it to every grantee.
* A continue-session automation manages its session's sharing: each run, and each
  change to the automation's grants, syncs that session to the list, revoking any
  grant not on it except the owner's and ``__public__``.

Older run sessions are never updated. Callers run these as the task's owner: the
fire path inside its owner scopes, a route in the owner's own request. Nothing here
raises; a failure is logged so it never changes a run's outcome.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from omnigent.entities import Conversation, ScheduledTask
from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.routes._sessions.helpers import _announce_session_added
from omnigent.server.session_sharing import require_sharing_mode_allows_grant
from omnigent.server.sharing_settings import host_is_managed_sandbox
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# The largest page the session grants list route serves.
GRANT_PAGE_SIZE = 1000

_Grant = TypeVar("_Grant")


def _every_grant(
    list_page: Callable[..., tuple[list[_Grant], str | None]], resource_id: str
) -> list[_Grant]:
    """Read every page of a resource's grants."""
    grants, cursor = list_page(resource_id, limit=GRANT_PAGE_SIZE)
    while cursor is not None:
        page, cursor = list_page(resource_id, limit=GRANT_PAGE_SIZE, after_user_id=cursor)
        grants.extend(page)
    return grants


def _principal_key(user_id: str) -> str:
    # Case-insensitive, as MySQL's keys match case variants to one row: a variant must
    # neither overwrite a grant's level nor be revoked as someone else.
    return user_id.casefold()


def _require_run_sharing_allowed(
    app_state: Any | None, task: ScheduledTask, session: Conversation, host_registry: Any | None
) -> None:
    """Raise unless the server's sharing policy allows a Read grant on *session* now."""
    managed = task.execution_target == "managed_sandbox" or host_is_managed_sandbox(
        host_registry, session.host_id
    )
    require_sharing_mode_allows_grant(
        app_state,
        level=LEVEL_READ,
        load_workspace=lambda: session.workspace,
        # Nothing rechecks the grants once a workspace is known, so fail closed. A sandbox
        # is exempt: the server picks its working directory in a disposable container.
        allow_unknown_workspace=managed,
    )


async def _apply_task_grants(
    task: ScheduledTask,
    session: Conversation,
    *,
    permission_store: PermissionStore,
    app_state: Any | None,
    host_registry: Any | None,
    revoke_unlisted: bool,
) -> None:
    try:
        listed = await asyncio.to_thread(
            _every_grant, permission_store.list_for_scheduled_task, task.id
        )
        existing = await asyncio.to_thread(
            _every_grant, permission_store.list_for_session, session.id
        )
    except Exception:
        # A partial list must not drive revocations.
        _logger.exception(
            "automation %s: could not read grants to share session %s", task.id, session.id
        )
        return
    listed_keys = {_principal_key(grant.user_id) for grant in listed}
    if revoke_unlisted:
        kept = {
            _principal_key(task.user_id or RESERVED_USER_LOCAL),
            _principal_key(RESERVED_USER_PUBLIC),
        }
        for grant in existing:
            key = _principal_key(grant.user_id)
            if grant.level == LEVEL_OWNER or key in kept or key in listed_keys:
                continue
            try:
                await asyncio.to_thread(permission_store.revoke, grant.user_id, session.id)
            except Exception:
                _logger.exception(
                    "automation %s: could not revoke %r from session %s",
                    task.id,
                    grant.user_id,
                    session.id,
                )
    # Anyone with access already holds at least Read, so skipping them never lowers a grant.
    holders = {_principal_key(grant.user_id) for grant in existing}
    missing = [grant for grant in listed if _principal_key(grant.user_id) not in holders]
    if not missing:
        return
    try:
        await asyncio.to_thread(
            _require_run_sharing_allowed, app_state, task, session, host_registry
        )
    except OmnigentError as exc:
        _logger.warning(
            "automation %s: not sharing session %s under the sharing policy: %s",
            task.id,
            session.id,
            exc,
        )
        return
    for grant in missing:
        try:
            await asyncio.to_thread(permission_store.ensure_user, grant.user_id)
            # Automations grant Read only; see SCHEDULED_TASK_GRANT_LEVELS.
            await asyncio.to_thread(permission_store.grant, grant.user_id, session.id, LEVEL_READ)
        except Exception:
            _logger.exception(
                "automation %s: could not share session %s with %r",
                task.id,
                session.id,
                grant.user_id,
            )
            continue
        _announce_session_added(grant.user_id, session.id)


async def _share_best_effort(
    task: ScheduledTask,
    session: Conversation,
    *,
    permission_store: PermissionStore | None,
    app_state: Any | None,
    host_registry: Any | None,
    revoke_unlisted: bool,
) -> None:
    try:
        if permission_store is None or not permission_store.supports_scheduled_task_grants:
            return
        await _apply_task_grants(
            task,
            session,
            permission_store=permission_store,
            app_state=app_state,
            host_registry=host_registry,
            revoke_unlisted=revoke_unlisted,
        )
    except Exception:
        _logger.exception("automation %s: could not share session %s", task.id, session.id)


async def share_new_run_session(
    task: ScheduledTask,
    session: Conversation,
    *,
    permission_store: PermissionStore | None,
    app_state: Any | None,
    host_registry: Any | None,
) -> None:
    """Share a session a run just created with each of *task*'s grantees, at Read.

    Each grant is checked against the live sharing policy, and each recipient is
    told about the session. Never raises.

    :param task: The automation the run belongs to.
    :param session: The run's new session.
    :param permission_store: The server's permission store; ``None`` does nothing.
    :param app_state: ``app.state``, which carries the sharing policy.
    :param host_registry: Live host connections, to recognize a sandbox session.
    """
    await _share_best_effort(
        task,
        session,
        permission_store=permission_store,
        app_state=app_state,
        host_registry=host_registry,
        revoke_unlisted=False,
    )


async def sync_continued_session(
    task: ScheduledTask,
    session: Conversation,
    *,
    permission_store: PermissionStore | None,
    app_state: Any | None,
    host_registry: Any | None,
) -> None:
    """Make a continue-session automation's session shared with exactly its grantees.

    Grants anyone missing as :func:`share_new_run_session` does, and revokes every
    other grant except the owner's and ``__public__``. Revoked recipients get no
    push; their session lists drop the session on the next refresh. Never raises.

    :param task: The automation that continues *session*.
    :param session: The session it continues, whose owner the caller has confirmed.
    :param permission_store: The server's permission store; ``None`` does nothing.
    :param app_state: ``app.state``, which carries the sharing policy.
    :param host_registry: Live host connections, to recognize a sandbox session.
    """
    await _share_best_effort(
        task,
        session,
        permission_store=permission_store,
        app_state=app_state,
        host_registry=host_registry,
        revoke_unlisted=True,
    )


async def sync_task_session(
    task: ScheduledTask,
    *,
    conversation_store: Any,
    permission_store: PermissionStore | None,
    app_state: Any | None,
    host_registry: Any | None,
) -> None:
    """Sync a continue-session automation's existing session right after its grants change.

    For the automation's grant and revoke routes; the next run's sync is the
    backstop. Does nothing for an automation that starts a new session each run, or
    whose session is gone or no longer owned by the automation's owner. Never raises.

    :param task: The automation whose grants changed.
    :param conversation_store: Loads the continued session.
    :param permission_store: The server's permission store; ``None`` does nothing.
    :param app_state: ``app.state``, which carries the sharing policy.
    :param host_registry: Live host connections, to recognize a sandbox session.
    """
    if (
        not task.continues_session
        or permission_store is None
        or not permission_store.supports_scheduled_task_grants
    ):
        return
    assert task.session_id is not None
    try:
        session = await asyncio.to_thread(conversation_store.get_conversation, task.session_id)
        owned = session is not None and await asyncio.to_thread(
            permission_store.check_access,
            task.user_id or RESERVED_USER_LOCAL,
            session.id,
            LEVEL_OWNER,
        )
    except Exception:
        _logger.exception("automation %s: could not load its session to share it", task.id)
        return
    if not owned:
        return
    assert session is not None
    await sync_continued_session(
        task,
        session,
        permission_store=permission_store,
        app_state=app_state,
        host_registry=host_registry,
    )


async def delete_task_grants(
    permission_store: PermissionStore | None, scheduled_task_id: str
) -> None:
    """Delete a deleted automation's grants. Best-effort: a failure is logged.

    :param permission_store: The server's permission store; ``None`` does nothing.
    :param scheduled_task_id: The automation, already deleted.
    """
    if permission_store is None:
        return
    try:
        await asyncio.to_thread(permission_store.delete_for_scheduled_task, scheduled_task_id)
    except Exception:
        _logger.exception(
            "could not delete the grants of deleted automation %s", scheduled_task_id
        )
