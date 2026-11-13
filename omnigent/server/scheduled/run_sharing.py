"""Share an automation's run sessions with the people the automation is shared with.

The automation's grants live in the permission store, which is the only place they
are read from: a claimed task is a scheduler read. Every run applies the current
list at Read, with the owner's authority:

* A run that creates a session grants it to every grantee.
* A continue-session automation manages its session's sharing once it is shared
  with anyone: each run, and each change to the automation's grants, syncs that
  session to the list, revoking any grant not on it except the owner's and
  ``__public__``.

Both grant lists are read a page at a time, never whole. A grant is revoked only
once the automation's share table confirms its holder isn't a grantee, so a read
that fails part way revokes nothing unchecked. Grantees are added by exact ID, as the
session share route adds them. Older run sessions are never updated. Callers run
these as the task's owner: the fire path inside its owner scopes, a route in the
owner's own request. Only :func:`revoke_from_task_session` raises, so the revoke
route can keep the grant for a retry; everything else logs a failure so it never
changes a run's outcome.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from omnigent.db.account_authority import target_account_scope
from omnigent.entities import Conversation, ScheduledTask
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner._subprocess_lifecycle import await_cleanup_task
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.routes._sessions.helpers import _announce_session_added
from omnigent.server.session_sharing import require_sharing_mode_allows_grant
from omnigent.server.sharing_settings import host_is_managed_sandbox
from omnigent.stores import permission_store as permission_store_module
from omnigent.stores.permission_store import PermissionStore

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
    # Revocations first, so a sharing policy that refuses additions still revokes.
    if await _revocation_applies(task, session, permission_store, revocation):
        await _revoke_unlisted_grants(task, session, permission_store)
    await _add_missing_grantees(
        task,
        session,
        permission_store=permission_store,
        app_state=app_state,
        host_registry=host_registry,
    )


async def _revocation_applies(
    task: ScheduledTask,
    session: Conversation,
    permission_store: PermissionStore,
    revocation: _Revocation,
) -> bool:
    """Whether this share revokes *session*'s grants that the automation's list lacks."""
    if revocation is not _Revocation.WHILE_SHARED:
        return revocation is _Revocation.ALWAYS
    try:
        grants, _ = await asyncio.to_thread(
            permission_store.list_for_scheduled_task, task.id, limit=1
        )
    except Exception:
        _logger.exception(
            "automation %s: could not check whether it is shared; revoking nothing from "
            "session %s",
            task.id,
            session.id,
        )
        return False
    return bool(grants)


async def _revoke_unlisted_grants(
    task: ScheduledTask, session: Conversation, permission_store: PermissionStore
) -> None:
    """Revoke each of *session*'s grants whose holder isn't one of the automation's grantees.

    Keeps Owner grants, the automation's owner and ``__public__``. Reads a page at a
    time and checks each holder on its own, so a failed read stops the pass without
    revoking anyone unchecked.
    """
    kept = {task.user_id or RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC}
    cursor: str | None = None
    while True:
        try:
            page, cursor = await asyncio.to_thread(
                permission_store.list_for_session,
                session.id,
                limit=permission_store_module.MAX_GRANTS_PAGE_SIZE,
                after_user_id=cursor,
            )
        except Exception:
            _logger.exception(
                "automation %s: could not read the grants of session %s; revoking no more",
                task.id,
                session.id,
            )
            return
        for grant in page:
            if grant.level == LEVEL_OWNER or grant.user_id in kept:
                continue
            if await asyncio.to_thread(
                _held_under_the_stores_comparison,
                permission_store,
                task,
                grant.user_id,
                if_unknown=True,
            ):
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
        if cursor is None:
            return


async def _add_missing_grantees(
    task: ScheduledTask,
    session: Conversation,
    *,
    permission_store: PermissionStore,
    app_state: Any | None,
    host_registry: Any | None,
) -> None:
    """Share *session* at Read with each of the automation's grantees who has no share.

    Reads the automation's grants a page at a time. The sharing policy is checked
    once, before the first write; a refusal or a failed read stops the pass.
    """
    policy_checked = False
    cursor: str | None = None
    while True:
        try:
            page, cursor = await asyncio.to_thread(
                permission_store.list_for_scheduled_task,
                task.id,
                limit=permission_store_module.MAX_GRANTS_PAGE_SIZE,
                after_user_id=cursor,
            )
        except Exception:
            _logger.exception(
                "automation %s: could not read its grants to share session %s",
                task.id,
                session.id,
            )
            return
        for grant in page:
            grantee = grant.user_id
            try:
                # Anyone with access holds at least Read, so there is nothing to add for them.
                held = await asyncio.to_thread(permission_store.get, grantee, session.id)
            except Exception:
                _logger.exception(
                    "automation %s: could not read %r's share of session %s; not sharing it",
                    task.id,
                    grantee,
                    session.id,
                )
                continue
            if held is not None:
                continue
            if not policy_checked:
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
                policy_checked = True
            if await _share_with_grantee(permission_store, task, session, grantee):
                _announce_session_added(grantee, session.id)
        if cursor is None:
            return


def _held_under_the_stores_comparison(
    permission_store: PermissionStore, task: ScheduledTask, user_id: str, *, if_unknown: bool
) -> bool:
    """Whether the automation's share table matches *user_id* to one of its grantees.

    On MySQL the table's collation also matches a case or accent variant, whose
    session grant is then kept rather than revoked and re-added at Read.

    :param if_unknown: The answer when the check fails. Revocation passes ``True``, so
        a failed check never revokes a share.
    """
    try:
        held = permission_store.resolve_scheduled_task_access([user_id], task.id)
    except Exception:
        _logger.exception(
            "automation %s: could not check %r against its grants; treating them as %s",
            task.id,
            user_id,
            "a grantee" if if_unknown else "not a grantee",
        )
        return if_unknown
    return held is not None


async def _share_with_grantee(
    permission_store: PermissionStore, task: ScheduledTask, session: Conversation, grantee: str
) -> bool:
    """Run :func:`_share_and_confirm` in a thread, which a cancelled run waits for.

    The thread can commit the share after a cancel, so cancellation propagates only
    once the share has been rechecked and, if need be, taken back.

    :returns: Whether a grant was written and kept.
    :raises asyncio.CancelledError: When cancelled, once the thread has finished.
    """
    sharing = asyncio.ensure_future(
        asyncio.to_thread(_share_and_confirm, permission_store, task, session, grantee)
    )
    try:
        return await asyncio.shield(sharing)
    except asyncio.CancelledError:
        await await_cleanup_task(sharing)
        raise


def _share_and_confirm(
    permission_store: PermissionStore, task: ScheduledTask, session: Conversation, grantee: str
) -> bool:
    """Grant *grantee* Read on *session* if they still hold the automation's grant.

    Pins the grantee's account as the session share route does, so an account deleted
    and registered again since the list was read is refused, not granted. The grant is
    checked again after the write, and the share is taken back if it was revoked. Never
    raises: a failure is logged.

    :returns: Whether a grant was written and kept.
    """
    try:
        account = permission_store.get_user(grantee)
        with target_account_scope(grantee, account.account_generation if account else None):
            # Checked after the pin: a replaced account no longer holds the grant.
            if permission_store.resolve_scheduled_task_access([grantee], task.id) is None:
                return False
            permission_store.ensure_user(grantee)
            # Automations grant Read only (SCHEDULED_TASK_GRANT_LEVELS). A share made
            # since the grantee was found without one is kept, never lowered.
            written = permission_store.grant_if_absent(grantee, session.id, LEVEL_READ)
    except Exception:
        _logger.exception(
            "automation %s: could not share session %s with %r", task.id, session.id, grantee
        )
        return False
    if not written or _held_under_the_stores_comparison(
        permission_store, task, grantee, if_unknown=False
    ):
        return written
    # Revoked since the check, perhaps after the revoke's own sync ran, or the recheck
    # failed: take the share back, unless it was changed since, e.g. upgraded by hand.
    try:
        permission_store.revoke_if_level(grantee, session.id, LEVEL_READ)
    except Exception:
        _logger.exception(
            "automation %s: could not take back %r's share of session %s",
            task.id,
            grantee,
            session.id,
        )
    return False


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

    For the automation's grant and revoke routes; after a grant, the next run's sync
    is the backstop. The sync is forced, so it revokes even when no grants are left. Does
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


async def revoke_from_task_session(
    task: ScheduledTask, user_id: str, *, permission_store: PermissionStore | None
) -> None:
    """Revoke a grantee from a continue-session automation's session, for the revoke route.

    The route calls this before it deletes their automation grant, which stays as
    the record that this revoke is owed: no run would make it once the automation
    has no grants. Leaves the owner's grant, ``__public__`` and a session the
    automation's owner doesn't own alone, as the sync does.

    :param task: The automation the grantee is being removed from.
    :param user_id: The grantee, e.g. ``"alice@example.com"``.
    :param permission_store: The server's permission store; ``None`` does nothing.
    :raises OmnigentError: ``INTERNAL_ERROR`` when the session's grants can't be
        read or changed, so the caller keeps the automation grant for a retry.
    """
    owner = task.user_id or RESERVED_USER_LOCAL
    if (
        not task.continues_session
        or permission_store is None
        or not permission_store.supports_scheduled_task_grants
        or user_id in (owner, RESERVED_USER_PUBLIC)
    ):
        return
    assert task.session_id is not None
    try:
        if not await asyncio.to_thread(
            permission_store.check_access, owner, task.session_id, LEVEL_OWNER
        ):
            return
        grant = await asyncio.to_thread(permission_store.get, user_id, task.session_id)
        if grant is not None and grant.level != LEVEL_OWNER:
            await asyncio.to_thread(permission_store.revoke, user_id, task.session_id)
    except Exception as exc:
        _logger.exception(
            "automation %s: could not revoke %r from session %s",
            task.id,
            user_id,
            task.session_id,
        )
        raise OmnigentError(
            "Could not remove them from the automation's session, so the automation is "
            "still shared with them. Try again.",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc


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
