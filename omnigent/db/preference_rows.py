"""Write one named per-user value in the ``preferences`` table."""

from __future__ import annotations

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.db_models import SqlPreference, current_workspace_id


def upsert_preference_row(session: Session, *, user_id: str, key: str, value: str) -> None:
    """Insert or replace a user's value for ``key`` in the current workspace.

    :param session: Open write session; the caller owns the transaction.
    :param user_id: Preference namespace, e.g. ``"alice@example.com"``.
    :param key: Preference name, e.g. ``"project_order"``.
    :param value: Serialized value; the column compresses it on write.
    """
    values = {
        "workspace_id": current_workspace_id(),
        "user_id": user_id,
        "key": key,
        "value": value,
    }
    dialect = session.get_bind().dialect.name
    stmt: Insert
    if dialect == "mysql":
        stmt = mysql_insert(SqlPreference).values(**values).on_duplicate_key_update(value=value)
    elif dialect == "sqlite":
        stmt = (
            sqlite_insert(SqlPreference)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["workspace_id", "user_id", "key"], set_={"value": value}
            )
        )
    else:
        stmt = (
            pg_insert(SqlPreference)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["workspace_id", "user_id", "key"], set_={"value": value}
            )
        )
    session.execute(stmt)
