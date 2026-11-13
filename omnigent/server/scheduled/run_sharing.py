"""Share an automation's run sessions with the people the automation is shared with.

The automation's grants live in the permission store, which is the only place they
are read from: a claimed task is a scheduler read. Every run applies the current
list at Read, with the owner's authority:

* A run that creates a session grants it to every grantee.
* A continue-session automation manages its session's sharing once it is shared
  with anyone: each run, and each change to the automation's grants, syncs that
  session to the list, revoking any grant not on it except the owner's and
  ``__public__``.

Grantees match by exact ID, as the session share route matches them. Older run
sessions are never updated. Callers run these as the task's owner: the fire path
inside its owner scopes, a route in the owner's own request. Nothing here raises;
a failure is logged so it never changes a run's outcome.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from omnigent.entities import Conversation, ScheduledTask
from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.routes._sessions.helpers import _announce_session_added
from omnigent.server.session_sharing import require_sharing_mode_allows_grant
from omnigent.server.sharing_settings import host_is_managed_sandbox
from omnigent.stores.permission_store import PermissionStore, read_all_grants

_logger = logging.getLogger(__name__)


class _Revocation(Enum):
    """Which grants missing from the automation's list a share revokes."""

    # A new run session keeps every grant it was created with.
    NONE = "none"
    # A run's sync revokes only once the automation is shared with anyone.
    WHILE_SHARED = "while_shared"
    # A sync after the automation's grants changed revokes even with none left.
    ALWAYS = "always"


def is_managed_run_session(
    task: ScheduledTask, host_id: str | None, host_registry: Any | None
) -> bool:
    """Whether a run's session is in a server-provisioned sandbox, for the sharing policy.

    :param task: The automation the run belongs to.
    :param host_id: The session's host, or ``None`` before a sandbox binds.
    :param host_registry: Live host connections; a host connected with a managed
        launch token is a sandbox.
    """
    return task.execution_target == "managed_sandbox" or host_is_managed_sandbox(
        host_registry, host_id
    )


def _require_run_sharing_allowed(
    app_state: Any | None, task: ScheduledTask, session: Conversation, host_registry: Any | None
) -> None:
    """Raise unless the server's sharing policy allows a Read grant on *session* now."""
    require_sharing_mode_allows_grant(
        app_state,
        level=LEVEL_READ,
        load_workspace=lambda: session.workspace,
        # Nothing rechecks the grants once a workspace is known, so fail closed. A sandbox
        # is exempt: the server picks its working directory in a disposable container.
        allow_unknown_workspace=is_managed_run_session(task, session.host_id, host_registry),
    )


async def _apply_task_grants(
    task: ScheduledTask,
    session: Conversation,
    *,
    permission_store: PermissionStore,
    app_state: Any | None,
    host_registry: Any | None,
    revocation: _Revocation,
) -> None:
    try:
        listed = await asyncio.to_thread(
            read_all_grants, permission_store.list_for_scheduled_task, task.id
        )
        existing = await asyncio.to_thread(
            read_all_grants, permission_store.list_for_session, session.id
        )
    except Exception:
        # A partial list must not drive revocations.
        _logger.exception(
            "automation %s: could not read grants to share session %s", task.id, session.id
        )
        return
    listed_ids = {grant.user_id for grant in listed}
    if revocation is _Revocation.ALWAYS or (revocation is _Revocation.WHILE_SHARED and listed):
        kept = {task.user_id or RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC, *listed_ids}
        for grant in existing:
            if grant.level == LEVEL_OWNER or grant.user_id in kept:
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
    # Anyone with access already holds at least Read, so there is nothing to add for them.
    holders = {grant.user_id for grant in existing}
    missing = [grant for grant in listed if grant.user_id not in holders]
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
            # Automations grant Read only (SCHEDULED_TASK_GRANT_LEVELS). A share made
            # since the session's grants were read is kept, never lowered.
            written = await asyncio.to_thread(
                permission_store.grant_if_absent, grant.user_id, session.id, LEVEL_READ
            )
        except Exception:
            _logger.exception(
                "automation %s: could not share session %s with %r",
                task.id,
                session.id,
                grant.user_id,
            )
            continue
        if written:
            _announce_session_added(grant.user_id, session.id)


async def _share_best_effort(
    task: ScheduledTask,
    session: Conversation,
    *,
    permission_store: PermissionStore | None,
    app_state: Any | None,
    host_registry: Any | None,
    revocation: _Revocation,
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
            revocation=revocation,
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
        revocation=_Revocation.NONE,
    )


async def sync_continued_session(
    task: ScheduledTask,
    session: Conversation,
    *,
    forced: bool,
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
    :param forced: Revoke even while the automation has no grants. A run passes
        ``False``, so an automation nobody has shared never changes its session's
        sharing; a change to the grants passes ``True``, so removing the last grantee
        still revokes them.
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
        revocation=_Revocation.ALWAYS if forced else _Revocation.WHILE_SHARED,
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
    backstop. The sync is forced, so it revokes even when no grants are left. Does
    nothing for an automation that starts a new session each run, or whose session
    is gone or no longer owned by the automation's owner. Never raises.

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
        forced=True,
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
