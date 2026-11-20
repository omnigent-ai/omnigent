"""Shared setup for the thingy service tests: a local store and explicit grants."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.stores.thingy_store import Thingy, ThingyFiles
from omnigent.stores.thingy_store.local import LocalThingyStore
from omnigent.thingies.access import (
    FULL_ACCESS,
    NO_ACCESS,
    SharedThingy,
    ThingyAccess,
    ThingyPermissions,
)
from omnigent.thingies.progress import SessionStateReader
from omnigent.thingies.service import ThingyService, ThingyViewer

READ = ThingyPermissions(read=True)
DATA_EDIT = ThingyPermissions(read=True, write_data=True)
EDIT = ThingyPermissions(read=True, write_data=True, publish=True)


def person(number: int) -> str:
    return f"person{number}@example.com"


class GrantedAccess(ThingyAccess):
    """Owners get everything; anyone else exactly the permissions a test grants them."""

    def __init__(self, store: LocalThingyStore) -> None:
        self.store = store
        self.grants: dict[tuple[str, str], ThingyPermissions] = {}

    def share(self, thingy: dict, user: int, permissions: ThingyPermissions) -> None:
        self.grants[person(user), thingy["id"]] = permissions

    def permissions(self, viewer_id: str, thingy: Thingy) -> ThingyPermissions:
        if viewer_id == thingy.owner_id:
            return FULL_ACCESS
        return self.grants.get((viewer_id, thingy.id), NO_ACCESS)

    def shared_with(self, viewer_id: str) -> list[SharedThingy]:
        found = self.store.get_many(app for user, app in self.grants if user == viewer_id)
        return [SharedThingy(thingy, "user") for thingy in found.values()]

    def forget(self, thingy_id: str) -> None:
        self.grants = {key: value for key, value in self.grants.items() if key[1] != thingy_id}


class HookedFiles(ThingyFiles):
    """A thingy's real files, with test hooks before a create and after a listing."""

    def __init__(self, inner: ThingyFiles, store: HookedStore) -> None:
        self.inner = inner
        self.store = store

    def read_json(self, path: str) -> dict:
        return self.inner.read_json(path)

    def create_json(self, path: str, value: dict) -> None:
        if self.store.before_create is not None:
            self.store.before_create(path)
        self.inner.create_json(path, value)

    def list_paths(self, path: str) -> list[str]:
        entries = self.inner.list_paths(path)
        if self.store.after_list is not None:
            self.store.after_list(path, entries)
        return entries

    def mkdirs(self, path: str) -> None:
        self.inner.mkdirs(path)


class HookedStore(LocalThingyStore):
    before_create: Callable[[str], None] | None = None
    after_list: Callable[[str, list[str]], None] | None = None

    def thingy_files(self, thingy_id: str) -> ThingyFiles:
        return HookedFiles(super().thingy_files(thingy_id), self)


class Fixture:
    """A store, its grants and a service per person."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = HookedStore(str(root))
        self.access = GrantedAccess(self.store)

    def service(self, user: int = 1, sessions: SessionStateReader | None = None) -> ThingyService:
        return ThingyService(self.store, self.access, ThingyViewer(person(user)), sessions)

    def snapshot(self) -> dict[str, bytes]:
        """Every stored file, to show a refused request wrote nothing."""
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def revision_path(self, app: dict, revision_id: str) -> Path:
        return (
            self.root
            / "thingies"
            / app["id"]
            / "files"
            / "code"
            / "revisions"
            / f"{revision_id}.json"
        )


@pytest.fixture
def thingies(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path / "thingies")
