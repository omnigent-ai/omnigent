"""Shared auth helpers for route handlers.

Provides user identity extraction and permission-check helpers
used by session route handlers. These are thin wrappers over
:mod:`omnigent.server.auth` and
:mod:`omnigent.server.permissions` that handle the ``None``
(disabled) case and raise the appropriate HTTP errors.

``require_access`` and ``get_permission_level`` are ``async def``
because they ultimately hit a synchronous SQLAlchemy permission
store (``is_admin`` + ``get``). Running those queries directly
from the asyncio event-loop thread monopolized the loop for the
full BEGIN / SELECT / COMMIT round trip — load testing showed this
contributing to 300+ ms loop-lag spikes on every session stream
open. Internally these helpers route the sync work through
``asyncio.to_thread`` so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import dataclasses

from fastapi import Request

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    LEVEL_OWNER,
    RESERVED_USER_LOCAL,
    AuthProvider,
)
from omnigent.server.permissions import (
    check_session_access,
    resolved_allows,
    resolved_level,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_LEVEL_NAMES = {1: "read", 2: "edit", 3: "manage", 4: "owner"}


def get_user_id(
    request: Request,
    auth_provider: AuthProvider | None,
) -> str | None:
    """Extract user identity from the request.

    :param request: The incoming FastAPI request.
    :param auth_provider: The auth provider, or ``None`` to skip auth.
    :returns: User ID string, or ``None`` if no auth provider.
    """
    if auth_provider is None:
        return None
    return auth_provider.get_user_id(request)


def attribution_user(user_id: str | None) -> str | None:
    """Map an authenticated identity to its per-message attribution actor.

    Drops the reserved single-user ``"local"`` sentinel — it is not a
    distinct human actor, so recording it as ``created_by`` would label
    every bubble ``"local"``; every other identity passes through.

    :param user_id: Authenticated id from :func:`get_user_id`, e.g.
        ``"alice@example.com"``, ``"local"``, or ``None``.
    :returns: ``None`` for ``"local"`` / ``None`` input; else ``user_id``.
    """
    if user_id == RESERVED_USER_LOCAL:
        return None
    return user_id


def require_user(
    request: Request,
    auth_provider: AuthProvider | None,
) -> str | None:
    """Extract user identity, raising 401 if missing in multi-user mode.

    :param request: The incoming FastAPI request.
    :param auth_provider: The auth provider, or ``None`` to skip auth.
    :returns: User ID string, or ``None`` if no auth provider.
    :raises OmnigentError: 401 if the provider returns ``None``.
    """
    if auth_provider is None:
        return None
    user_id = auth_provider.get_user_id(request)
    if user_id is None:
        raise OmnigentError(
            "Authentication required",
            code=ErrorCode.UNAUTHORIZED,
        )
    return user_id


def _require_access_sync(
    user_id: str | None,
    conversation_id: str,
    required_level: int,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> None:
    """Synchronous core of :func:`require_access`.

    Lives in its own function so the async wrapper below can
    dispatch it to a worker thread via :func:`asyncio.to_thread`.
    Direct callers are limited to non-async contexts (background
    workflows, tests); production async routes use the async
    wrapper.

    :param user_id: The authenticated user, or ``None`` to skip.
    :param conversation_id: The session to check.
    :param required_level: Minimum numeric level needed.
    :param permission_store: Permission store, or ``None`` to skip.
    :param conversation_store: Conversation store for sub-agent lookups.
    :raises OmnigentError: 403 if insufficient level, 404 if no
        access at all.
    """
    if permission_store is None:
        return
    if user_id is None:
        raise OmnigentError(
            "Authentication required",
            code=ErrorCode.UNAUTHORIZED,
        )
    if check_session_access(
        user_id, conversation_id, required_level, permission_store, conversation_store
    ):
        return
    # Distinguish "has some access but not enough" from "no access at all".
    has_any = check_session_access(
        user_id, conversation_id, 1, permission_store, conversation_store
    )
    if has_any:
        level_name = _LEVEL_NAMES.get(required_level, str(required_level))
        raise OmnigentError(
            f"{user_id!r} needs {level_name} permission on session {conversation_id!r}",
            code=ErrorCode.FORBIDDEN,
        )
    raise OmnigentError(
        "Conversation not found",
        code=ErrorCode.NOT_FOUND,
    )


async def require_access(
    user_id: str | None,
    conversation_id: str,
    required_level: int,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> None:
    """Check permission, raising 403 or 404 on failure.

    Async wrapper over :func:`_require_access_sync` that routes
    the synchronous permission-store reads through
    :func:`asyncio.to_thread`, keeping the event-loop thread
    unblocked while the DB call runs.

    If the user has *some* access to the session but not enough,
    returns 403 with an actionable message. If the user has no
    access at all, returns 404 to avoid leaking session existence.

    :param user_id: The authenticated user, or ``None`` to skip.
    :param conversation_id: The session to check.
    :param required_level: Minimum numeric level needed.
    :param permission_store: Permission store, or ``None`` to skip.
    :param conversation_store: Conversation store for sub-agent lookups.
    :raises OmnigentError: 403 if insufficient level, 404 if no
        access at all.
    """
    await asyncio.to_thread(
        _require_access_sync,
        user_id,
        conversation_id,
        required_level,
        permission_store,
        conversation_store,
    )


def _get_permission_level_sync(
    user_id: str | None,
    conversation_id: str,
    permission_store: PermissionStore | None,
) -> int | None:
    """Synchronous core of ``get_permission_level``.

    Delegates to ``PermissionStore.get_permission_level()``.

    :param user_id: The authenticated user, or ``None``.
    :param conversation_id: The session to check.
    :param permission_store: Permission store, or ``None`` to skip.
    :returns: Numeric level (1/2/3), or ``None``.
    """
    if permission_store is None or user_id is None:
        return None
    return permission_store.get_permission_level(user_id, conversation_id)


async def get_permission_level(
    user_id: str | None,
    conversation_id: str,
    permission_store: PermissionStore | None,
) -> int | None:
    """Return the user's numeric permission level, or ``None``.

    Async wrapper over :func:`_get_permission_level_sync` that
    routes the synchronous permission-store reads through
    :func:`asyncio.to_thread`.

    Returns ``None`` when permissions are disabled or the user is
    unauthenticated. Returns ``3`` (manage) for admin users.
    Used to populate the ``permission_level`` field on session
    responses so the UI knows what to enable/disable.

    :param user_id: The authenticated user, or ``None``.
    :param conversation_id: The session to check.
    :param permission_store: Permission store, or ``None`` to skip.
    :returns: Numeric level (1/2/3), or ``None``.
    """
    return await asyncio.to_thread(
        _get_permission_level_sync,
        user_id,
        conversation_id,
        permission_store,
    )


@dataclasses.dataclass(frozen=True)
class SessionAccess:
    """Result of authorizing a caller against a session in one pass.

    Returned by :func:`require_access_and_level` so a snapshot route can
    reuse both the displayed permission level and the already-fetched
    conversation row instead of re-reading them.

    :param level: The caller's numeric permission level for UI display
        (mirrors :func:`get_permission_level`), or ``None`` when
        permissions are disabled.
    :param conversation: The conversation fetched while authorizing, ready
        to thread into the snapshot builder so it isn't re-read. ``None``
        when permissions are disabled (no lookup happened) or for admins
        (who bypass the conversation lookup) — callers fall back to their
        own fetch in those cases.
    """

    level: int | None
    conversation: Conversation | None


def _require_access_and_level_sync(
    user_id: str | None,
    conversation_id: str,
    required_level: int,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> SessionAccess:
    """Synchronous core of :func:`require_access_and_level`.

    Folds :func:`_require_access_sync` and :func:`_get_permission_level_sync`
    into a single resolution: one :meth:`PermissionStore.resolve_access`
    round-trip yields the admin flag and both grants, from which the access
    decision and the displayed level are derived without re-querying. The
    conversation is read at most once (and only for non-admin callers), then
    reused for both the sub-agent parent check and the snapshot.

    Behaviour is identical to calling ``require_access`` followed by
    ``get_permission_level``: the displayed level is the caller's direct
    grant (no parent walk), while the access decision walks sub-agent
    parents and yields 403 vs 404 the same way.

    :param user_id: The authenticated user, or ``None`` to skip.
    :param conversation_id: The session to check, e.g. ``"conv_abc123"``.
    :param required_level: Minimum numeric level needed (1=read, 2=edit,
        3=manage, 4=owner).
    :param permission_store: Permission store, or ``None`` to skip auth.
    :param conversation_store: Conversation store for sub-agent lookups.
    :returns: A :class:`SessionAccess` with the level and (for non-admin
        callers) the fetched conversation.
    :raises OmnigentError: 401 unauthenticated, 403 insufficient level,
        404 no access at all / conversation not found.
    """
    if permission_store is None:
        return SessionAccess(level=None, conversation=None)
    if user_id is None:
        raise OmnigentError(
            "Authentication required",
            code=ErrorCode.UNAUTHORIZED,
        )

    # Single round-trip: admin flag + the user's and public grants on the
    # conversation the caller asked about. The displayed level is the direct
    # grant (no parent walk), matching get_permission_level exactly.
    access = permission_store.resolve_access(user_id, conversation_id)
    level = resolved_level(access)

    # Admins bypass the conversation lookup entirely (mirrors
    # check_session_access's admin short-circuit, which never reads the
    # conversation). A missing conversation is left for the snapshot builder
    # to 404 on, exactly as today.
    if access.is_admin:
        return SessionAccess(level=level, conversation=None)

    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        raise OmnigentError(
            "Conversation not found",
            code=ErrorCode.NOT_FOUND,
        )

    if conv.parent_conversation_id is None:
        # Top-level session: the access-governing grant lives on this same
        # conversation, so reuse the rows already fetched — no extra reads.
        allowed = resolved_allows(access, required_level)
    else:
        # Sub-agent: access delegates to the parent chain. Defer to the
        # canonical recursive checker (its own reads); sub-agents are rare
        # and the parent's grants are a different conversation's rows.
        allowed = check_session_access(
            user_id,
            conv.parent_conversation_id,
            required_level,
            permission_store,
            conversation_store,
        )
    if allowed:
        return SessionAccess(level=level, conversation=conv)

    # Denied — distinguish "has some access but not enough" (403) from
    # "no access at all" (404, to avoid leaking session existence).
    if conv.parent_conversation_id is None:
        has_any = resolved_allows(access, 1)
    else:
        has_any = check_session_access(
            user_id, conv.parent_conversation_id, 1, permission_store, conversation_store
        )
    if has_any:
        level_name = _LEVEL_NAMES.get(required_level, str(required_level))
        raise OmnigentError(
            f"{user_id!r} needs {level_name} permission on session {conversation_id!r}",
            code=ErrorCode.FORBIDDEN,
        )
    raise OmnigentError(
        "Conversation not found",
        code=ErrorCode.NOT_FOUND,
    )


async def require_access_and_level(
    user_id: str | None,
    conversation_id: str,
    required_level: int,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> SessionAccess:
    """Authorize a caller and resolve their display level in one threaded pass.

    Combines :func:`require_access` and :func:`get_permission_level` so a
    GET-snapshot route makes a single set of permission reads (one
    :meth:`PermissionStore.resolve_access` + at most one conversation read)
    instead of the ~5-6 separate store round-trips the two helpers issued
    independently. On a remote DB those round-trips dominate the snapshot's
    fixed cost. Async wrapper over :func:`_require_access_and_level_sync`,
    routed through :func:`asyncio.to_thread` to keep the event loop unblocked.

    :param user_id: The authenticated user, or ``None`` to skip.
    :param conversation_id: The session to check, e.g. ``"conv_abc123"``.
    :param required_level: Minimum numeric level needed (1=read, 2=edit,
        3=manage, 4=owner).
    :param permission_store: Permission store, or ``None`` to skip auth.
    :param conversation_store: Conversation store for sub-agent lookups.
    :returns: A :class:`SessionAccess` (level + fetched conversation).
    :raises OmnigentError: 401 / 403 / 404 as documented on the sync core.
    """
    return await asyncio.to_thread(
        _require_access_and_level_sync,
        user_id,
        conversation_id,
        required_level,
        permission_store,
        conversation_store,
    )


def get_session_owner_id(
    conversation_id: str,
    permission_store: PermissionStore | None,
) -> str | None:
    """Return the user_id of the session owner, or ``None``.

    :param conversation_id: The session to look up.
    :param permission_store: Permission store, or ``None`` to skip.
    :returns: Owner's user_id, or ``None``.
    """
    if permission_store is None:
        return None
    grants, _ = permission_store.list_for_session(conversation_id, limit=1000)
    for g in grants:
        if g.level >= LEVEL_OWNER:
            return g.user_id
    return None
