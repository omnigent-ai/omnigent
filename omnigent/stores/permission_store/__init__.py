"""Permission store — manages session-level access grants.

Each grant is a ``(user_id, conversation_id, level)`` triple where
level is an integer: 1=read, 2=edit, 3=manage. The ``"__public__"``
sentinel user ID represents public link access, capped by the live server policy.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence

from omnigent.entities import Account, ResolvedAccess, SessionPermission
from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ

logger = logging.getLogger(__name__)

# Matches the width of users.id and session_permissions.user_id.
_USER_ID_MAX_LENGTH = 128
# session_permissions accepts 1-4; owner (4) is dropped before this check.
_GRANTABLE_LEVELS = (LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE)


def _entry_problem(user_id: object, level: object) -> str | None:
    """Describe why a grant entry cannot be stored, or return ``None`` if it can.

    :param user_id: The grantee ID as passed by the caller.
    :param level: The requested level as passed by the caller.
    :returns: A short description for the log, or ``None``.
    """
    if not isinstance(user_id, str):
        return f"grantee ID is a {type(user_id).__name__}, not a string"
    if not user_id:
        return "grantee ID is empty"
    if len(user_id) > _USER_ID_MAX_LENGTH:
        return f"grantee ID is {len(user_id)} characters, over {_USER_ID_MAX_LENGTH}"
    if "\x00" in user_id:
        return "grantee ID contains a NUL byte"
    if not (isinstance(level, int) and not isinstance(level, bool) and level in _GRANTABLE_LEVELS):
        return f"level {level!r} is not read, edit or manage"
    return None


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

    def level_outranks(self, level: int, other_level: int) -> bool:
        """Return whether *level* grants strictly more access than *other_level*.

        The default :meth:`grant_many` makes every level comparison through this
        method, so a store whose levels are not ordered by integer value overrides
        it here. The SQL override compares integers in SQL, so such a store also
        overrides :meth:`grant_many`.

        :param level: The candidate level, e.g. ``2``.
        :param other_level: The level it is compared with, e.g. ``1``.
        :returns: ``True`` if *level* outranks *other_level*.
        """
        return level > other_level

    def grant_many(
        self,
        conversation_id: str,
        grants: Sequence[tuple[str, int]],
    ) -> list[SessionPermission]:
        """Grant several users on one session without lowering any grant.

        A user listed more than once keeps the highest requested level and is
        written once. A request is skipped when an equal or higher grant already
        exists, and the owner's grant is never written or changed. An entry that
        cannot be stored is logged and skipped before any write: a grantee ID that
        is not a non-empty string of at most 128 characters without a NUL byte, or
        a level other than read, edit or manage. A grantee whose write raises an
        :class:`~omnigent.errors.OmnigentError` is logged and skipped while the rest
        are still granted.

        This default is not atomic. It reads and writes each grantee in turn, so a
        change made between one grantee's read and its write is not guarded. It
        cannot tell a revoked acting account from a refused grantee, so it skips
        both. A store with actor-level authority checks must override this method,
        as the SQL store does. A pin set by ``target_account_scope`` applies to every
        write in the loop, so a replaced pinned account fails each grantee rather
        than only its own.

        :param conversation_id: The session to grant on, e.g. ``"conv_abc123"``.
        :param grants: ``(user_id, level)`` pairs, e.g.
            ``[("alice@example.com", 1)]``. An empty list writes nothing.
        :returns: The grants now stored at the requested level that were missing or
            lower when the call began, in the first-appearance order of their user
            IDs. A concurrent writer that stored the same level may also appear, which
            is accepted.
        """
        written: list[SessionPermission] = []
        for user_id, level in self._collapse_grants(grants).items():
            try:
                existing = self.get(user_id, conversation_id)
                if not self._should_write_grant(level, existing.level if existing else None):
                    continue
                self.ensure_user(user_id)
                written.append(self.grant(user_id, conversation_id, level))
            except OmnigentError as error:
                logger.warning("grant_many skipped grantee %r: %s", user_id, error)
        return written

    def _collapse_grants(self, grants: Sequence[tuple[str, int]]) -> dict[str, int]:
        """Return one requested level per user, in first-appearance order.

        A repeated user keeps the highest requested level. Owner requests are
        dropped, because :meth:`grant_many` never writes an owner grant. An entry
        that the tables cannot store is logged and dropped here, before any write.

        :param grants: ``(user_id, level)`` pairs as passed to :meth:`grant_many`.
        :returns: A mapping from each user ID to its level.
        """
        collapsed: dict[str, int] = {}
        for user_id, level in grants:
            if level == LEVEL_OWNER:
                continue
            problem = _entry_problem(user_id, level)
            if problem is not None:
                logger.warning("grant_many skipped an entry: %s", problem)
                continue
            current = collapsed.get(user_id)
            if current is None or self.level_outranks(level, current):
                collapsed[user_id] = level
        return collapsed

    def _should_write_grant(self, level: int, existing_level: int | None) -> bool:
        """Return whether a grant at *level* should be written over *existing_level*.

        A missing grant is always written. An owner grant is never written, and
        any other grant is written only when *level* outranks it.

        :param level: The requested level.
        :param existing_level: The stored level, or ``None`` if there is no grant.
        :returns: ``True`` if the grant should be written.
        """
        if existing_level is None:
            return True
        return existing_level != LEVEL_OWNER and self.level_outranks(level, existing_level)

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
