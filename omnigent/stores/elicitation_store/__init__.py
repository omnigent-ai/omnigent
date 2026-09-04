"""Elicitation store — persists approval prompts that are still waiting.

Owns the ``elicitations`` table. One row per outstanding prompt; resolving a
prompt deletes its row, so the table holds the parked set and never grows into
a history log.

This is the durable side of :mod:`omnigent.runtime.pending_elicitations`, which
keeps the same set in process memory to serve the sidebar badge and the session
snapshot. The in-memory index stays the read path; the rows exist so a restart
does not take the parked set with it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import Elicitation


class ElicitationStore(ABC):
    """
    Abstract base for outstanding-approval persistence.

    Writes are on the elicitation publish path, which runs at human-approval
    rate — one row per question actually asked — so implementations may be
    synchronous.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the elicitation store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def put(self, elicitation: Elicitation) -> None:
        """
        Record an outstanding prompt, replacing any row with the same id.

        Upsert rather than insert: a prompt may be re-published for the same
        ``elicitation_id`` (the in-memory index treats a repeat publish as an
        overwrite), and several harnesses mint deterministic ids that repeat
        across polls for the same gated tool call.

        :param elicitation: The prompt to record.
        """
        ...

    @abstractmethod
    def delete(self, conversation_id: str, elicitation_id: str) -> bool:
        """
        Drop a prompt that is no longer outstanding.

        Conditional on the row existing, so a verdict racing another resolve
        path has exactly one winner and the loser can tell it lost.

        :param conversation_id: Session the prompt was raised on.
        :param elicitation_id: The prompt's correlation id.
        :returns: ``True`` if a row was deleted, ``False`` if none matched.
        """
        ...

    @abstractmethod
    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        not_before: int | None = None,
    ) -> list[Elicitation]:
        """
        Return the prompts still outstanding for one session, oldest first.

        :param conversation_id: Session to query.
        :param not_before: When given, skip prompts raised earlier than this
            Unix epoch second. Callers pass it to drop prompts too old for any
            awaiter to still be parked on them.
        :returns: Outstanding prompts, in the order they were raised.
        """
        ...

    @abstractmethod
    def delete_for_conversation(self, conversation_id: str) -> int:
        """
        Drop every prompt for a session, for referential cleanup on delete.

        The schema has no foreign keys (Rule R032), so deleting a conversation
        must delete its prompts explicitly.

        :param conversation_id: Session being deleted.
        :returns: Number of rows deleted.
        """
        ...
