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
from omnigent.stores.project_store import ProjectStore


def check_session_access(
    user_id: str | None,
    conversation_id: str,
    required_level: int,
    permission_store: PermissionStore,
    conversation_store: ConversationStore,
    project_store: ProjectStore | None = None,
) -> bool:
    """Check whether *user_id* may perform an action on a session.

    Resolution algorithm:

    1. Admin → allow (before conversation lookup)
    2. Conversation not found → deny
    3. Sub-agent → delegate to parent conversation
    4. Direct session grant (``permission_store.check_access``), OR
    5. Project grant — if the session is filed under a project and
       *project_store* is provided, the session inherits the project's
       ACL. This is what makes sharing a project cover every chat in it.

    :param user_id: The authenticated user, e.g.
        ``"alice@example.com"``. ``None`` if unauthenticated.
    :param conversation_id: The session to check, e.g.
        ``"conv_abc123"``.
    :param required_level: Minimum numeric level needed
        (1=read, 2=edit, 3=manage).
    :param permission_store: Store for permission lookups.
    :param conversation_store: Store for conversation lookups
        (needed for sub-agent parent delegation).
    :param project_store: Store for project-grant inheritance. ``None``
        disables inheritance (the pre-projects behavior).
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
            project_store,
        )

    if permission_store.check_access(user_id, conversation_id, required_level):
        return True

    # Inherit the project's grants for a filed session.
    if (
        project_store is not None
        and conv.project_id is not None
        and project_store.check_access(user_id, conv.project_id, required_level)
    ):
        return True

    return False


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
    if access.members_grant_level is not None and access.members_grant_level >= required_level:
        return True
    if access.project_grant_level is not None and access.project_grant_level >= required_level:
        return True
    return False


def resolved_level(access: ResolvedAccess) -> int | None:
    """The effective level for UI display from a resolved-access snapshot.

    The in-memory equivalent of :meth:`PermissionStore.get_permission_level`
    extended with project inheritance: admin → ``LEVEL_OWNER``; otherwise the
    session-level display grant (the user's own, falling back to
    ``"__public__"`` then ``"__members__"`` — the deliberate access-vs-display
    asymmetry), with the session's project grant max'd on top. The project
    grant can exceed the session grant — a project manager who only reads a
    specific chat should still see manage-level for it.

    :param access: The resolved-access snapshot for one ``(user, conv)``.
    :returns: Numeric level (1/2/3/4), or ``None`` when the user has no
        access.
    """
    if access.is_admin:
        return LEVEL_OWNER
    # Session-level display keeps the historical preference: the user's own
    # grant first, then the ``__public__`` / ``__members__`` sentinels (the
    # deliberate access-vs-display asymmetry — see the docstring). A project
    # grant is then max'd on top, since it can legitimately exceed the session
    # grant (a project manager who only reads a specific chat still manages it).
    session_level = next(
        (
            lvl
            for lvl in (
                access.user_grant_level,
                access.public_grant_level,
                access.members_grant_level,
            )
            if lvl is not None
        ),
        None,
    )
    candidates = [lvl for lvl in (session_level, access.project_grant_level) if lvl is not None]
    return max(candidates) if candidates else None


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
