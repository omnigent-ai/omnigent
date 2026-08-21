"""SQLAlchemy-backed elicitation store."""

from __future__ import annotations

import json
from typing import Protocol, cast

from sqlalchemy import delete, select

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlElicitation,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
)
from omnigent.entities import Elicitation
from omnigent.stores.elicitation_store import ElicitationStore


class _RowCountResult(Protocol):
    rowcount: int


def _to_entity(row: SqlElicitation) -> Elicitation:
    """
    Convert a :class:`SqlElicitation` ORM row to an :class:`Elicitation`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: An :class:`Elicitation` dataclass instance.
    """
    return Elicitation(
        id=row.id,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        conversation_id=row.conversation_id,
        created_at=row.created_at,
        event=json.loads(row.event),
    )


class SqlAlchemyElicitationStore(ElicitationStore):
    """
    SQLAlchemy-backed implementation of :class:`ElicitationStore`.

    Persists outstanding approval prompts in a relational database via the
    SQLAlchemy ORM.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy elicitation store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.elicitation_store",
        )

    def put(self, elicitation: Elicitation) -> None:
        """Record an outstanding prompt, replacing any row with the same id."""
        payload = json.dumps(elicitation.event)
        with self._session("upsert_elicitation") as session:
            row = session.get(
                SqlElicitation,
                (current_workspace_id(), elicitation.id),
            )
            if row is None:
                session.add(
                    SqlElicitation(
                        id=elicitation.id,
                        conversation_id=elicitation.conversation_id,
                        created_at=elicitation.created_at,
                        event=payload,
                    )
                )
                return
            row.conversation_id = elicitation.conversation_id
            row.created_at = elicitation.created_at
            row.event = payload

    def delete(self, conversation_id: str, elicitation_id: str) -> bool:
        """Drop a prompt that is no longer outstanding."""
        with self._session("delete_elicitation") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    delete(SqlElicitation).where(
                        SqlElicitation.workspace_id == current_workspace_id(),
                        SqlElicitation.id == elicitation_id,
                        SqlElicitation.conversation_id == conversation_id,
                    )
                ),
            )
            return result.rowcount > 0

    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        not_before: int | None = None,
    ) -> list[Elicitation]:
        """Return the prompts still outstanding for one session, oldest first."""
        with self._session("list_elicitations_for_conversation") as session:
            stmt = select(SqlElicitation).where(
                SqlElicitation.workspace_id == current_workspace_id(),
                SqlElicitation.conversation_id == conversation_id,
            )
            if not_before is not None:
                stmt = stmt.where(SqlElicitation.created_at >= not_before)
            rows = session.scalars(stmt.order_by(SqlElicitation.created_at, SqlElicitation.id))
            return [_to_entity(row) for row in rows]

    def delete_for_conversation(self, conversation_id: str) -> int:
        """Drop every prompt for a session, for referential cleanup on delete."""
        with self._session("delete_elicitations_for_conversation") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    delete(SqlElicitation).where(
                        SqlElicitation.workspace_id == current_workspace_id(),
                        SqlElicitation.conversation_id == conversation_id,
                    )
                ),
            )
            return result.rowcount
