"""Shared setup for the gadget service tests: a local store and explicit grants."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.gadgets.access import (
    FULL_ACCESS,
    NO_ACCESS,
    GadgetAccess,
    GadgetPermissions,
    SharedGadget,
)
from omnigent.gadgets.progress import SessionStateReader
from omnigent.gadgets.service import GadgetService, GadgetViewer
from omnigent.stores.gadget_store import Gadget, GadgetFiles
from omnigent.stores.gadget_store.local import LocalGadgetStore

READ = GadgetPermissions(read=True)
DATA_EDIT = GadgetPermissions(read=True, write_data=True)
EDIT = GadgetPermissions(read=True, write_data=True, publish=True)


def person(number: int) -> str:
    return f"person{number}@example.com"


class GrantedAccess(GadgetAccess):
    """Owners get everything; anyone else exactly the permissions a test grants them."""

    def __init__(self, store: LocalGadgetStore) -> None:
        self.store = store
        self.grants: dict[tuple[str, str], GadgetPermissions] = {}

    def share(self, gadget: dict, user: int, permissions: GadgetPermissions) -> None:
        self.grants[person(user), gadget["id"]] = permissions

    def permissions(self, viewer_id: str, gadget: Gadget) -> GadgetPermissions:
        if viewer_id == gadget.owner_id:
            return FULL_ACCESS
        return self.grants.get((viewer_id, gadget.id), NO_ACCESS)

    def shared_with(self, viewer_id: str) -> list[SharedGadget]:
        found = self.store.get_many(app for user, app in self.grants if user == viewer_id)
        return [SharedGadget(gadget, "user") for gadget in found.values()]

    def forget(self, gadget_id: str) -> None:
        self.grants = {key: value for key, value in self.grants.items() if key[1] != gadget_id}


class HookedFiles(GadgetFiles):
    """A gadget's real files, with test hooks before a create and after a listing."""

    def __init__(self, inner: GadgetFiles, store: HookedStore) -> None:
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

    @property
    def supports_delete(self) -> bool:  # type: ignore[override]
        return self.inner.supports_delete

    def delete_tree(self, path: str) -> None:
        self.inner.delete_tree(path)


class HookedStore(LocalGadgetStore):
    before_create: Callable[[str], None] | None = None
    after_list: Callable[[str, list[str]], None] | None = None

    def gadget_files(self, gadget_id: str) -> GadgetFiles:
        return HookedFiles(super().gadget_files(gadget_id), self)


class Fixture:
    """A store, its grants and a service per person."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = HookedStore(str(root))
        self.access = GrantedAccess(self.store)

    def service(self, user: int = 1, sessions: SessionStateReader | None = None) -> GadgetService:
        return GadgetService(self.store, self.access, GadgetViewer(person(user)), sessions)

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
            / "gadgets"
            / app["id"]
            / "files"
            / "code"
            / "revisions"
            / f"{revision_id}.json"
        )


@pytest.fixture
def gadgets(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path / "gadgets")
