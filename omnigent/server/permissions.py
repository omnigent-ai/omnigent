"""Session permission checks for route handlers.

Provides :func:`check_session_access` which implements the
permission resolution algorithm from ``designs/SESSIONS_AUTH.md``.
All session access — reads, edits, management — routes through
this single function.
"""

from __future__ import annotations

from omnigent.entities import ResolvedAccess
from omnigent.server.auth import LEVEL_MANAGE, LEVEL_OWNER
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def check_session_access(
    user_id: str | None,
    conversation_id: str,
    required_level: int,
    permission_store: PermissionStore,
    conversation_store: ConversationStore,
) -> bool:
    """Check whether *user_id* may perform an action on a session.

    Resolution algorithm:

    1. Admin → allow (before conversation lookup)
    2. Conversation not found → deny
    3. Sub-agent → delegate to parent conversation
    4. Delegate grant check to ``permission_store.check_access``

    :param user_id: The authenticated user, e.g.
        ``"alice@example.com"``. ``None`` if unauthenticated.
    :param conversation_id: The session to check, e.g.
        ``"conv_abc123"``.
    :param required_level: Minimum numeric level needed
        (1=read, 2=edit, 3=manage).
    :param permission_store: Store for permission lookups.
    :param conversation_store: Store for conversation lookups
        (needed for sub-agent parent delegation).
    :returns: ``True`` if access is allowed, ``False`` otherwise.
    """
    if user_id is not None and permission_store.is_admin(user_id):
        return True

    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return False

    if conv.parent_conversation_id is not None:
        return check_session_access(
            user_id,
            conv.parent_conversation_id,
            required_level,
            permission_store,
            conversation_store,
        )

    return permission_store.check_access(user_id, conversation_id, required_level)


def resolved_allows(access: ResolvedAccess, required_level: int) -> bool:
    """Whether *access* grants *required_level*, ignoring sub-agent delegation.

    The in-memory equivalent of the admin bypass plus
    :meth:`PermissionStore.check_access` (direct grant OR ``"__public__"``
    grant), for a :class:`ResolvedAccess` snapshot already fetched from the
    store. Sub-agent parent delegation is the caller's responsibility — this
    only considers the grants on the conversation the snapshot was resolved
    for.

    :param access: The resolved-access snapshot for one ``(user, conv)``.
    :param required_level: Minimum numeric level needed (1=read, 2=edit,
        3=manage, 4=owner).
    :returns: ``True`` if access is allowed, ``False`` otherwise.
    """
    if access.is_admin:
        return True
    if access.user_grant_level is not None and access.user_grant_level >= required_level:
        return True
    if access.public_grant_level is not None and access.public_grant_level >= required_level:
        return True
    return False


def resolved_level(access: ResolvedAccess) -> int | None:
    """The effective level for UI display from a resolved-access snapshot.

    The in-memory equivalent of :meth:`PermissionStore.get_permission_level`:
    admin → ``LEVEL_OWNER``; otherwise the user's own grant, falling back to
    the ``"__public__"`` grant, else ``None``. Note this deliberately prefers
    the user's own grant over a (possibly higher) public grant, matching the
    store — so it can differ from :func:`resolved_allows`, which is satisfied
    by either.

    :param access: The resolved-access snapshot for one ``(user, conv)``.
    :returns: Numeric level (1/2/3/4), or ``None`` when the user has no
        access.
    """
    if access.is_admin:
        return LEVEL_OWNER
    if access.user_grant_level is not None:
        return access.user_grant_level
    return access.public_grant_level


def check_is_manager(
    user_id: str | None,
    conversation_id: str,
    permission_store: PermissionStore,
    conversation_store: ConversationStore,
) -> bool:
    """Shorthand for checking manage-level access.

    :param user_id: The authenticated user, or ``None``.
    :param conversation_id: The session to check, e.g.
        ``"conv_abc123"``.
    :param permission_store: Store for permission lookups.
    :param conversation_store: Store for conversation lookups.
    :returns: ``True`` if the user has manage access.
    """
    return check_session_access(
        user_id,
        conversation_id,
        LEVEL_MANAGE,
        permission_store,
        conversation_store,
    )
