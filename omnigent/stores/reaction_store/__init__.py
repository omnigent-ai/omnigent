"""Reaction store: one user's thumbs up/down per assistant response."""

from abc import ABC, abstractmethod

from omnigent.entities import MessageReaction


class ReactionStore(ABC):
    """Abstract base for message-reaction persistence.

    A reaction is keyed by (conversation, response, user): setting it again
    replaces the rating and note, clearing removes the row, and the owning
    conversation's deletion removes them all.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def set(
        self,
        conversation_id: str,
        response_id: str,
        rating: str,
        note: str | None = None,
        created_by: str | None = None,
        harness: str | None = None,
        model: str | None = None,
    ) -> MessageReaction:
        """Create or replace the caller's reaction to a response.

        :param conversation_id: The owning session, e.g. ``"conv_abc123"``.
        :param response_id: The assistant turn, e.g. ``"resp_abc"``.
        :param rating: ``"up"`` or ``"down"``.
        :param note: Optional free text; ``None`` clears a previous note.
        :param created_by: The reacting user, or ``None`` in single-user mode.
        :param harness: Harness that answered, snapshotted now; ``None`` if unknown.
        :param model: Model that answered, snapshotted now; ``None`` if unknown.
        :returns: The persisted :class:`MessageReaction`.
        """
        ...

    @abstractmethod
    def clear(self, conversation_id: str, response_id: str, created_by: str | None = None) -> bool:
        """Remove the caller's reaction to a response.

        :returns: ``True`` when a reaction existed and was removed.
        """
        ...

    @abstractmethod
    def list_for_user(
        self, conversation_id: str, created_by: str | None = None
    ) -> list[MessageReaction]:
        """Return the caller's reactions in a session, oldest first."""
        ...

    @abstractmethod
    def remove_conversation(self, conversation_id: str) -> None:
        """Delete every reaction in a conversation (called on session delete)."""
        ...
