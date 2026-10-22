"""Shared reads and upserts for the per-user ``preferences`` table."""

from __future__ import annotations

from sqlalchemy import LargeBinary, Select, select, type_coerce
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.db_models import SqlPreference, current_workspace_id


def select_preference_value(user_id: str, key: str) -> Select[tuple[bytes]]:
    """
    Select one stored preference value as raw bytes in the current workspace.

    Callers decode the bytes themselves so a malformed row cannot fail in the
    ORM result processor.

    :param user_id: Preference owner (``RESERVED_USER_LOCAL`` in single-user mode).
    :param key: Preference name.
    :returns: A select statement yielding at most one raw value.
    """
    return select(type_coerce(SqlPreference.value, LargeBinary)).where(
        SqlPreference.workspace_id == current_workspace_id(),
        SqlPreference.user_id == user_id,
        SqlPreference.key == key,
    )


def upsert_preference(
    session: Session, *, dialect: str, user_id: str, key: str, value: str
) -> None:
    """
    Insert or replace one preference value in the current workspace.

    :param session: Open write session.
    :param dialect: The engine's dialect name, e.g. ``"sqlite"``.
    :param user_id: Preference owner (``RESERVED_USER_LOCAL`` in single-user mode).
    :param key: Preference name.
    :param value: Serialized JSON value.
    """
    values = {
        "workspace_id": current_workspace_id(),
        "user_id": user_id,
        "key": key,
        "value": value,
    }
    stmt: Insert
    if dialect == "mysql":
        stmt = mysql_insert(SqlPreference).values(**values).on_duplicate_key_update(value=value)
    elif dialect == "sqlite":
        stmt = (
            sqlite_insert(SqlPreference)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["workspace_id", "user_id", "key"],
                set_={"value": value},
            )
        )
    else:
        stmt = (
            pg_insert(SqlPreference)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["workspace_id", "user_id", "key"],
                set_={"value": value},
            )
        )
    session.execute(stmt)
