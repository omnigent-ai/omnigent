"""Persistence for versioned, user-scoped web preferences.

The server stores one small JSON envelope on the authenticated user's row.
Namespace updates run in a single database transaction, so concurrent clients
cannot observe a partially-written preference set. The store owns structural
and size validation as a defence-in-depth boundary for non-HTTP callers.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, TypeAlias, cast

from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlUser, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker

USER_PREFERENCE_VERSION = 1
USER_PREFERENCES_MAX_BYTES = 64 * 1024
USER_PREFERENCE_NAMESPACES = frozenset(
    {
        "keyboard_shortcuts",
        "mobile_assistant",
        "session_navigation",
        "context_indicator",
        "usage_context",
        "agent_badges",
    }
)

PreferencesEnvelope: TypeAlias = dict[str, Any]


class UserPreferencesValidationError(ValueError):
    """A preferences payload violates the persisted envelope contract."""


class UserPreferencesUserNotFoundError(LookupError):
    """The authenticated account no longer has a backing user row."""


def _validate_json(value: Any, *, depth: int = 0) -> None:
    """Reject non-JSON values and pathological nesting before serialization."""
    if depth > 32:
        raise UserPreferencesValidationError("preferences nesting exceeds 32 levels")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UserPreferencesValidationError("preferences numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UserPreferencesValidationError("preferences object keys must be strings")
            _validate_json(item, depth=depth + 1)
        return
    raise UserPreferencesValidationError(
        f"preferences values must be JSON-compatible, got {type(value).__name__}"
    )


def validate_preferences_envelope(envelope: Any) -> PreferencesEnvelope:
    """Validate and copy a version-1 preferences envelope.

    ``settings`` is deliberately namespaced and allowlisted. Namespace values
    remain client-versioned JSON (for example, ``context_indicator`` is a
    string while shortcut settings are objects); the server still guarantees
    JSON-only content, bounded nesting, and a 64 KiB serialized cap.
    """
    if not isinstance(envelope, dict) or set(envelope) != {"version", "settings"}:
        raise UserPreferencesValidationError(
            "preferences must contain exactly 'version' and 'settings'"
        )
    if type(envelope["version"]) is not int or envelope["version"] != USER_PREFERENCE_VERSION:
        raise UserPreferencesValidationError("preferences version must be 1")
    settings = envelope["settings"]
    if not isinstance(settings, dict):
        raise UserPreferencesValidationError("preferences settings must be an object")
    unknown = set(settings) - USER_PREFERENCE_NAMESPACES
    if unknown:
        raise UserPreferencesValidationError(
            f"unsupported preferences namespace: {sorted(unknown)[0]}"
        )
    for value in settings.values():
        _validate_json(value)

    copied: PreferencesEnvelope = {
        "version": USER_PREFERENCE_VERSION,
        "settings": deepcopy(settings),
    }
    try:
        serialized = json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UserPreferencesValidationError("preferences strings must be valid UTF-8") from exc
    if len(serialized) > USER_PREFERENCES_MAX_BYTES:
        raise UserPreferencesValidationError("preferences exceed the 64 KiB limit")
    return copied


def _serialize(envelope: PreferencesEnvelope) -> str:
    validated = validate_preferences_envelope(envelope)
    return json.dumps(
        validated,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _deserialize(raw: str) -> PreferencesEnvelope:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise UserPreferencesValidationError("stored preferences are invalid JSON") from exc
    return validate_preferences_envelope(value)


class SqlAlchemyUserPreferencesStore:
    """SQLAlchemy repository for current-user preferences."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        # BEGIN IMMEDIATE closes SQLite's read-then-write race. Other dialects
        # pair the transaction with SELECT FOR UPDATE below.
        self._read_session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.user_preferences_store",
        )
        self._write_session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.user_preferences_store",
            immediate=True,
        )

    def get(self, user_id: str) -> PreferencesEnvelope | None:
        """Return the user's envelope, preserving NULL as uninitialized."""
        with self._read_session("get") as session:
            row = session.get(SqlUser, (current_workspace_id(), user_id))
            if row is None or row.preferences is None:
                return None
            return _deserialize(row.preferences)

    def initialize(
        self,
        user_id: str,
        envelope: PreferencesEnvelope,
        *,
        create_if_missing: bool = True,
    ) -> PreferencesEnvelope:
        """Set the full envelope only when this user is still uninitialized.

        Repeated/racing first-device migrations are idempotent: once any
        client wins, later calls receive the already-persisted value instead
        of overwriting it with stale localStorage.
        """
        encoded = _serialize(envelope)
        for attempt in range(2):
            try:
                with self._write_session("initialize") as session:
                    row = session.get(
                        SqlUser,
                        (current_workspace_id(), user_id),
                        with_for_update=self._engine.dialect.name != "sqlite",
                    )
                    if row is None:
                        if not create_if_missing:
                            raise UserPreferencesUserNotFoundError(user_id)
                        row = SqlUser(id=user_id, is_admin=False, preferences=encoded)
                        session.add(row)
                        # SELECT FOR UPDATE cannot lock a missing PostgreSQL row.
                        # Flush now so a racing first writer is handled below.
                        session.flush()
                        return _deserialize(encoded)
                    if row.preferences is None:
                        row.preferences = encoded
                        return _deserialize(encoded)
                    return _deserialize(row.preferences)
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def patch_namespace(
        self,
        user_id: str,
        namespace: str,
        value: Any | None,
        *,
        create_if_missing: bool = True,
    ) -> PreferencesEnvelope:
        """Atomically shallow-merge one namespace, or delete it with NULL."""
        if namespace not in USER_PREFERENCE_NAMESPACES:
            raise UserPreferencesValidationError(f"unsupported preferences namespace: {namespace}")
        if value is not None:
            _validate_json(value)

        for attempt in range(2):
            try:
                with self._write_session("patch_namespace") as session:
                    row = session.get(
                        SqlUser,
                        (current_workspace_id(), user_id),
                        with_for_update=self._engine.dialect.name != "sqlite",
                    )
                    if row is None:
                        if not create_if_missing:
                            raise UserPreferencesUserNotFoundError(user_id)
                        row = SqlUser(id=user_id, is_admin=False)
                        session.add(row)
                        session.flush()
                    current = (
                        _deserialize(row.preferences)
                        if row.preferences is not None
                        else {"version": USER_PREFERENCE_VERSION, "settings": {}}
                    )
                    settings = cast(dict[str, Any], current["settings"])
                    if value is None:
                        settings.pop(namespace, None)
                    else:
                        existing = settings.get(namespace)
                        if isinstance(existing, dict) and isinstance(value, dict):
                            settings[namespace] = {**existing, **deepcopy(value)}
                        else:
                            settings[namespace] = deepcopy(value)
                    encoded = _serialize(current)
                    row.preferences = encoded
                    return _deserialize(encoded)
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")
