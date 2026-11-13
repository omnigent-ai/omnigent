"""Permission store — manages session-level access grants.

Each grant is a ``(user_id, conversation_id, level)`` triple where
level is an integer: 1=read, 2=edit, 3=manage. The ``"__public__"``
sentinel user ID represents public link access, capped by the live server policy.

Scheduled tasks (automations) have grants of the same shape, keyed by
``scheduled_task_id``. Stores opt in through
:attr:`PermissionStore.supports_scheduled_task_grants`.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from omnigent.entities import Account, ResolvedAccess, ScheduledTaskPermission, SessionPermission
from omnigent.errors import ErrorCode, OmnigentError

# Largest page a grants list returns; the session permissions route allows the same.
MAX_GRANTS_PAGE_SIZE = 1000


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
        *,
        ensure_minimum: bool = False,
    ) -> SessionPermission:
        """Upsert a permission grant.

        By default, an existing grant's level is overwritten (can
        upgrade or downgrade). The caller is
        responsible for authorization checks (only managers may
        grant).

        :param user_id: The grantee, e.g. ``"alice@example.com"``
            or ``"__public__"`` for public access.
        :param conversation_id: The session to grant access to,
            e.g. ``"conv_abc123"``.
        :param level: Numeric permission level (1=read, 2=edit,
            3=manage).
        :param ensure_minimum: Atomically retain the higher of the existing
            and requested levels instead of replacing the existing level.
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
    def reassign_user_grants(self, from_user_id: str, to_user_id: str) -> int:
        """Move one user's grants to another user.

        Existing destination grants win when both users have access to the
        same session, and the duplicate source grant is removed.

        :param from_user_id: Source grantee whose grants move.
        :param to_user_id: Destination grantee that receives them.
        :returns: Number of grants moved to *to_user_id*.
        """
        ...

    @abstractmethod
    def list_for_session(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        after_user_id: str | None = None,
    ) -> tuple[list[SessionPermission], str | None]:
        """Return grants on a session with cursor pagination.

        :param conversation_id: The session to query.
        :param limit: Max grants to return (default 100).
        :param after_user_id: Exclusive cursor — return grants with
            user_id > this value.
        :returns: Tuple of (grants, next_cursor). next_cursor is the
            user_id to pass as after_user_id on the next call, or None
            if no more results.
        """
        ...

    @abstractmethod
    def list_for_sessions(
        self, conversation_ids: list[str], *, user_id: str | None = None
    ) -> dict[str, list[SessionPermission]]:
        """Return all grants for multiple sessions in one batched query.

        Issues a single ``WHERE conversation_id IN (…)`` query and
        returns all grants unless ``user_id`` restricts it to one caller.
        Without that filter, it includes all users, including
        the ``"__public__"`` sentinel. Callers filter in memory
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
    def list_for_user(self, user_id: str, *, limit: int = 1000) -> list[SessionPermission]:
        """Return all grants for a user.

        :param user_id: The user to query, e.g.
            ``"alice@example.com"``.
        :param limit: Maximum number of grants to return (default 1000).
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
    def get_user(self, user_id: str) -> Account | None:
        """Read the target account and generation before an operation on another user."""
        ...

    @abstractmethod
    def user_exists(self, user_id: str) -> bool:
        """Check whether a user row exists, without creating it.

        :param user_id: The user identifier, e.g. ``"alice"``.
        :returns: ``True`` if a row exists for the user.
        """
        ...

    @abstractmethod
    def list_users(self, *, limit: int = 1000) -> list[Account]:
        """Return every real user row, for the admin user list.

        Excludes the reserved sentinels (``"__public__"`` and
        ``"local"``) that aren't real actors, mirroring
        :meth:`SqlAlchemyAccountStore.list_users` so the OIDC/header
        admin surface can reuse the accounts-mode ``Account`` shape.
        The ``password_hash`` is never included (see :class:`Account`).

        Result is unordered; callers sort for display.

        :param limit: Maximum number of user rows to return (default 1000).
        :returns: List of :class:`Account` rows.
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
        grant, subject to the live public ceiling. Does NOT handle admin bypass or sub-agent parent
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

    def validate_grants(self, grants: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
        """Check a batch of ``(user_id, level)`` grants before they are saved.

        The default accepts every grant and writes nothing, because :meth:`grant`
        creates users on demand. Stores that resolve principals override this.
        Overrides must follow this contract:

        * Return the grants in input order with each ``user_id`` replaced by the
          store's canonical name. Save the returned list, not the input, so one
          person cannot be stored under two spellings.
        * Raise ``OmnigentError(ErrorCode.INVALID_INPUT)`` that names every unknown
          principal, not just the first.
        * Raise ``OmnigentError(ErrorCode.FORBIDDEN)`` for a grant the store refuses.

        :param grants: The ``(user_id, level)`` pairs to check, e.g.
            ``[("alice@example.com", 1)]``.
        :returns: The grants with canonical user IDs, in input order.
        :raises OmnigentError: ``INVALID_INPUT`` for unknown principals, or
            ``FORBIDDEN`` for a refused grant.
        """
        return list(grants)

    # ── Scheduled task grants ─────────────────────────────────────────────
    # The defaults suit a store that cannot persist these grants: it holds none,
    # refuses new ones and deletes nothing.

    @property
    def supports_scheduled_task_grants(self) -> bool:
        """Whether this store persists scheduled task grants.

        Callers hide automation sharing when this is ``False``, the default.
        """
        return False

    def grant_for_scheduled_task(
        self,
        user_id: str,  # noqa: ARG002
        scheduled_task_id: str,  # noqa: ARG002
        level: int,  # noqa: ARG002
    ) -> ScheduledTaskPermission:
        """Upsert a grant on a scheduled task, as :meth:`grant` does on a session.

        Overwrites the level of an existing grant. As for session grants, the
        caller authorizes the request and calls :meth:`ensure_user` first. The
        task's owner is implicit and holds no grant, and automations are never
        public.

        :param user_id: The grantee, a user or group, e.g. ``"alice@example.com"``.
        :param scheduled_task_id: The task to grant access to.
        :param level: A level in
            :data:`~omnigent.server.session_sharing.SCHEDULED_TASK_GRANT_LEVELS`.
        :returns: The resulting :class:`ScheduledTaskPermission`.
        :raises OmnigentError: ``INVALID_INPUT`` for the owner, a reserved grantee
            or a level outside the allowlist; ``NOT_FOUND`` when the task is missing
            or deleted; ``FORBIDDEN`` when the store does not support these grants.
        """
        raise OmnigentError(
            "Sharing automations is not supported by this server.",
            code=ErrorCode.FORBIDDEN,
        )

    def revoke_for_scheduled_task(
        self,
        user_id: str,  # noqa: ARG002
        scheduled_task_id: str,  # noqa: ARG002
    ) -> bool:
        """Remove a grant on a scheduled task, as :meth:`revoke` does on a session.

        :param user_id: The grantee to revoke, e.g. ``"alice@example.com"``.
        :param scheduled_task_id: The task to revoke access from.
        :returns: ``True`` if a grant was deleted, ``False`` if none existed.
        """
        return False

    def list_for_scheduled_task(
        self,
        scheduled_task_id: str,  # noqa: ARG002
        *,
        limit: int = 100,  # noqa: ARG002
        after_user_id: str | None = None,  # noqa: ARG002
    ) -> tuple[list[ScheduledTaskPermission], str | None]:
        """Return a scheduled task's grants with cursor pagination.

        Pages exactly like :meth:`list_for_session`. Grants can outlive a task whose
        row was purged, so callers check that the task is live before trusting them.

        :param scheduled_task_id: The task to query.
        :param limit: Max grants to return, 1 to :data:`MAX_GRANTS_PAGE_SIZE`
            (default 100).
        :param after_user_id: Exclusive cursor — return grants with
            user_id > this value.
        :returns: Tuple of (grants ordered by user_id, next_cursor). next_cursor
            is the user_id to pass as after_user_id, or None on the last page.
        :raises OmnigentError: ``INVALID_INPUT`` when *limit* is out of range.
        """
        return [], None

    def resolve_scheduled_task_access(
        self,
        principals: Sequence[str],  # noqa: ARG002
        scheduled_task_id: str,  # noqa: ARG002
    ) -> int | None:
        """Return the highest level any of *principals* holds on a scheduled task.

        Reads grants only: owner and admin rules are the caller's. Grants can outlive
        a task whose row was purged, so callers check that the task is live first.

        :param principals: The caller's user ID and any groups they belong to,
            e.g. ``["alice@example.com"]``.
        :param scheduled_task_id: The task to resolve.
        :returns: The highest granted level, or ``None`` when no principal has a grant.
        """
        return None

    def delete_for_scheduled_task(self, scheduled_task_id: str) -> int:  # noqa: ARG002
        """Delete every grant on a scheduled task.

        Call after the task is deleted, so a concurrent grant sees the deletion
        and is refused instead of leaving a grant behind. Implementations may delete
        in batches that each commit, so a failure can leave some grants; they are
        inert on a deleted task, and calling again finishes. ``ScheduledTaskStore.delete``
        can remove the task row in the same call; grants this call misses are then
        orphans that nothing else removes, which is why readers check the task first.

        :param scheduled_task_id: The deleted task.
        :returns: The number of grants deleted.
        """
        return 0
