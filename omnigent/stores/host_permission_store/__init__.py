"""Host permission store — manages host-level access grants.

Each grant is a ``(user_id, host_id, level)`` triple where level is an
integer: 1=view, 2=use, 3=manage. Unlike the session permission store
there is no ``"__public__"`` sentinel — host sharing is explicit
per-user opt-in (see the feature spec, SR-007).

The host owner (``hosts.owner``) and admins are NOT stored here; their
access is resolved in :func:`omnigent.server.host_permissions.check_host_access`.
"""

from abc import ABC, abstractmethod

from omnigent.entities import HostPermission


class HostPermissionStore(ABC):
    """Abstract base for host permission persistence.

    Manages grants between users and hosts they do not own. A separate
    store from :class:`~omnigent.stores.permission_store.PermissionStore`
    because session permissions are keyed by ``conversation_id`` and
    carry ``"__public__"`` semantics that must not transfer to hosts.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the host permission store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def grant(
        self,
        user_id: str,
        host_id: str,
        level: int,
        *,
        created_by: str | None = None,
    ) -> HostPermission:
        """Upsert a host permission grant.

        If the user already has a grant on this host, the level is
        overwritten (upgrade or downgrade); ``created_at`` and the
        original ``created_by`` are preserved. The caller is responsible
        for authorization (only owner/admin/manage may grant).

        :param user_id: The grantee, e.g. ``"alice@example.com"``.
        :param host_id: The host to grant access to, e.g.
            ``"host_a1b2c3d4..."``.
        :param level: Numeric permission level (1=view, 2=use, 3=manage).
        :param created_by: User ID recording who created the grant, set
            only on first insert.
        :returns: The resulting :class:`HostPermission`.
        """
        ...

    @abstractmethod
    def revoke(self, user_id: str, host_id: str) -> bool:
        """Remove a host permission grant.

        No-op if the grant does not exist (returns ``False``).

        :param user_id: The grantee to revoke.
        :param host_id: The host to revoke access from.
        :returns: ``True`` if a row was deleted, ``False`` otherwise.
        """
        ...

    @abstractmethod
    def get(self, user_id: str, host_id: str) -> HostPermission | None:
        """Look up a single host permission grant.

        :param user_id: The grantee.
        :param host_id: The host.
        :returns: The :class:`HostPermission` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list_for_host(self, host_id: str) -> list[HostPermission]:
        """Return all grants on a host.

        :param host_id: The host to query, e.g. ``"host_a1b2c3d4..."``.
        :returns: List of :class:`HostPermission` objects.
        """
        ...

    @abstractmethod
    def list_for_user(self, user_id: str) -> list[HostPermission]:
        """Return all grants for a user.

        The hot path backing "list hosts I can access".

        :param user_id: The user to query, e.g. ``"alice@example.com"``.
        :returns: List of :class:`HostPermission` objects.
        """
        ...

    @abstractmethod
    def check_access(
        self,
        user_id: str | None,
        host_id: str,
        required_level: int,
    ) -> bool:
        """Check whether *user_id* has a grant at *required_level* or above.

        Considers only the user's direct grant. Does NOT handle owner
        short-circuit or admin bypass — those are resolution policy in
        :func:`omnigent.server.host_permissions.check_host_access`.

        :param user_id: The authenticated user, or ``None``.
        :param host_id: The host to check.
        :param required_level: Minimum numeric level needed.
        :returns: ``True`` if a sufficient grant exists, ``False`` otherwise.
        """
        ...

    @abstractmethod
    def get_permission_level(
        self,
        user_id: str | None,
        host_id: str,
    ) -> int | None:
        """Return the user's effective grant level for UI display.

        Returns the user's direct grant level, or ``None`` when they have
        no grant. Does NOT apply owner/admin resolution — callers layer
        that on (owner/admin display as ``owner``).

        :param user_id: The authenticated user, or ``None``.
        :param host_id: The host to check.
        :returns: Numeric level (1/2/3), or ``None``.
        """
        ...
