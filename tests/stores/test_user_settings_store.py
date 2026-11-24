"""Tests for per-user settings the server applies on a user's behalf."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import column, insert, table

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.server.server_config import SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
from omnigent.stores.host_store import host_session_defaults_key
from omnigent.stores.user_settings_store import (
    HOST_SESSION_DEFAULTS_MAX_BYTES,
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


def _write_raw_value(
    db_uri: str,
    user_id: str,
    value: bytes,
    key: str = SESSION_TITLE_INSTRUCTIONS_PREFERENCE_KEY,
) -> None:
    with get_or_create_engine(db_uri).begin() as connection:
        # Untyped columns skip the value codec while letting each dialect quote ``key``.
        raw_preferences = table(
            "preferences",
            column("workspace_id"),
            column("user_id"),
            column("key"),
            column("value"),
        )
        connection.execute(
            insert(raw_preferences).values(
                workspace_id=0,
                user_id=user_id,
                key=key,
                value=value,
            )
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


def test_instructions_are_isolated_per_workspace(store: UserSettingsStore) -> None:
    with workspace_scope(101):
        store.save_session_title_instructions("Workspace one rule.", user_id=ALICE)
    with workspace_scope(102):
        assert store.get_session_title_instructions(user_id=ALICE) is None
        store.save_session_title_instructions("Workspace two rule.", user_id=ALICE)
    with workspace_scope(101):
        assert store.get_session_title_instructions(user_id=ALICE) == "Workspace one rule."
    with workspace_scope(102):
        store.save_session_title_instructions(None, user_id=ALICE)
        assert store.get_session_title_instructions(user_id=ALICE) is None
    with workspace_scope(101):
        assert store.get_session_title_instructions(user_id=ALICE) == "Workspace one rule."


@pytest.mark.parametrize(
    "character",
    [pytest.param("x", id="ascii"), pytest.param("\N{ROCKET}", id="four-byte")],
)
def test_save_accepts_limit_and_rejects_longer(store: UserSettingsStore, character: str) -> None:
    at_limit = character * SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
    assert store.save_session_title_instructions(at_limit, user_id=ALICE) == at_limit
    assert store.get_session_title_instructions(user_id=ALICE) == at_limit

    with pytest.raises(ValueError, match="exceed 4000 characters"):
        store.save_session_title_instructions(at_limit + character, user_id=ALICE)
    assert store.get_session_title_instructions(user_id=ALICE) == at_limit


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(b"\x00\x07unknown codec", id="unknown-codec"),
        pytest.param(b"\x00\x01not a zstd frame", id="corrupt-zstd"),
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


HOST_ID = "a1ed1ab71de2311e20488a989e61701c"
OTHER_HOST_ID = "b1b5efd7dfc33b5a6241f1866ffb00e6"


def test_host_session_defaults_round_trip_and_clear(store: UserSettingsStore) -> None:
    assert store.get_host_session_defaults(HOST_ID, user_id=ALICE) == {}

    defaults = {"agent_id": "187b7cb7ac30abf4debfaa578d052ec6", "workspace": "/work/app"}
    assert store.save_host_session_defaults(HOST_ID, defaults, user_id=ALICE) == defaults
    assert store.get_host_session_defaults(HOST_ID, user_id=ALICE) == defaults

    store.save_host_session_defaults(HOST_ID, {"workspace": "/work/other"}, user_id=ALICE)
    assert store.get_host_session_defaults(HOST_ID, user_id=ALICE) == {"workspace": "/work/other"}

    assert store.save_host_session_defaults(HOST_ID, {}, user_id=ALICE) == {}
    assert store.get_host_session_defaults(HOST_ID, user_id=ALICE) == {}


def test_host_session_defaults_are_isolated_per_host_and_user(store: UserSettingsStore) -> None:
    store.save_host_session_defaults(HOST_ID, {"workspace": "/alice"}, user_id=ALICE)
    store.save_host_session_defaults(HOST_ID, {"workspace": "/local"}, user_id=None)

    assert store.get_host_session_defaults(HOST_ID, user_id=ALICE) == {"workspace": "/alice"}
    assert store.get_host_session_defaults(HOST_ID, user_id=BOB) == {}
    assert store.get_host_session_defaults(HOST_ID, user_id=None) == {"workspace": "/local"}
    assert store.get_host_session_defaults(OTHER_HOST_ID, user_id=ALICE) == {}
    # A dashed spelling of the same host id reads the same row.
    dashed = "a1ed1ab7-1de2-311e-2048-8a989e61701c"
    assert store.get_host_session_defaults(dashed, user_id=ALICE) == {"workspace": "/alice"}


def test_host_session_defaults_reject_oversized_value(store: UserSettingsStore) -> None:
    store.save_host_session_defaults(HOST_ID, {"workspace": "/keep"}, user_id=ALICE)

    with pytest.raises(ValueError, match=f"exceed {HOST_SESSION_DEFAULTS_MAX_BYTES} bytes"):
        store.save_host_session_defaults(
            HOST_ID, {"workspace": "/" + "x" * HOST_SESSION_DEFAULTS_MAX_BYTES}, user_id=ALICE
        )
    assert store.get_host_session_defaults(HOST_ID, user_id=ALICE) == {"workspace": "/keep"}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param(b"\x00\x07unknown codec", {}, id="unknown-codec"),
        pytest.param(b"not json", {}, id="not-json"),
        pytest.param(b'["/work"]', {}, id="not-an-object"),
        pytest.param(b'{"workspace": "/work", "agent_id": 7}', {"workspace": "/work"}, id="mixed"),
        pytest.param(b"x" * (HOST_SESSION_DEFAULTS_MAX_BYTES + 1), {}, id="over-limit"),
    ],
)
def test_unreadable_host_session_defaults_are_ignored(
    store: UserSettingsStore, db_uri: str, stored: bytes, expected: dict[str, str]
) -> None:
    _write_raw_value(db_uri, ALICE, stored, key=host_session_defaults_key(HOST_ID))

    assert store.get_host_session_defaults(HOST_ID, user_id=ALICE) == expected
