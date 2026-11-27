"""PermissionStoreGadgetAccess: owners, grant levels, groups and deleted owners."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.gadgets.access import (
    FULL_ACCESS,
    NO_ACCESS,
    GadgetPermissions,
    PermissionStoreGadgetAccess,
    SharedPage,
    workspace_registrations,
)
from omnigent.gadgets.accounts import DeletedAccountGadgets
from omnigent.gadgets.service import GadgetService, GadgetViewer
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ
from omnigent.stores.gadget_store import NotFound
from omnigent.stores.gadget_store.local import LocalGadgetStore
from tests.gadgets.grant_store import GadgetGrantStore

ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"


class _GroupStore(GadgetGrantStore):
    """Carol belongs to ``team``; a deployment's directory would answer this."""

    deleted: frozenset[str] = frozenset()

    def caller_principals(self, user_id: str) -> list[str]:
        return [user_id, "team"] if user_id == CAROL else [user_id]

    def deleted_accounts(self, user_ids: Sequence[str]) -> set[str]:
        return {user_id for user_id in user_ids if user_id in self.deleted}


@pytest.fixture()
def setup(db_uri: str, tmp_path: Path) -> tuple[LocalGadgetStore, _GroupStore]:
    grants = _GroupStore(db_uri)
    for user in (BOB, CAROL, "team"):
        grants.ensure_user(user)
    return LocalGadgetStore(str(tmp_path)), grants


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (LEVEL_READ, GadgetPermissions(read=True)),
        (LEVEL_EDIT, GadgetPermissions(read=True, write_data=True, publish=True)),
        (LEVEL_MANAGE, FULL_ACCESS),
    ],
)
def test_grant_levels_map_to_gadget_permissions(
    setup: tuple[LocalGadgetStore, _GroupStore], level: int, expected: GadgetPermissions
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    gadget = store.create(ALICE, "Board")
    assert access.permissions(ALICE, gadget) == FULL_ACCESS
    assert access.permissions(BOB, gadget) == NO_ACCESS
    grants.grant_for_gadget(BOB, gadget.id, level)
    assert access.permissions(BOB, gadget) == expected


def test_shared_gadgets_come_through_a_user_or_a_group_grant(
    setup: tuple[LocalGadgetStore, _GroupStore],
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    direct, grouped, gone = (store.create(ALICE, name) for name in ("Direct", "Grouped", "Gone"))
    own = store.create(CAROL, "Own")
    grants.grant_for_gadget(CAROL, direct.id, LEVEL_READ)
    grants.grant_for_gadget("team", grouped.id, LEVEL_EDIT)
    grants.grant_for_gadget(CAROL, gone.id, LEVEL_READ)
    grants.grant_for_gadget(CAROL, own.id, LEVEL_READ)
    store.delete(gone.id)

    shared = {item.gadget.id: item.shared_via for item in access.shared_with(CAROL).items}
    assert shared == {direct.id: "user", grouped.id: "group"}
    assert access.permissions(CAROL, grouped).publish is True
    assert access.shared_with(BOB) == SharedPage([], None)

    # A deleted owner's gadgets stop being shared at once.
    grants.deleted = frozenset({ALICE})
    assert access.shared_with(CAROL).items == []
    assert access.permissions(CAROL, direct) == NO_ACCESS

    access.forget(direct.id)
    assert grants.list_for_gadget(direct.id) == ([], None)


def test_without_a_permission_store_gadgets_stay_private(tmp_path: Path) -> None:
    store = LocalGadgetStore(str(tmp_path))
    access = PermissionStoreGadgetAccess(store, None)
    gadget = store.create(ALICE, "Board")
    assert access.permissions(ALICE, gadget) == FULL_ACCESS
    assert access.permissions(BOB, gadget) == NO_ACCESS
    assert access.shared_with(BOB) == SharedPage([], None)
    access.forget(gadget.id)


def _all_shared(access: PermissionStoreGadgetAccess, viewer: str, limit: int) -> list[list[str]]:
    """Every page of what is shared with *viewer*, as gadget IDs."""
    pages, cursor = [], None
    while True:
        page = access.shared_with(viewer, limit=limit, cursor=cursor)
        pages.append([item.gadget.id for item in page.items])
        cursor = page.next_cursor
        if cursor is None:
            return pages


def test_the_shared_list_pages_through_every_grant(
    setup: tuple[LocalGadgetStore, _GroupStore],
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    gadgets = [store.create(ALICE, f"Board {n}") for n in range(5)]
    for gadget in gadgets:
        grants.grant_for_gadget(CAROL, gadget.id, LEVEL_READ)
    pages = _all_shared(access, CAROL, limit=2)
    assert [len(page) for page in pages] == [2, 2, 1]
    assert sorted([item for page in pages for item in page]) == sorted(
        gadget.id for gadget in gadgets
    )


def test_grants_for_gadgets_this_store_lacks_are_skipped_and_kept(
    setup: tuple[LocalGadgetStore, _GroupStore], tmp_path: Path
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    gone = [store.create(ALICE, f"Gone {n}") for n in range(2)]
    live = [store.create(ALICE, f"Live {n}") for n in range(2)]
    # Another server's gadgets, granted in the same permission store.
    other = LocalGadgetStore(str(tmp_path / "other"))
    elsewhere = [other.create(ALICE, f"Elsewhere {n}") for n in range(2)]
    for gadget in [*gone, *live, *elsewhere]:
        grants.grant_for_gadget(CAROL, gadget.id, LEVEL_READ)
    for gadget in gone:
        # Deleted as the routes do: the grants go first.
        access.forget(gadget.id)
        store.delete(gadget.id)

    # Short or empty pages still continue, so every live gadget is reached.
    shared = {item for page in _all_shared(access, CAROL, limit=2) for item in page}
    assert shared == {gadget.id for gadget in live}
    for gadget in gone:
        assert grants.list_for_gadget(gadget.id) == ([], None)
    # A read that misses here doesn't delete the other server's grants.
    other_access = PermissionStoreGadgetAccess(other, grants)
    shared_there = {item for page in _all_shared(other_access, CAROL, limit=2) for item in page}
    assert shared_there == {gadget.id for gadget in elsewhere}


def test_permissions_for_several_gadgets_match_one_at_a_time(
    setup: tuple[LocalGadgetStore, _GroupStore],
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    own, shared, private = (store.create(owner, "x") for owner in (CAROL, ALICE, ALICE))
    grants.grant_for_gadget(CAROL, shared.id, LEVEL_EDIT)
    gadgets = [own, shared, private]
    expected = {gadget.id: access.permissions(CAROL, gadget) for gadget in gadgets}
    assert access.permissions_many(CAROL, gadgets) == expected
    assert expected[own.id] == FULL_ACCESS and expected[private.id] == NO_ACCESS
    grants.deleted = frozenset({ALICE})
    assert access.permissions_many(CAROL, gadgets)[shared.id] == NO_ACCESS


class _DecimalIdStore(LocalGadgetStore):
    """A store naming gadgets with decimal IDs, as a deployment's own store may."""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self._next = 102717306144392

    def _new_id(self) -> str:
        self._next += 1
        return str(self._next)


def test_grants_and_account_cleanup_work_for_a_store_without_uuid_ids(
    db_uri: str, tmp_path: Path
) -> None:
    store = _DecimalIdStore(str(tmp_path))
    grants = _GroupStore(db_uri)
    for user in (BOB, CAROL):
        grants.ensure_user(user)
    access = PermissionStoreGadgetAccess(store, grants)
    board, report = store.create(ALICE, "Board"), store.create(ALICE, "Report")
    assert board.id.isdigit()
    grants.grant_for_gadget(BOB, board.id, LEVEL_EDIT)
    grants.grant_for_gadget(CAROL, report.id, LEVEL_READ)
    assert access.permissions(BOB, board).write_data is True
    assert [item.gadget.id for item in access.shared_with(BOB).items] == [board.id]

    # Deleting Alice's account removes her gadgets and every grant on them.
    DeletedAccountGadgets(store, access).delete_owned(ALICE, None)
    assert store.get(board.id) is None and store.get(report.id) is None
    assert grants.list_for_gadget(board.id) == ([], None)
    assert grants.resolve_gadget_access([CAROL], report.id) is None
    assert access.shared_with(BOB) == SharedPage([], None)


@pytest.mark.parametrize("registered", [False, True])
def test_a_gadget_belongs_to_its_owner_only_in_the_workspace_it_was_made_in(
    setup: tuple[LocalGadgetStore, _GroupStore], registered: bool
) -> None:
    # One user ID in two workspaces of one server, sharing one gadget store: with no account
    # row (an externally provisioned user) or the same registration in both.
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)

    def service() -> GadgetService:
        registration = workspace_registrations(grants, [ALICE])[ALICE]
        return GadgetService(store, access, GadgetViewer(ALICE, registration), None)

    if registered:
        # A registered account in each workspace, made at the same time: the same
        # registration in both, so only the workspace tells the gadgets apart.
        from sqlalchemy import update

        from omnigent.db.db_models import SqlUser

        for workspace in (1, 2):
            with workspace_scope(workspace):
                grants.ensure_user(ALICE)
                with grants._session("test_register") as session:
                    session.execute(
                        update(SqlUser)
                        .where(SqlUser.workspace_id == workspace, SqlUser.id == ALICE)
                        .values(created_at=1_700_000_000)
                    )
        with workspace_scope(1):
            assert workspace_registrations(grants, [ALICE])[ALICE] is not None
    with workspace_scope(1):
        made = service().create("Board")
        assert access.permissions(ALICE, store.get(made["id"])) == FULL_ACCESS
    with workspace_scope(2):
        gadget = store.get(made["id"])
        assert gadget is not None
        assert access.permissions(ALICE, gadget) == NO_ACCESS
        other = service()
        assert other.list_owned()["items"] == []
        # Hidden, as any gadget the viewer can't see.
        with pytest.raises(NotFound):
            other.get(made["id"])
        with pytest.raises(NotFound):
            other.delete(made["id"])
    with workspace_scope(1):
        assert service().get(made["id"])["id"] == made["id"]
