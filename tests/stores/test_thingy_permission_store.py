"""Thingy grants in the SQL permission store and the defaults for stores without them."""

from __future__ import annotations

import uuid

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.entities import ThingyPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ
from omnigent.stores.permission_store import (
    MAX_GRANTS_PAGE_SIZE,
    MAX_SHARED_THINGIES_PAGE_SIZE,
    PermissionStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.stores.test_permission_store import _minimal_store


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPermissionStore:
    return SqlAlchemyPermissionStore(db_uri)


def _thingy() -> str:
    return uuid.uuid4().hex


def test_thingy_grants_upsert_page_resolve_and_revoke(store: SqlAlchemyPermissionStore) -> None:
    thingy, other = _thingy(), _thingy()
    user_ids = [f"user{i:02d}@example.com" for i in range(5)]
    for user_id in [*user_ids, "editors"]:
        store.ensure_user(user_id)
    assert store.supports_thingy_grants is True
    for user_id in reversed(user_ids):
        assert store.grant_for_thingy(user_id, thingy, LEVEL_READ) == ThingyPermission(
            user_id, thingy, LEVEL_READ
        )
    store.grant_for_thingy("user00@example.com", other, LEVEL_MANAGE)
    store.grant_for_thingy("editors", thingy, LEVEL_EDIT)

    pages: list[list[str]] = []
    cursor = None
    while True:
        page, cursor = store.list_for_thingy(thingy, limit=2, after_user_id=cursor)
        pages.append([grant.user_id for grant in page])
        if cursor is None:
            break
    assert pages == [["editors", user_ids[0]], user_ids[1:3], user_ids[3:]]
    for limit in (0, MAX_GRANTS_PAGE_SIZE + 1):
        with pytest.raises(OmnigentError) as caught:
            store.list_for_thingy(thingy, limit=limit)
        assert caught.value.code == ErrorCode.INVALID_INPUT

    assert store.resolve_thingy_access(["user01@example.com", "editors"], thingy) == LEVEL_EDIT
    assert store.resolve_thingy_access(["bob@example.com"], thingy) is None
    assert store.resolve_thingy_access([], thingy) is None

    # ensure_minimum keeps a higher grant; a plain grant replaces it.
    kept = store.grant_for_thingy("editors", thingy, LEVEL_READ, ensure_minimum=True)
    assert kept.level == LEVEL_EDIT
    assert store.grant_for_thingy("editors", thingy, LEVEL_READ).level == LEVEL_READ
    assert store.revoke_for_thingy("user00@example.com", thingy) is True
    assert store.revoke_for_thingy("user00@example.com", thingy) is False
    assert store.resolve_thingy_access(["user00@example.com"], other) == LEVEL_MANAGE


def test_thingies_shared_with_a_caller_page_once_each(store: SqlAlchemyPermissionStore) -> None:
    thingies = sorted(_thingy() for _ in range(5))
    for user_id in ("alice@example.com", "team", "bob@example.com"):
        store.ensure_user(user_id)
    for thingy in thingies[:3]:
        store.grant_for_thingy("alice@example.com", thingy, LEVEL_READ)
    for thingy in thingies[2:]:
        store.grant_for_thingy("team", thingy, LEVEL_EDIT)
    store.grant_for_thingy("bob@example.com", _thingy(), LEVEL_READ)

    pages: list[list[str]] = []
    cursor = None
    while True:
        page, cursor = store.list_thingies_shared_with(
            ["team", "alice@example.com"], limit=2, after_thingy_id=cursor
        )
        pages.append(page)
        if cursor is None:
            break
    assert pages == [thingies[:2], thingies[2:4], thingies[4:]]
    assert store.list_thingies_shared_with(["carol@example.com"]) == ([], None)
    assert store.list_thingies_shared_with([]) == ([], None)
    for limit in (0, MAX_SHARED_THINGIES_PAGE_SIZE + 1):
        with pytest.raises(OmnigentError) as caught:
            store.list_thingies_shared_with(["team"], limit=limit)
        assert caught.value.code == ErrorCode.INVALID_INPUT

    assert store.delete_for_thingy(thingies[2]) == 2
    assert store.delete_for_thingy(thingies[2]) == 0
    assert store.list_thingies_shared_with(["alice@example.com"]) == (thingies[:2], None)


@pytest.mark.parametrize(
    ("user_id", "level"),
    [("__public__", LEVEL_READ), ("local", LEVEL_READ), ("alice@example.com", LEVEL_OWNER)],
)
def test_thingy_grants_refuse_what_the_store_must_not_hold(
    store: SqlAlchemyPermissionStore, user_id: str, level: int
) -> None:
    thingy = _thingy()
    store.ensure_user(user_id)
    with pytest.raises(OmnigentError) as caught:
        store.grant_for_thingy(user_id, thingy, level)
    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert store.list_for_thingy(thingy) == ([], None)


def test_thingy_grants_are_scoped_to_the_workspace(store: SqlAlchemyPermissionStore) -> None:
    thingy = _thingy()
    with workspace_scope(101):
        store.ensure_user("alice@example.com")
        store.grant_for_thingy("alice@example.com", thingy, LEVEL_READ)
    with workspace_scope(102):
        assert store.resolve_thingy_access(["alice@example.com"], thingy) is None
        assert store.list_thingies_shared_with(["alice@example.com"]) == ([], None)
        assert store.delete_for_thingy(thingy) == 0
    with workspace_scope(101):
        assert store.list_thingies_shared_with(["alice@example.com"]) == ([thingy], None)


def test_store_without_thingy_grants_keeps_thingies_private() -> None:
    methods = {
        "grant_for_thingy",
        "revoke_for_thingy",
        "list_for_thingy",
        "resolve_thingy_access",
        "list_thingies_shared_with",
        "delete_for_thingy",
    }
    assert not methods & PermissionStore.__abstractmethods__
    minimal = _minimal_store()
    assert minimal.supports_thingy_grants is False
    with pytest.raises(OmnigentError) as caught:
        minimal.grant_for_thingy("alice@example.com", _thingy(), LEVEL_READ)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert minimal.resolve_thingy_access(["alice@example.com"], _thingy()) is None
    assert minimal.list_thingies_shared_with(["alice@example.com"]) == ([], None)
    assert minimal.delete_for_thingy(_thingy()) == 0
