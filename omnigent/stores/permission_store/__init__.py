"""Permission store — manages session-level access grants.

Each grant is a ``(user_id, conversation_id, level)`` triple where
level is an integer: 1=read, 2=edit, 3=manage. The ``"__public__"``
sentinel user ID represents public read access.
"""

from abc import ABC, abstractmethod

from omnigent.entities import ResolvedAccess, SessionPermission, UserRecord


class PermissionStore(ABC):
    """Abstract base for session permission persistence.

    Manages grants between users and sessions. All access control
    for sessions routes through this store — there are no ownership
    columns on the conversations table.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the permission store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def grant(
        self,
        user_id: str,
        conversation_id: str,
        level: int,
    ) -> SessionPermission:
        """Upsert a permission grant.

        If the user already has a grant on this session, the level
        is overwritten (can upgrade or downgrade). The caller is
        responsible for authorization checks (only managers may
        grant).

        :param user_id: The grantee, e.g. ``"alice@example.com"``
            or ``"__public__"`` for public access.
        :param conversation_id: The session to grant access to,
            e.g. ``"conv_abc123"``.
        :param level: Numeric permission level (1=read, 2=edit,
            3=manage).
        :returns: The resulting :class:`SessionPermission`.
        """
        ...

    @abstractmethod
    def revoke(self, user_id: str, conversation_id: str) -> bool:
        """Remove a permission grant.

        No-op if the grant does not exist (returns ``False``).

        :param user_id: The grantee to revoke, e.g.
            ``"alice@example.com"``.
        :param conversation_id: The session to revoke access from,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` if a row was deleted, ``False`` if no
            matching grant existed.
        """
        ...

    @abstractmethod
    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        """Look up a single permission grant.

        :param user_id: The grantee, e.g. ``"alice@example.com"``.
        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :returns: The :class:`SessionPermission` if found, otherwise
            ``None``.
        """
        ...

    @abstractmethod
    def list_for_session(self, conversation_id: str) -> list[SessionPermission]:
        """Return all grants on a session.

        :param conversation_id: The session to query, e.g.
            ``"conv_abc123"``.
        :returns: List of :class:`SessionPermission` objects.
        """
        ...

    @abstractmethod
    def list_for_sessions(self, conversation_ids: list[str]) -> dict[str, list[SessionPermission]]:
        """Return all grants for multiple sessions in one batched query.

        Issues a single ``WHERE conversation_id IN (…)`` query and
        returns ALL grants for those sessions (all users, including
        the ``"__public__"`` sentinel).  Callers filter in memory
        for the specific user or owner they care about.

        :param conversation_ids: List of conversation IDs to fetch,
            e.g. ``["conv_abc123", "conv_def456"]``.  An empty list
            returns an empty dict without touching the database.
        :returns: A dict mapping each conversation ID from
            *conversation_ids* to its list of :class:`SessionPermission`
            objects.  Conversations with no grants map to an empty list.
        """
        ...

    @abstractmethod
    def list_for_user(self, user_id: str) -> list[SessionPermission]:
        """Return all grants for a user.

        :param user_id: The user to query, e.g.
            ``"alice@example.com"``.
        :returns: List of :class:`SessionPermission` objects.
        """
        ...

    @abstractmethod
    def ensure_user(self, user_id: str, *, is_admin: bool = False) -> None:
        """Upsert a user row (insert if not exists).

        Called on every authenticated request to ensure the user
        exists before any permission operations reference them.

        :param user_id: The user identifier, e.g.
            ``"alice@example.com"`` or ``"local"``.
        :param is_admin: Set to ``True`` for the ``"local"`` user
            in single-user mode.
        """
        ...

    @abstractmethod
    def is_admin(self, user_id: str) -> bool:
        """Check whether a user has the admin flag set.

        :param user_id: The user to check, e.g. ``"local"``.
        :returns: ``True`` if the user exists and ``is_admin``
            is set, ``False`` otherwise.
        """
        ...

    @abstractmethod
    def list_users(self) -> list[UserRecord]:
        """List all known users (for the admin user list).

        Excludes the reserved sentinels — ``__public__`` (a grant
        target) and ``local`` (the single-user fallback identity) —
        which are not real accounts. Ordered by ``user_id`` for a
        stable listing.

        :returns: A list of :class:`UserRecord` (``user_id`` +
            ``is_admin``), one per real row in the ``users`` table.
        """
        ...

    @abstractmethod
    def set_admin(self, user_id: str, is_admin: bool) -> None:
        """Set the admin flag on an existing user row.

        Unlike :meth:`ensure_user` (which inserts-or-does-nothing and
        therefore can't change an existing row's flag), this is the
        explicit promotion/demotion path. Used by the file-backed
        admin-list promotion at login (see
        :mod:`omnigent.server.admin_list`), which only ever passes
        ``True``. No-op if the user row does not exist — callers
        promote only after the login path has ensured the row.

        :param user_id: The user to update, e.g.
            ``"alice@example.com"``.
        :param is_admin: The flag value to set.
        """
        ...

    @abstractmethod
    def check_access(
        self,
        user_id: str | None,
        conversation_id: str,
        required_level: int,
    ) -> bool:
        """Check whether *user_id* has a grant at *required_level* or above.

        Checks the user's direct grant and the ``__public__`` sentinel
        grant.  Does NOT handle admin bypass or sub-agent parent
        delegation — callers are responsible for those.

        :param user_id: The authenticated user, or ``None`` if unauthenticated.
        :param conversation_id: The session to check.
        :param required_level: Minimum numeric level needed.
        :returns: ``True`` if a sufficient grant exists, ``False`` otherwise.
        """
        ...

    @abstractmethod
    def get_permission_level(
        self,
        user_id: str | None,
        conversation_id: str,
    ) -> int | None:
        """Return the user's effective permission level for UI display.

        Returns ``None`` when the user has no access. Implementations may
        return ``LEVEL_OWNER`` for admin users or the session creator.

        :param user_id: The authenticated user, or ``None``.
        :param conversation_id: The session to check.
        :returns: Numeric level, or ``None``.
        """
        ...

    @abstractmethod
    def resolve_access(
        self,
        user_id: str | None,
        conversation_id: str,
    ) -> ResolvedAccess:
        """Fetch admin flag + the user's and public grants in one round-trip.

        Bundles the three reads that back :meth:`check_access` and
        :meth:`get_permission_level` so a caller needing BOTH the access
        decision and the displayed level pays a single store round-trip
        instead of two. Does NOT apply the admin bypass, public fallback,
        or sub-agent parent delegation — those are resolution policy and
        live in :mod:`omnigent.server.permissions`
        (:func:`resolved_allows` / :func:`resolved_level`). Implementations
        MUST issue the reads on one connection/transaction.

        :param user_id: The authenticated user, e.g.
            ``"alice@example.com"``, or ``None`` if unauthenticated.
        :param conversation_id: The session to resolve, e.g.
            ``"conv_abc123"``.
        :returns: A :class:`ResolvedAccess` snapshot. For ``user_id=None``
            every field is falsy (``is_admin=False``, both levels ``None``).
        """
        ...

    @abstractmethod
    def has_any_grants(self, conversation_id: str) -> bool:
        """Check whether a session has any permission rows at all.

        Used for backwards-compat: pre-migration sessions with no
        grants are treated as open-access during the transition
        period.

        :param conversation_id: The session to check, e.g.
            ``"conv_abc123"``.
        :returns: ``True`` if at least one grant exists.
        """
        ...
