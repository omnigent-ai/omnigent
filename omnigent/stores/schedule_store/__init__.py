"""Schedule store — CRUD over the ``schedules`` table.

Schedules are loops (cron-driven prompts) and monitors (stream-driven
prompts) scoped to a conversation. The scheduler service reads enabled
schedules at startup and on mutation to (re)arm them.
"""

from abc import ABC, abstractmethod

from omnigent.entities.schedule import Schedule


class ScheduleStore(ABC):
    """Abstract base for schedule persistence."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        schedule_id: str,
        conversation_id: str,
        name: str,
        kind: str,
        prompt: str,
        *,
        cron: str | None = None,
        command: str | None = None,
        enabled: bool = True,
        created_by_user_id: str | None = None,
    ) -> Schedule:
        """
        Insert a new schedule. Composite uniqueness on
        ``(conversation_id, name)`` is enforced at the DB layer; a
        duplicate raises ``IntegrityError``.

        :param schedule_id: Pre-generated id, e.g. ``"sch_a1b2c3..."``.
        :param conversation_id: The conversation the schedule fires into.
        :param name: Name, unique within the conversation.
        :param kind: ``"loop"`` or ``"monitor"``.
        :param prompt: The prompt (or monitor template) to fire.
        :param cron: Cron expression (loops).
        :param command: Shell command to stream (monitors).
        :param enabled: Whether the scheduler runs it (default ``True``).
        :param created_by_user_id: Creating user id, or ``None``.
        :returns: The created :class:`Schedule`.
        """
        ...

    @abstractmethod
    def get(self, schedule_id: str) -> Schedule | None:
        """
        :param schedule_id: Opaque schedule id.
        :returns: The :class:`Schedule`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def list_for_conversation(self, conversation_id: str) -> list[Schedule]:
        """
        List schedules for a conversation, ordered ``created_at ASC``.

        :param conversation_id: The owning conversation.
        :returns: List of :class:`Schedule`.
        """
        ...

    @abstractmethod
    def list_enabled(self) -> list[Schedule]:
        """
        List all enabled schedules across conversations — used by the
        scheduler service to arm jobs at startup.

        :returns: List of enabled :class:`Schedule`.
        """
        ...

    @abstractmethod
    def update(
        self,
        schedule_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        cron: str | None = None,
        command: str | None = None,
        enabled: bool | None = None,
        status: str | None = None,
        last_fired_at: int | None = None,
        last_run_id: str | None = None,
    ) -> Schedule | None:
        """
        Update mutable fields. ``kind``/``conversation_id`` are immutable.
        Only provided (non-``None``) fields change.

        :returns: The updated :class:`Schedule`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def delete(self, schedule_id: str) -> bool:
        """
        Delete a schedule. Idempotent.

        :returns: ``True`` if a row was removed; ``False`` if not found.
        """
        ...
