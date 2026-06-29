"""Canvas store — one rendered artifact per conversation.

The agent writes via ``upsert`` (overwriting the conversation's canvas); the
web UI reads via ``get_by_conversation``.
"""

from abc import ABC, abstractmethod

from omnigent.entities.canvas import Canvas


class CanvasStore(ABC):
    """Abstract base for canvas persistence (one canvas per conversation)."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def get_by_conversation(self, conversation_id: str) -> Canvas | None:
        """
        :param conversation_id: The owning conversation.
        :returns: The :class:`Canvas`, or ``None`` if none is set.
        """
        ...

    @abstractmethod
    def upsert(
        self,
        canvas_id: str,
        conversation_id: str,
        title: str,
        content: str,
        content_type: str,
    ) -> Canvas:
        """
        Create or overwrite the conversation's canvas.

        :param canvas_id: Id to use if creating (ignored on overwrite).
        :param conversation_id: The owning conversation (unique key).
        :param title: Tab title.
        :param content: HTML or Markdown source.
        :param content_type: ``"html"`` or ``"markdown"``.
        :returns: The stored :class:`Canvas`.
        """
        ...

    @abstractmethod
    def delete(self, conversation_id: str) -> bool:
        """
        Delete a conversation's canvas. Idempotent.

        :param conversation_id: The owning conversation.
        :returns: ``True`` if a canvas was removed; ``False`` if none existed.
        """
        ...
