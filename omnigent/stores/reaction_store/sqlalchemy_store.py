"""SQLAlchemy-backed :class:`ReactionStore`."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMessageReaction, current_workspace_id
from omnigent.db.enum_codecs import decode_reaction_rating, encode_reaction_rating
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)
from omnigent.entities import MessageReaction
from omnigent.stores.reaction_store import ReactionStore

# ``created_by`` is part of the PK, so single-user mode stores the empty
# string rather than NULL and maps it back to ``None`` on read.
_ANONYMOUS = ""


def _to_entity(row: SqlMessageReaction) -> MessageReaction:
    """Convert an ORM row to a :class:`MessageReaction`."""
    return MessageReaction(
        conversation_id=row.conversation_id,
        response_id=row.response_id,
        rating=decode_reaction_rating(row.rating),
        note=row.note,
        harness=row.harness,
        model=row.model,
        created_by=row.created_by or None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyReactionStore(ReactionStore):
    """SQLAlchemy implementation of :class:`ReactionStore`."""

    def __init__(self, storage_location: str) -> None:
        """Initialize the store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.reaction_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.reaction_store", immediate=True
        )

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
        """Create or replace a reaction. See base class for contract."""
        code = encode_reaction_rating(rating)
        now_us = now_epoch_us()
        key = (current_workspace_id(), conversation_id, response_id, created_by or _ANONYMOUS)

        def write(session: Session) -> MessageReaction:
            row = session.get(SqlMessageReaction, key)
            if row is None:
                row = SqlMessageReaction(
                    conversation_id=conversation_id,
                    response_id=response_id,
                    created_by=created_by or _ANONYMOUS,
                    rating=code,
                    harness=harness,
                    model=model,
                    note=note,
                    created_at=now_us // 1_000_000,
                    updated_at=now_us,
                )
                session.add(row)
            else:
                row.rating = code
                row.note = note
                row.harness = harness
                row.model = model
                row.updated_at = now_us
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "upsert_message_reaction", write)

    def clear(self, conversation_id: str, response_id: str, created_by: str | None = None) -> bool:
        """Remove a reaction. See base class for contract."""
        key = (current_workspace_id(), conversation_id, response_id, created_by or _ANONYMOUS)

        def write(session: Session) -> bool:
            row = session.get(SqlMessageReaction, key)
            if row is None:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_message_reaction", write)

    def list_for_user(
        self, conversation_id: str, created_by: str | None = None
    ) -> list[MessageReaction]:
        """List a user's reactions in a session. See base class for contract."""
        stmt = (
            select(SqlMessageReaction)
            .where(
                SqlMessageReaction.workspace_id == current_workspace_id(),
                SqlMessageReaction.conversation_id == conversation_id,
                SqlMessageReaction.created_by == (created_by or _ANONYMOUS),
            )
            .order_by(SqlMessageReaction.created_at, SqlMessageReaction.response_id)
        )
        with self._session("list_message_reactions") as session:
            return [_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def remove_conversation(self, conversation_id: str) -> None:
        """Delete a conversation's reactions. See base class for contract."""
        stmt = delete(SqlMessageReaction).where(
            SqlMessageReaction.workspace_id == current_workspace_id(),
            SqlMessageReaction.conversation_id == conversation_id,
        )

        def write(session: Session) -> None:
            session.execute(stmt)

        run_write_transaction(self._session_immediate, "delete_conversation_reactions", write)
