"""Workspace and per-user on/off settings for the proactive agent.

Both live in the ``preferences`` table: the workspace setting under a reserved
user id, as session families and host lifecycle records already do, and each
user's override under that user's id. Either is on until someone turns it off.
"""

from __future__ import annotations

import json

import zstandard
from sqlalchemy import LargeBinary, delete, select, type_coerce
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
_PROMPT_KEY = "proactive_agent.v1.prompt"
# Longest prompt override, in UTF-8 bytes, so the stored value stays under the
# 16,383-byte cap for a database value even uncompressed.
MAX_PROMPT_OVERRIDE_BYTES = 16_000
# The stored JSON text may be longer than the prompt (escapes); readers stop here.
_MAX_STORED_PROMPT_BYTES = 4 * MAX_PROMPT_OVERRIDE_BYTES


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

    def read_prompt_override(self) -> str | None:
        """Return the admin's replacement for the agent's built-in prompt, if any."""
        stmt = select(type_coerce(SqlPreference.value, LargeBinary)).where(
            SqlPreference.workspace_id == current_workspace_id(),
            SqlPreference.user_id == WORKSPACE_SETTINGS_USER,
            SqlPreference.key == _PROMPT_KEY,
        )
        with self._session("read_proactive_agent_prompt") as session:
            raw = session.scalar(stmt)
        if raw is None:
            return None
        try:
            prompt = json.loads(decode(raw, max_decoded_bytes=_MAX_STORED_PROMPT_BYTES) or "")
        except (ValueError, TypeError):
            return None
        return prompt if isinstance(prompt, str) and prompt.strip() else None

    def set_prompt_override(self, prompt: str | None) -> None:
        """Replace the agent's built-in prompt for the workspace; ``None`` restores it.

        :raises ValueError: If *prompt* is longer than
            :data:`MAX_PROMPT_OVERRIDE_BYTES` bytes of UTF-8, or its JSON text is
            longer than readers accept.
        """
        if prompt is not None and len(prompt.encode("utf-8")) > MAX_PROMPT_OVERRIDE_BYTES:
            raise ValueError(f"the prompt is longer than {MAX_PROMPT_OVERRIDE_BYTES} bytes")
        if (
            prompt is not None
            and len(json.dumps(prompt).encode("utf-8")) > _MAX_STORED_PROMPT_BYTES
        ):
            raise ValueError("the prompt has too many characters that need escaping")

        def write(session: Session) -> None:
            if prompt is None:
                session.execute(
                    delete(SqlPreference).where(
                        SqlPreference.workspace_id == current_workspace_id(),
                        SqlPreference.user_id == WORKSPACE_SETTINGS_USER,
                        SqlPreference.key == _PROMPT_KEY,
                    )
                )
            else:
                self._write(session, WORKSPACE_SETTINGS_USER, prompt, key=_PROMPT_KEY)

        run_write_transaction(self._session_immediate, "write_proactive_agent_prompt", write)

    def _write(
        self, session: Session, owner: str, setting: bool | str, *, key: str = _ENABLED_KEY
    ) -> None:
        value = json.dumps(setting)
        values = {
            "workspace_id": current_workspace_id(),
            "user_id": owner,
            "key": key,
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
