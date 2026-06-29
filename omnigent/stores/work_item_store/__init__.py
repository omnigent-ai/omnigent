"""Work-item store — CRUD over the ``work_items`` table.

Work items are tracked units of work (manual, or ingested from Slack /
email / GitHub / Jira) that an agent processes in a linked conversation.
The store enforces idempotency through a globally-unique ``dedup_key`` so a
repeated intake of the same external event resolves to the existing row.
"""

from abc import ABC, abstractmethod

from omnigent.entities import WorkItem


class WorkItemStore(ABC):
    """Abstract base for work-item persistence."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        work_item_id: str,
        source: str,
        title: str,
        *,
        dedup_key: str,
        external_id: str | None = None,
        body: str | None = None,
        status: str = "new",
        conversation_id: str | None = None,
        assignee_user_id: str | None = None,
        created_by: str | None = None,
        plan: str | None = None,
    ) -> WorkItem:
        """
        Insert a new work item. ``dedup_key`` is globally UNIQUE; a
        duplicate raises ``IntegrityError`` (callers wanting idempotency
        should consult :meth:`get_by_dedup_key` first).

        :param work_item_id: Pre-generated id, e.g. ``"wi_a1b2c3..."``.
        :param source: One of :data:`~omnigent.entities.WORK_ITEM_SOURCES`.
        :param title: Short human-readable title.
        :param dedup_key: Idempotency key, globally unique.
        :param external_id: Source-native id, or ``None``.
        :param body: Optional longer description.
        :param status: Initial lifecycle state (default ``"new"``).
        :param conversation_id: Linked conversation, or ``None``.
        :param assignee_user_id: Assignee, or ``None``.
        :param created_by: Creating user id, or ``None``.
        :param plan: Optional plan text/JSON.
        :returns: The created :class:`WorkItem`.
        """
        ...

    @abstractmethod
    def get(self, work_item_id: str) -> WorkItem | None:
        """
        :param work_item_id: Opaque work-item id.
        :returns: The :class:`WorkItem`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def get_by_dedup_key(self, dedup_key: str) -> WorkItem | None:
        """
        Look up a work item by its idempotency key — the basis for an
        idempotent ``create_work_item``.

        :param dedup_key: The globally-unique idempotency key.
        :returns: The :class:`WorkItem`, or ``None`` if none matches.
        """
        ...

    @abstractmethod
    def list(
        self,
        *,
        status: str | None = None,
        conversation_id: str | None = None,
        limit: int = 200,
    ) -> list[WorkItem]:
        """
        List work items, newest first, optionally filtered.

        :param status: Restrict to this lifecycle state, or ``None`` for all.
        :param conversation_id: Restrict to items linked to this
            conversation, or ``None`` for all.
        :param limit: Maximum rows to return.
        :returns: List of :class:`WorkItem`, ordered ``created_at DESC``.
        """
        ...

    @abstractmethod
    def update(
        self,
        work_item_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        pr_url: str | None = None,
        conversation_id: str | None = None,
        assignee_user_id: str | None = None,
        plan: str | None = None,
    ) -> WorkItem | None:
        """
        Update mutable fields. ``source``/``dedup_key`` are immutable.
        Only provided (non-``None``) fields change.

        :returns: The updated :class:`WorkItem`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def delete(self, work_item_id: str) -> bool:
        """
        Delete a work item. Idempotent.

        :returns: ``True`` if a row was removed; ``False`` if not found.
        """
        ...
