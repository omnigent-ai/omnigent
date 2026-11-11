"""Workspace and per-user on/off settings for the proactive agent.

Both live in the ``preferences`` table: the workspace setting under a reserved
user id, as session families and host lifecycle records already do, and each
user's override under that user's id. Either is on until someone turns it off.
"""

from __future__ import annotations

import json

import zstandard
from sqlalchemy import LargeBinary, select, type_coerce
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.account_authority import lock_account, require_active_account
from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError

# Reserved owner of workspace-wide rows; account user names start with a letter or digit.
WORKSPACE_SETTINGS_USER = "__workspace_settings__"
_ENABLED_KEY = "proactive_agent.v1.enabled"


def _decode_enabled(raw: bytes | str | memoryview | None) -> bool:
    """A missing or unreadable value is on."""
    if raw is None:
        return True
    try:
        return json.loads(decode(raw, max_decoded_bytes=64) or "true") is not False
    except (ValueError, TypeError, zstandard.ZstdError):
        # Not JSON, over the size cap, or a corrupt compressed frame.
        return True


class ProactiveAgentSettingsStore:
    """Reads and writes the workspace setting and each user's override."""

    def __init__(self, storage_location: str) -> None:
        """Initialize the store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.proactive_agent_settings",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.proactive_agent_settings",
            immediate=True,
        )

    def read(self, user_id: str) -> tuple[bool, bool]:
        """Return ``(workspace_enabled, user_enabled)`` for *user_id* in one query."""
        stmt = select(SqlPreference.user_id, type_coerce(SqlPreference.value, LargeBinary)).where(
            SqlPreference.workspace_id == current_workspace_id(),
            SqlPreference.user_id.in_([WORKSPACE_SETTINGS_USER, user_id]),
            SqlPreference.key == _ENABLED_KEY,
        )
        with self._session("read_proactive_agent_settings") as session:
            values = dict(session.execute(stmt).tuples().all())
        return (
            _decode_enabled(values.get(WORKSPACE_SETTINGS_USER)),
            _decode_enabled(values.get(user_id)),
        )

    def set_workspace_enabled(
        self, enabled: bool, *, actor: str | None, require_admin: bool = False
    ) -> None:
        """Turn the agent on or off for the whole workspace, on *actor*'s behalf.

        The route checked the actor before calling; the actor's account is
        checked again under its lock in the same transaction as the write, so
        a request that was in flight when the actor was deleted or demoted
        cannot land. With *require_admin* the actor must still be an admin;
        identities without an accounts row (header, OIDC, the local user) have
        no row to check and keep the route's decision.
        """

        def write(session: Session) -> None:
            require_active_account(session, actor)
            if require_admin and actor is not None:
                row = lock_account(session, actor)
                if row is not None and not row.is_admin:
                    raise OmnigentError(
                        "Admin privileges required to change the workspace setting",
                        code=ErrorCode.FORBIDDEN,
                    )
            self._write(session, WORKSPACE_SETTINGS_USER, enabled)

        run_write_transaction(
            self._session_immediate, "write_proactive_agent_workspace_setting", write
        )

    def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        """Record *user_id*'s own choice."""

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            self._write(session, user_id, enabled)

        run_write_transaction(self._session_immediate, "write_proactive_agent_user_setting", write)

    def _write(self, session: Session, owner: str, enabled: bool) -> None:
        value = json.dumps(enabled)
        values = {
            "workspace_id": current_workspace_id(),
            "user_id": owner,
            "key": _ENABLED_KEY,
            "value": value,
        }
        dialect = self._engine.dialect.name
        stmt: Insert
        if dialect == "mysql":
            stmt = (
                mysql_insert(SqlPreference).values(**values).on_duplicate_key_update(value=value)
            )
        else:
            insert = sqlite_insert if dialect == "sqlite" else pg_insert
            stmt = (
                insert(SqlPreference)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["workspace_id", "user_id", "key"],
                    set_={"value": value},
                )
            )
        session.execute(stmt)
