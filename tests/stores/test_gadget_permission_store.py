"""Gadget grants in the SQL permission store and the defaults for stores without them."""

from __future__ import annotations

import uuid

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.entities import GadgetPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ
from omnigent.stores.permission_store import (
    MAX_GRANTS_PAGE_SIZE,
    MAX_SHARED_GADGETS_PAGE_SIZE,
    PermissionStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.stores.test_permission_store import _minimal_store


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPermissionStore:
    return SqlAlchemyPermissionStore(db_uri)


def _gadget() -> str:
    return uuid.uuid4().hex


def test_gadget_grants_upsert_page_resolve_and_revoke(store: SqlAlchemyPermissionStore) -> None:
    gadget, other = _gadget(), _gadget()
    user_ids = [f"user{i:02d}@example.com" for i in range(5)]
    for user_id in [*user_ids, "editors"]:
        store.ensure_user(user_id)
    assert store.supports_gadget_grants is True
    for user_id in reversed(user_ids):
        assert store.grant_for_gadget(user_id, gadget, LEVEL_READ) == GadgetPermission(
            user_id, gadget, LEVEL_READ
        )
    store.grant_for_gadget("user00@example.com", other, LEVEL_MANAGE)
    store.grant_for_gadget("editors", gadget, LEVEL_EDIT)

    pages: list[list[str]] = []
    cursor = None
    while True:
        page, cursor = store.list_for_gadget(gadget, limit=2, after_user_id=cursor)
        pages.append([grant.user_id for grant in page])
        if cursor is None:
            break
    assert pages == [["editors", user_ids[0]], user_ids[1:3], user_ids[3:]]
    for limit in (0, MAX_GRANTS_PAGE_SIZE + 1):
        with pytest.raises(OmnigentError) as caught:
            store.list_for_gadget(gadget, limit=limit)
        assert caught.value.code == ErrorCode.INVALID_INPUT

    assert store.resolve_gadget_access(["user01@example.com", "editors"], gadget) == LEVEL_EDIT
    assert store.resolve_gadget_access(["bob@example.com"], gadget) is None
    assert store.resolve_gadget_access([], gadget) is None

    # ensure_minimum keeps a higher grant; a plain grant replaces it.
    kept = store.grant_for_gadget("editors", gadget, LEVEL_READ, ensure_minimum=True)
    assert kept.level == LEVEL_EDIT
    assert store.grant_for_gadget("editors", gadget, LEVEL_READ).level == LEVEL_READ
    assert store.revoke_for_gadget("user00@example.com", gadget) is True
    assert store.revoke_for_gadget("user00@example.com", gadget) is False
    assert store.resolve_gadget_access(["user00@example.com"], other) == LEVEL_MANAGE


def test_gadgets_shared_with_a_caller_page_once_each(store: SqlAlchemyPermissionStore) -> None:
    gadgets = sorted(_gadget() for _ in range(5))
    for user_id in ("alice@example.com", "team", "bob@example.com"):
        store.ensure_user(user_id)
    for gadget in gadgets[:3]:
        store.grant_for_gadget("alice@example.com", gadget, LEVEL_READ)
    for gadget in gadgets[2:]:
        store.grant_for_gadget("team", gadget, LEVEL_EDIT)
    store.grant_for_gadget("bob@example.com", _gadget(), LEVEL_READ)

    pages: list[list[str]] = []
    cursor = None
    while True:
        page, cursor = store.list_gadgets_shared_with(
            ["team", "alice@example.com"], limit=2, after_gadget_id=cursor
        )
        pages.append(page)
        if cursor is None:
            break
    assert pages == [gadgets[:2], gadgets[2:4], gadgets[4:]]
    assert store.list_gadgets_shared_with(["carol@example.com"]) == ([], None)
    assert store.list_gadgets_shared_with([]) == ([], None)
    for limit in (0, MAX_SHARED_GADGETS_PAGE_SIZE + 1):
        with pytest.raises(OmnigentError) as caught:
            store.list_gadgets_shared_with(["team"], limit=limit)
        assert caught.value.code == ErrorCode.INVALID_INPUT

    assert store.delete_for_gadget(gadgets[2]) == 2
    assert store.delete_for_gadget(gadgets[2]) == 0
    assert store.list_gadgets_shared_with(["alice@example.com"]) == (gadgets[:2], None)


@pytest.mark.parametrize(
    ("user_id", "level"),
    [("__public__", LEVEL_READ), ("local", LEVEL_READ), ("alice@example.com", LEVEL_OWNER)],
)
def test_gadget_grants_refuse_what_the_store_must_not_hold(
    store: SqlAlchemyPermissionStore, user_id: str, level: int
) -> None:
    gadget = _gadget()
    store.ensure_user(user_id)
    with pytest.raises(OmnigentError) as caught:
        store.grant_for_gadget(user_id, gadget, level)
    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert store.list_for_gadget(gadget) == ([], None)


def test_gadget_grants_are_scoped_to_the_workspace(store: SqlAlchemyPermissionStore) -> None:
    gadget = _gadget()
    with workspace_scope(101):
        store.ensure_user("alice@example.com")
        store.grant_for_gadget("alice@example.com", gadget, LEVEL_READ)
    with workspace_scope(102):
        assert store.resolve_gadget_access(["alice@example.com"], gadget) is None
        assert store.list_gadgets_shared_with(["alice@example.com"]) == ([], None)
        assert store.delete_for_gadget(gadget) == 0
    with workspace_scope(101):
        assert store.list_gadgets_shared_with(["alice@example.com"]) == ([gadget], None)


def test_store_without_gadget_grants_keeps_gadgets_private() -> None:
    methods = {
        "grant_for_gadget",
        "revoke_for_gadget",
        "list_for_gadget",
        "resolve_gadget_access",
        "list_gadgets_shared_with",
        "delete_for_gadget",
    }
    assert not methods & PermissionStore.__abstractmethods__
    minimal = _minimal_store()
    assert minimal.supports_gadget_grants is False
    with pytest.raises(OmnigentError) as caught:
        minimal.grant_for_gadget("alice@example.com", _gadget(), LEVEL_READ)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert minimal.resolve_gadget_access(["alice@example.com"], _gadget()) is None
    assert minimal.list_gadgets_shared_with(["alice@example.com"]) == ([], None)
    assert minimal.delete_for_gadget(_gadget()) == 0


def test_grantee_ids_that_differ_only_in_case_are_separate_principals(
    store: SqlAlchemyPermissionStore,
) -> None:
    # Authorization matches IDs exactly, so grants do too; a store that treats such IDs
    # as one person canonicalizes them in validate_grants.
    if store._engine.dialect.name == "mysql":
        pytest.skip("MySQL's default collation compares these IDs as equal, as for every grant")
    gadget = _gadget()
    for user_id, level in (("UserA", LEVEL_READ), ("usera", LEVEL_MANAGE)):
        store.ensure_user(user_id)
        store.grant_for_gadget(user_id, gadget, level)
    assert store.resolve_gadget_access(["UserA"], gadget) == LEVEL_READ
    assert store.resolve_gadget_access(["usera"], gadget) == LEVEL_MANAGE
    store.revoke_for_gadget("usera", gadget)
    assert store.resolve_gadget_access(["UserA"], gadget) == LEVEL_READ
    assert store.resolve_gadget_access(["usera"], gadget) is None
