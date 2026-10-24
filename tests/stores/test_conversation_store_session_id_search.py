"""Session search finds a session by a pasted Omnigent or harness session id."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import event

from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
    _session_id_search_terms,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_ALICE = "alice@example.com"
_BOB = "bob@example.com"


@pytest.fixture(params=["single_db", "split_db"])
def stores(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    """A conversation store and the permission store sharing its Omnigent DB."""
    omnigent_uri = f"sqlite:///{tmp_path}/omnigent.db"
    if request.param == "single_db":
        conversation_store = SqlAlchemyConversationStore(omnigent_uri)
    else:
        conversation_store = SqlAlchemyConversationStore(
            omnigent_uri, f"sqlite:///{tmp_path}/conversations.db"
        )
    permission_store = SqlAlchemyPermissionStore(omnigent_uri)
    for user in (_ALICE, _BOB):
        permission_store.ensure_user(user)
    return conversation_store, permission_store


def _archived_ids(store: SqlAlchemyConversationStore, query: str) -> set[str]:
    """Ids the Archived settings view lists for *query* as Alice."""
    page = store.list_conversations(
        search_query=query,
        accessible_by=_ALICE,
        include_archived=True,
        archived_only=True,
    )
    return {conversation.id for conversation in page.data}


def test_search_finds_archived_session_by_any_spelling_of_its_id(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
) -> None:
    """A pasted Omnigent id matches exactly, whatever spelling the user copied."""
    store, permissions = stores
    target = store.create_conversation(title="Quarterly planning")
    other = store.create_conversation(title="Unrelated")
    for conversation in (target, other):
        store.update_conversation(conversation.id, archived=True)
        permissions.grant(_ALICE, conversation.id, LEVEL_OWNER)

    dashed = str(uuid.UUID(target.id))
    for spelling in (target.id, dashed, f"conv_{target.id}", f"  {target.id}\n"):
        assert _archived_ids(store, spelling) == {target.id}, spelling

    # Exact ids only: a fragment or an id with inner whitespace finds nothing.
    assert _archived_ids(store, target.id[:12]) == set()
    assert _archived_ids(store, f"{target.id[:16]} {target.id[16:]}") == set()
    # Another id type's prefix does not name a session.
    assert _archived_ids(store, f"host_{target.id}") == set()

    # Bob's archived session stays hidden from Alice even by its exact id.
    bob_session = store.create_conversation(title="Bob's planning")
    store.update_conversation(bob_session.id, archived=True)
    permissions.grant(_BOB, bob_session.id, LEVEL_OWNER)
    assert _archived_ids(store, bob_session.id) == set()


def test_search_finds_archived_import_by_its_harness_session_id(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
) -> None:
    """The harness id a session was imported from finds it, scoped to the caller."""
    store, permissions = stores
    harness_session_id = str(uuid.uuid4())
    alice_import = store.create_conversation(title="Imported from Claude")
    store.set_external_session_id(alice_import.id, harness_session_id)
    store.update_conversation(alice_import.id, archived=True)
    permissions.grant(_ALICE, alice_import.id, LEVEL_OWNER)

    # Another user's session wrapping the same harness id stays invisible.
    bob_import = store.create_conversation(title="Bob's copy")
    store.set_external_session_id(bob_import.id, harness_session_id)
    store.update_conversation(bob_import.id, archived=True)
    permissions.grant(_BOB, bob_import.id, LEVEL_OWNER)

    assert _archived_ids(store, harness_session_id) == {alice_import.id}
    assert _archived_ids(store, f" {harness_session_id} ") == {alice_import.id}
    # Non-uuid harness ids (e.g. OpenCode's) match as exact text too.
    opencode_id = "ses_7f3a9c2e1b"
    opencode_import = store.create_conversation(title="Imported from OpenCode")
    store.set_external_session_id(opencode_import.id, opencode_id)
    store.update_conversation(opencode_import.id, archived=True)
    permissions.grant(_ALICE, opencode_import.id, LEVEL_OWNER)
    assert _archived_ids(store, opencode_id) == {opencode_import.id}

    # Restoring it moves it from the archived search to the active one.
    store.update_conversation(alice_import.id, archived=False)
    assert _archived_ids(store, harness_session_id) == set()
    active = store.list_conversations(search_query=harness_session_id, accessible_by=_ALICE)
    assert [conversation.id for conversation in active.data] == [alice_import.id]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("a1b2c3d", (None, None)),  # 7 chars: too short to be an id
        ("a1b2c3d4", (None, "a1b2c3d4")),
        ("x" * 127 + "1", (None, "x" * 127 + "1")),
        ("x" * 128 + "1", (None, None)),  # wider than external_session_id
        ("migration", (None, None)),  # a plain word skips the id lookup
        ("ses 7f3a9c2e1b", (None, None)),
        ("0" * 32, ("0" * 32, "0" * 32)),
        ("conv_" + "0" * 32, ("0" * 32, "conv_" + "0" * 32)),
        ("CONV_" + "AB" * 16, ("ab" * 16, "CONV_" + "AB" * 16)),
        ("host_" + "0" * 32, (None, "host_" + "0" * 32)),
    ],
)
def test_session_id_search_terms(query: str, expected: tuple[str | None, str | None]) -> None:
    """Only one id-shaped token is tried as an Omnigent or harness session id."""
    assert _session_id_search_terms(query) == expected


def test_unscoped_split_db_search_skips_the_harness_id_lookup(tmp_path: Path) -> None:
    """Without a permission scope, split-DB search avoids the unindexed harness-id scan."""
    store = SqlAlchemyConversationStore(
        f"sqlite:///{tmp_path}/omnigent.db", f"sqlite:///{tmp_path}/conversations.db"
    )
    harness_session_id = str(uuid.uuid4())
    imported = store.create_conversation(title="Imported from Claude")
    store.set_external_session_id(imported.id, harness_session_id)

    by_harness_id = store.list_conversations(search_query=harness_session_id)
    assert by_harness_id.data == []
    by_omnigent_id = store.list_conversations(search_query=imported.id)
    assert [conversation.id for conversation in by_omnigent_id.data] == [imported.id]


def test_harness_id_search_composes_with_project_and_owner_scope(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
) -> None:
    """A harness-id match still honours the project filter and the "mine" scope."""
    store, permissions = stores
    harness_session_id = str(uuid.uuid4())
    imported = store.create_conversation(title="Imported from Claude")
    store.set_external_session_id(imported.id, harness_session_id)
    store.set_labels(imported.id, {"omni_project": "Work"})
    permissions.grant(_ALICE, imported.id, LEVEL_OWNER)

    def _ids(**filters: object) -> set[str]:
        page = store.list_conversations(search_query=harness_session_id, **filters)
        return {conversation.id for conversation in page.data}

    assert _ids(owned_by=_ALICE) == {imported.id}
    assert _ids(owned_by=_ALICE, project="Work") == {imported.id}
    assert _ids(owned_by=_ALICE, project="Personal") == set()
    assert _ids(owned_by=_BOB) == set()


def test_harness_id_match_sql_stays_scoped(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
) -> None:
    """The harness-id predicate is limited to the outer row or the caller's sessions.

    Single-DB correlates an EXISTS on the outer row; split-DB probes only the
    caller's qualifying ids, with a row cap. Asserted on the emitted SQL because
    an unscoped probe returns the same rows.
    """
    store, permissions = stores
    harness_session_id = str(uuid.uuid4())
    imported = store.create_conversation(title="Imported from Claude")
    store.set_external_session_id(imported.id, harness_session_id)
    permissions.grant(_ALICE, imported.id, LEVEL_OWNER)

    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        statements.append(" ".join(statement.split()).lower())

    engines = {store._engine, store._conv_engine}
    for engine in engines:
        event.listen(engine, "before_cursor_execute", _capture)
    try:
        page = store.list_conversations(search_query=harness_session_id, accessible_by=_ALICE)
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", _capture)
    assert [conversation.id for conversation in page.data] == [imported.id]

    if store._conv_engine is store._engine:
        [list_sql] = [sql for sql in statements if sql.startswith("select conversations.")]
        assert "omnigent_conversation_metadata.id = conversations.id" in list_sql, list_sql
        assert "omnigent_conversation_metadata.external_session_id = ?" in list_sql, list_sql
    else:
        [probe_sql] = [
            sql
            for sql in statements
            if sql.startswith("select omnigent_conversation_metadata.id from")
            and "external_session_id = ?" in sql
        ]
        assert "omnigent_conversation_metadata.id in (" in probe_sql, probe_sql
        assert " limit ?" in probe_sql, probe_sql
