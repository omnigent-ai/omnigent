"""Policy and writes for granting another user or group access to a session.

The Share dialog's grant route and automations that share their runs both go
through here, so a grant an automation adds is never one its owner could not
add by hand.
"""

from __future__ import annotations

from typing import Any

from omnigent.db.account_authority import target_account_scope
from omnigent.entities import SessionPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    SharingMode,
    workspace_sharing_blocked,
)
from omnigent.server.sharing_settings import PublicSharingMaxLevel
from omnigent.stores.permission_store import PermissionStore


def sharing_mode(state: Any) -> SharingMode:
    """The live sharing mode off ``app.state``; ``ON`` for hand-built test apps."""
    return getattr(state, "sharing_mode", lambda: SharingMode.ON)()


def require_new_grant_allowed(
    state: Any, *, user_id: str, level: int, workspace: str | None
) -> None:
    """Raise unless the server's sharing policy allows a new grant.

    Only new grants are gated; revoking, listing and owner grants never are.

    :param state: ``app.state``, which carries the live policy callables.
    :param user_id: The grantee, e.g. ``"alice@example.com"`` or ``"__public__"``.
    :param level: The level to grant, e.g. ``LEVEL_READ``.
    :param workspace: The session's working directory, or ``None`` when unknown.
    :raises OmnigentError: ``FORBIDDEN`` or ``INVALID_INPUT`` naming the policy.
    """
    mode = sharing_mode(state)
    if mode == SharingMode.OFF:
        raise OmnigentError(
            "Sharing has been disabled for this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )
    # RESTRICTED_READ_ONLY blocks sharing entirely (even read) for a session
    # whose cwd is a home dir or the filesystem root — too broad to expose.
    if mode == SharingMode.RESTRICTED_READ_ONLY and workspace_sharing_blocked(workspace):
        raise OmnigentError(
            "This session's working directory (a home or root directory) "
            "cannot be shared on this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )
    if mode in (SharingMode.READ_ONLY, SharingMode.RESTRICTED_READ_ONLY) and level > LEVEL_READ:
        raise OmnigentError(
            "Sharing is limited to read-only access on this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )
    if user_id != RESERVED_USER_PUBLIC:
        return
    # Public-access kill switch, independent of the sharing mode: it blocks the
    # anyone-with-the-link grant while leaving user-to-user sharing intact.
    if not getattr(state, "public_sharing", lambda: True)():
        raise OmnigentError(
            "Public access has been disabled for this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )
    ceiling = getattr(state, "public_sharing_max_level", lambda: PublicSharingMaxLevel.READ)()
    if level > ceiling.level:
        raise OmnigentError(
            f"Public access is limited to {ceiling.value} (level {ceiling.level})",
            code=ErrorCode.INVALID_INPUT,
        )


def write_session_grant(
    permission_store: PermissionStore,
    user_id: str,
    session_id: str,
    level: int,
    *,
    keep_higher: bool = False,
) -> SessionPermission | None:
    """Write *user_id*'s grant on *session_id*, never touching the owner's.

    Creates the grantee's user row on demand. Blocking; run it off the event loop.

    :param keep_higher: Leave an existing grant at or above *level* in place and
        return ``None``, so the write never lowers access someone already has,
        even one raised concurrently (see :meth:`PermissionStore.grant_unless_higher`).
    :returns: The written grant, or ``None`` when ``keep_higher`` kept one.
    :raises OmnigentError: ``FORBIDDEN`` when *user_id* owns the session.
    """
    target = permission_store.get_user(user_id)
    with target_account_scope(user_id, target.account_generation if target else None):
        existing = permission_store.get(user_id, session_id)
        if existing is not None and existing.level == LEVEL_OWNER:
            raise OmnigentError("Cannot modify owner permissions", code=ErrorCode.FORBIDDEN)
        permission_store.ensure_user(user_id)
        if keep_higher:
            return permission_store.grant_unless_higher(user_id, session_id, level)
        return permission_store.grant(user_id, session_id, level)
