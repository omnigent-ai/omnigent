"""Policy store — manages policies via the CRUD API.

Supports both session-scoped policies (``session_id`` set) and
server-wide default policies (``session_id IS NULL``).
"""

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import Policy


class PolicyStore(ABC):
    """
    Abstract base for policy persistence.

    Manages the lifecycle of policies created at runtime:

    - **Session-scoped** via
      ``POST /v1/sessions/{session_id}/policies``: composite
      uniqueness on ``(session_id, name)``.
    - **Server-wide defaults** via ``POST /v1/policies``:
      ``session_id`` is ``None``, name is globally unique among
      defaults.

    Three handler types are supported:

    - ``type="python"`` — dotted import path to a callable.
    - ``type="url"`` — HTTPS endpoint of an external policy server.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the policy store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    # ── Session-scoped policy methods ────────────────────────────

    @abstractmethod
    def create(
        self,
        policy_id: str,
        session_id: str,
        name: str,
        type: str,
        handler: str,
        factory_params: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> Policy:
        """
        Insert a new session-scoped policy. Composite uniqueness
        on ``(session_id, name)`` is enforced at the DB layer;
        a duplicate raises ``IntegrityError``.

        :param policy_id: Pre-generated unique policy identifier,
            e.g. ``"pol_a1b2c3..."``.
        :param session_id: The session this policy is scoped to,
            e.g. ``"conv_abc123"``.
        :param name: Human-readable name unique within the
            session, e.g. ``"block_non_feature_branch_push"``.
        :param type: Handler discriminator: ``"python"``,
            ``"url"``.
        :param handler: Dotted import path (python), HTTPS URL
            (url).
        :param factory_params: Optional dict of factory kwargs,
            e.g. ``{"limit": 10}``.
        :param enabled: Whether the engine consults this policy.
            Defaults to ``True``.
        :returns: The newly created :class:`Policy`.
        """
        ...

    @abstractmethod
    def get(self, policy_id: str, session_id: str) -> Policy | None:
        """
        Return a policy if it belongs to the given session.

        :param policy_id: Opaque policy identifier.
        :param session_id: The owning session.
        :returns: The :class:`Policy` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list_for_session(self, session_id: str) -> list[Policy]:
        """
        List policies scoped to a single session, ordered by
        ``created_at ASC``.

        :param session_id: The session whose policies to return.
        :returns: List of :class:`Policy` instances.
        """
        ...

    @abstractmethod
    def update(
        self,
        policy_id: str,
        session_id: str,
        *,
        name: str | None = None,
        handler: str | None = None,
        enabled: bool | None = None,
    ) -> Policy | None:
        """
        Update mutable fields of a policy. ``type`` is immutable.

        Returns ``None`` if not found or not owned by the session.

        :param policy_id: Opaque policy identifier.
        :param session_id: The owning session.
        :param name: New name.
        :param handler: New handler path or URL.
        :param enabled: New enabled flag.
        :returns: The updated :class:`Policy`, or ``None``.
        """
        ...

    @abstractmethod
    def delete(self, policy_id: str, session_id: str) -> bool:
        """
        Delete a policy. Idempotent.

        :param policy_id: Opaque policy identifier.
        :param session_id: The owning session.
        :returns: ``True`` if removed; ``False`` if not found
            or wrong session.
        """
        ...

    # ── Default (server-wide) policy methods ─────────────────────

    @abstractmethod
    def create_default(
        self,
        policy_id: str,
        name: str,
        type: str,
        handler: str,
        factory_params: dict[str, Any] | None = None,
        enabled: bool = True,
        created_by: str | None = None,
    ) -> Policy:
        """
        Insert a new server-wide default policy (``session_id=NULL``).

        Name uniqueness among defaults is enforced at the DB layer
        via the ``(session_id, name)`` composite unique constraint
        (NULL session_id groups together); a duplicate raises
        ``IntegrityError``.

        :param policy_id: Pre-generated unique policy identifier,
            e.g. ``"pol_a1b2c3..."``.
        :param name: Human-readable name, globally unique among
            defaults, e.g. ``"block_non_feature_branch_push"``.
        :param type: Handler discriminator: ``"python"``,
            ``"url"``.
        :param handler: Dotted import path (python), HTTPS URL
            (url).
        :param factory_params: Optional dict of factory kwargs,
            e.g. ``{"limit": 10}``.
        :param enabled: Whether the engine consults this policy.
            Defaults to ``True``.
        :param created_by: User ID of the creating admin, e.g.
            ``"alice@example.com"``. ``None`` in single-user mode.
        :returns: The newly created :class:`Policy`.
        """
        ...

    @abstractmethod
    def get_default(self, policy_id: str) -> Policy | None:
        """
        Return a default policy by ID (``session_id IS NULL``).

        :param policy_id: Opaque policy identifier.
        :returns: The :class:`Policy` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list_defaults(self) -> list[Policy]:
        """
        List all default policies, ordered by ``created_at ASC``.

        :returns: List of :class:`Policy` instances where
            ``session_id IS NULL``.
        """
        ...

    @abstractmethod
    def update_default(
        self,
        policy_id: str,
        *,
        name: str | None = None,
        handler: str | None = None,
        enabled: bool | None = None,
    ) -> Policy | None:
        """
        Update mutable fields of a default policy. ``type`` is
        immutable. Returns ``None`` if not found.

        :param policy_id: Opaque policy identifier.
        :param name: New name.
        :param handler: New handler path or URL.
        :param enabled: New enabled flag.
        :returns: The updated :class:`Policy`, or ``None``.
        """
        ...

    @abstractmethod
    def delete_default(self, policy_id: str) -> bool:
        """
        Delete a default policy. Idempotent.

        :param policy_id: Opaque policy identifier.
        :returns: ``True`` if removed; ``False`` if not found.
        """
        ...
