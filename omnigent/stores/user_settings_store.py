"""Per-user settings the server applies on the user's behalf.

Values live in the ``preferences`` table beside the session metadata, so any
deployment with a SQL conversation store has them without extra wiring.
Account deletion already removes a user's ``preferences`` rows.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import zstandard
from sqlalchemy import LargeBinary, delete, select, type_coerce
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.preference_rows import upsert_preference_row
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.server_config import (
    SESSION_TITLE_INSTRUCTIONS_MAX_CHARS,
    normalize_session_title_instructions,
)

_logger = logging.getLogger(__name__)

SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY = "session_title_instructions"
# UTF-8 needs at most four bytes per character.
_MAX_DECODED_BYTES = SESSION_TITLE_INSTRUCTIONS_MAX_CHARS * 4


class UserSettingsStore:
    """Read and write one user's server-applied settings.

    ``user_id=None`` is single-user mode and maps to the reserved ``local``
    namespace, matching the other per-user preference readers.
    """

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI of the Omnigent DB,
            e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.user_settings_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.user_settings_store", immediate=True
        )

    def get_session_title_instructions(self, *, user_id: str | None) -> str | None:
        """Return the user's saved title instructions, or ``None`` when unset or invalid."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id
        with self._session("read_session_title_instructions") as session:
            # Decode raw bytes here so a malformed value cannot fail in the ORM result processor.
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary)).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
                )
            )
        if raw is None:
            return None
        try:
            value = decode(raw, max_decoded_bytes=_MAX_DECODED_BYTES)
        except (ValueError, zstandard.ZstdError):
            _logger.warning("stored session title instructions are unreadable — ignoring them")
            return None
        return normalize_session_title_instructions(
            value, source="saved session title instructions"
        )

    def save_session_title_instructions(
        self, instructions: str | None, *, user_id: str | None
    ) -> str | None:
        """Save or clear the user's title instructions.

        :param instructions: New instructions; ``None`` or blank clears them.
        :param user_id: The authenticated user, or ``None`` in single-user mode.
        :returns: The stored, stripped instructions, or ``None`` when cleared.
        :raises ValueError: If the instructions exceed the character limit.
        """
        value = instructions.strip() if instructions else ""
        if len(value) > SESSION_TITLE_INSTRUCTIONS_MAX_CHARS:
            limit = SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
            raise ValueError(f"Session title instructions exceed {limit} characters")
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            if value:
                upsert_preference_row(
                    session,
                    user_id=preference_user_id,
                    key=SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
                    value=value,
                )
                return
            session.execute(
                delete(SqlPreference).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
                )
            )

        run_write_transaction(self._session_immediate, "save_session_title_instructions", write)
        return value or None


# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, UserSettingsStore] = {}
_stores_lock = threading.Lock()


def open_user_settings_store(conversation_store: object) -> UserSettingsStore | None:
    """Return the settings store beside the session metadata, or ``None`` without SQL.

    :param conversation_store: The server's conversation store; settings live
        in the database at its ``storage_location``.
    """
    location = getattr(conversation_store, "storage_location", None)
    if not isinstance(location, str) or not location:
        return None
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            try:
                store = UserSettingsStore(location)
            except Exception:
                _logger.warning("User settings storage is unavailable", exc_info=True)
                return None
            _stores[location] = store
        return store


def session_owner_title_instructions(
    store: UserSettingsStore,
    conversation_store: Any,
    *,
    single_user: bool,
) -> Callable[[str], str | None]:
    """Build a resolver from a session id to its owner's title instructions.

    :param store: Where the owner's settings live.
    :param conversation_store: Answers ``get_session_owner`` for the session.
    :param single_user: Whether the server runs without authentication; then
        every session belongs to the local user.
    :returns: A callable that returns ``None`` when the owner is unknown or
        has saved no instructions.
    """

    def resolve(session_id: str) -> str | None:
        if single_user:
            return store.get_session_title_instructions(user_id=None)
        owner = conversation_store.get_session_owner(session_id, owner_only=True)
        if owner is None:
            return None
        return store.get_session_title_instructions(user_id=owner)

    return resolve
