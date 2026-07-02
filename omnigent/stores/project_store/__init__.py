"""Project store — first-class projects and their access-control grants.

A project is a shareable collection of conversations. This store owns both
the project records (``projects`` table) and their ACL
(``project_permissions`` table). The grant side mirrors
:class:`omnigent.stores.permission_store.PermissionStore` exactly — same
levels (1=read, 2=edit, 3=manage, 4=owner) and the same ``"__public__"`` /
``"__members__"`` sentinels — but is keyed by ``project_id`` instead of
``conversation_id``.

A conversation *inherits* its project's grants (resolved in
:mod:`omnigent.server.permissions`), which is what makes "share a project"
cover every chat in it, including chats filed after the share.
"""

from abc import ABC, abstractmethod

from omnigent.entities import Project, ProjectPermission


class ProjectStore(ABC):
    """Abstract base for project persistence + project-level grants."""

    def __init__(self, storage_location: str) -> None:
        """Initialize the project store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    # ── Project records ───────────────────────────────────────────

    @abstractmethod
    def create_project(self, name: str, owner: str | None) -> Project:
        """Create a project and (when *owner* is set) its owner grant.

        :param name: Human-readable name, e.g. ``"Q3 launch"``.
        :param owner: The creator's user id, or ``None`` in single-user
            mode. When set, an owner (level 4) grant is written.
        :returns: The created :class:`Project`.
        :raises ValueError: If *owner* already has a project with *name*.
        """
        ...

    @abstractmethod
    def get_project(self, project_id: str) -> Project | None:
        """Look up a project by id, or ``None`` if it doesn't exist."""
        ...

    @abstractmethod
    def rename_project(self, project_id: str, name: str) -> Project | None:
        """Rename a project. Returns the updated project, or ``None`` if
        missing. Raises ``ValueError`` on a per-owner name collision."""
        ...

    @abstractmethod
    def delete_project(self, project_id: str) -> bool:
        """Delete a project row and its grants.

        The caller is responsible for unfiling the project's conversations
        (setting ``project_id = NULL``) beforehand; on backends without the
        DB-level FK the store cannot rely on ``ON DELETE SET NULL``.

        :returns: ``True`` if a row was deleted.
        """
        ...

    @abstractmethod
    def list_projects_for_user(self, user_id: str | None) -> list[tuple[Project, int]]:
        """Return every project the user can access, with their level.

        Includes projects the user holds a direct grant on plus those
        shared with all members (``"__members__"``). Ordered by name.

        :param user_id: The authenticated user, or ``None``.
        :returns: ``(project, effective_level)`` pairs.
        """
        ...

    # ── Grants (mirror PermissionStore, keyed by project_id) ──────

    @abstractmethod
    def grant(self, user_id: str, project_id: str, level: int) -> ProjectPermission:
        """Upsert a project grant. Caller does authorization."""
        ...

    @abstractmethod
    def revoke(self, user_id: str, project_id: str) -> bool:
        """Remove a project grant. Returns ``True`` if one existed."""
        ...

    @abstractmethod
    def get(self, user_id: str, project_id: str) -> ProjectPermission | None:
        """Look up a single grant, or ``None``."""
        ...

    @abstractmethod
    def list_for_project(self, project_id: str) -> list[ProjectPermission]:
        """Return all grants on a project."""
        ...

    @abstractmethod
    def check_access(self, user_id: str | None, project_id: str, required_level: int) -> bool:
        """Whether *user_id* has ``>= required_level`` on the project.

        Considers the user's direct grant plus the ``"__public__"`` and
        (for authenticated users) ``"__members__"`` sentinels. Does not
        apply the admin bypass — that lives in the resolution layer.
        """
        ...

    @abstractmethod
    def get_permission_level(self, user_id: str | None, project_id: str) -> int | None:
        """The caller's effective level on the project, or ``None``.

        The ``max`` of the user's own grant and the applicable sentinel
        grants — used to fold a project grant into a session's resolved
        level. No admin bypass here.
        """
        ...
