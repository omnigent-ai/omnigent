"""Tests for per-user settings the server applies on a user's behalf."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.server.server_config import SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
from omnigent.stores.user_settings_store import (
    SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
    UserSettingsStore,
    open_user_settings_store,
    session_owner_title_instructions,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture()
def store(db_uri: str) -> UserSettingsStore:
    return UserSettingsStore(db_uri)


def _write_raw_value(db_uri: str, user_id: str, value: bytes) -> None:
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO preferences (workspace_id, user_id, key, value) "
                "VALUES (0, :user_id, :key, :value)"
            ),
            {"user_id": user_id, "key": SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY, "value": value},
        )


def test_save_round_trips_stripped_instructions(store: UserSettingsStore) -> None:
    saved = store.save_session_title_instructions("  Start with an emoji.  ", user_id=ALICE)

    assert saved == "Start with an emoji."
    assert store.get_session_title_instructions(user_id=ALICE) == "Start with an emoji."


def test_save_replaces_previous_instructions(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("First rule.", user_id=ALICE)
    store.save_session_title_instructions("Second rule.", user_id=ALICE)

    assert store.get_session_title_instructions(user_id=ALICE) == "Second rule."


@pytest.mark.parametrize("cleared", [None, "", "   \n"])
def test_blank_save_clears_instructions(store: UserSettingsStore, cleared: str | None) -> None:
    store.save_session_title_instructions("Start with an emoji.", user_id=ALICE)

    assert store.save_session_title_instructions(cleared, user_id=ALICE) is None
    assert store.get_session_title_instructions(user_id=ALICE) is None


def test_instructions_are_isolated_per_user(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("Alice rule.", user_id=ALICE)
    store.save_session_title_instructions("Local rule.", user_id=None)

    assert store.get_session_title_instructions(user_id=ALICE) == "Alice rule."
    assert store.get_session_title_instructions(user_id=BOB) is None
    assert store.get_session_title_instructions(user_id=None) == "Local rule."


def test_save_accepts_limit_and_rejects_longer(store: UserSettingsStore) -> None:
    at_limit = "x" * SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
    assert store.save_session_title_instructions(at_limit, user_id=ALICE) == at_limit

    with pytest.raises(ValueError, match="exceed 4000 characters"):
        store.save_session_title_instructions(at_limit + "x", user_id=ALICE)
    assert store.get_session_title_instructions(user_id=ALICE) == at_limit


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(b"\x00\x07unknown codec", id="unknown-codec"),
        pytest.param(b"\xff\xfe not utf-8", id="invalid-utf8"),
        pytest.param(b"x" * (SESSION_TITLE_INSTRUCTIONS_MAX_CHARS + 1), id="over-limit"),
        pytest.param(b"   ", id="blank"),
    ],
)
def test_invalid_stored_value_falls_back_to_default(
    store: UserSettingsStore, db_uri: str, stored: bytes
) -> None:
    _write_raw_value(db_uri, ALICE, stored)

    assert store.get_session_title_instructions(user_id=ALICE) is None


def test_open_store_requires_sql_storage(db_uri: str) -> None:
    assert open_user_settings_store(object()) is None
    first = open_user_settings_store(SimpleNamespace(storage_location=db_uri))
    assert first is not None
    assert open_user_settings_store(SimpleNamespace(storage_location=db_uri)) is first


def test_owner_resolver_reads_session_owner(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("Alice rule.", user_id=ALICE)
    owners = {"conv_alice": ALICE, "conv_bob": BOB}
    calls: list[tuple[str, bool]] = []

    def get_session_owner(session_id: str, *, owner_only: bool = False) -> str | None:
        calls.append((session_id, owner_only))
        return owners.get(session_id)

    resolve = session_owner_title_instructions(
        store, SimpleNamespace(get_session_owner=get_session_owner), single_user=False
    )

    assert resolve("conv_alice") == "Alice rule."
    assert resolve("conv_bob") is None
    assert resolve("conv_unowned") is None
    assert calls == [("conv_alice", True), ("conv_bob", True), ("conv_unowned", True)]


def test_single_user_resolver_reads_local_user(store: UserSettingsStore) -> None:
    store.save_session_title_instructions("Local rule.", user_id=None)

    def unexpected_owner_lookup(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("single-user mode has one owner")

    resolve = session_owner_title_instructions(
        store, SimpleNamespace(get_session_owner=unexpected_owner_lookup), single_user=True
    )

    assert resolve("conv_any") == "Local rule."
