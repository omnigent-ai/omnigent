"""LocalThingyStore: thingy metadata, owner index, create-only files and path safety."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from omnigent.stores.thingy_store import (
    MAX_FILE_BYTES,
    CapacityExceeded,
    Conflict,
    CorruptData,
    InvalidInput,
    NotFound,
    is_thingy_id,
)
from omnigent.stores.thingy_store.local import LocalThingyStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def store(tmp_path: Path) -> LocalThingyStore:
    return LocalThingyStore(str(tmp_path / "thingies"))


def test_thingies_are_created_listed_renamed_and_deleted_per_owner(
    store: LocalThingyStore,
) -> None:
    first = store.create(ALICE, "Board")
    second = store.create(ALICE, "Report")
    other = store.create(BOB, "Tracker")
    assert len(first.id) == 32 and first.owner_id == ALICE

    assert [thingy.id for thingy in store.list_owned(ALICE)] == [first.id, second.id]
    assert [thingy.id for thingy in store.list_owned(BOB)] == [other.id]
    assert store.list_owned("carol@example.com") == []
    assert store.get_many([first.id, "f" * 32, other.id]) == {first.id: first, other.id: other}

    renamed = store.rename(first.id, "Launch board")
    assert renamed is not None and renamed.name == "Launch board"
    assert store.get(first.id) == renamed
    assert store.rename("f" * 32, "Nothing") is None

    store.thingy_files(first.id).mkdirs("data/cards")
    store.thingy_files(first.id).create_json("data/cards/one.json", {"title": "Ship"})
    assert store.delete(first.id) is True
    assert store.get(first.id) is None
    assert store.delete(first.id) is False
    assert [thingy.id for thingy in store.list_owned(ALICE)] == [second.id]
    # The deleted thingy's files are gone too, and a new one starts empty.
    with pytest.raises(NotFound):
        store.thingy_files(first.id).read_json("data/cards/one.json")


@pytest.mark.parametrize("thingy_id", ["", "../x", "A" * 32, "a" * 31, 7])
def test_malformed_ids_name_no_thingy(store: LocalThingyStore, thingy_id: object) -> None:
    assert store.get(thingy_id) is None  # type: ignore[arg-type]


def test_a_store_reopened_on_the_same_directory_sees_the_same_thingies(tmp_path: Path) -> None:
    created = LocalThingyStore(str(tmp_path)).create(ALICE, "Board")
    LocalThingyStore(str(tmp_path)).thingy_files(created.id).mkdirs("code")
    assert LocalThingyStore(str(tmp_path)).get(created.id) == created


def test_files_are_create_only_and_read_back(store: LocalThingyStore) -> None:
    files = store.thingy_files(store.create(ALICE, "Board").id)
    with pytest.raises(NotFound):
        files.create_json("data/x.json", {"n": 1})
    files.mkdirs("data")
    files.mkdirs("data")
    files.create_json("data/x.json", {"n": 1})
    with pytest.raises(Conflict):
        files.create_json("data/x.json", {"n": 2})
    assert files.read_json("data/x.json") == {"n": 1}
    files.mkdirs("data/sub")
    assert files.list_paths("data") == ["sub", "x.json"]
    with pytest.raises(NotFound):
        files.list_paths("missing")
    with pytest.raises(NotFound):
        files.read_json("data/missing.json")


def test_mkdirs_reports_a_directory_deleted_mid_call_as_not_found(
    store: LocalThingyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = store.thingy_files(store.create(ALICE, "Board").id)
    files.mkdirs("data")
    real_mkdir = Path.mkdir

    # The race: os.mkdir finds the directory, then a deletion moves it away before mkdir's
    # own is_dir check, so mkdir(exist_ok=True) raises FileExistsError.
    def mkdir_racing_a_deletion(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "data":
            self.rename(self.with_name("data.deleted"))
            raise FileExistsError(str(self))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", mkdir_racing_a_deletion)
    with pytest.raises(NotFound):
        files.mkdirs("data/sub")


def test_concurrent_creates_of_one_file_have_exactly_one_winner(store: LocalThingyStore) -> None:
    files = store.thingy_files(store.create(ALICE, "Board").id)
    files.mkdirs("slots")
    outcomes: list[str] = []
    start = threading.Barrier(8)

    def write(index: int) -> None:
        start.wait()
        try:
            files.create_json("slots/00000001.json", {"writer": index})
            outcomes.append("won")
        except Conflict:
            outcomes.append("lost")

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["lost"] * 7 + ["won"]
    # Staging files never show up in a listing.
    assert files.list_paths("slots") == ["00000001.json"]


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../escape.json", "data/../x.json", "data//x.json", "a\\b", "./x", ""]
)
def test_unsafe_paths_are_refused(store: LocalThingyStore, path: str) -> None:
    files = store.thingy_files(store.create(ALICE, "Board").id)
    with pytest.raises(InvalidInput):
        files.read_json(path)
    with pytest.raises(InvalidInput):
        files.mkdirs(path)


def test_a_symlink_cannot_lead_a_scope_outside_itself(
    store: LocalThingyStore, tmp_path: Path
) -> None:
    thingy = store.create(ALICE, "Board")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text('{"secret": true}')
    root = tmp_path / "thingies" / "thingies" / thingy.id / "files"
    os.symlink(outside, root / "link")
    with pytest.raises(InvalidInput):
        store.thingy_files(thingy.id).read_json("link/secret.json")


def test_corrupt_and_oversized_files_fail_closed(store: LocalThingyStore, tmp_path: Path) -> None:
    thingy = store.create(ALICE, "Board")
    files = store.thingy_files(thingy.id)
    files.mkdirs("data")
    root = tmp_path / "thingies" / "thingies" / thingy.id / "files" / "data"
    (root / "bad.json").write_text("{not json")
    (root / "list.json").write_text("[1, 2]")
    (root / "nan.json").write_text('{"n": NaN}')
    for name in ("bad.json", "list.json", "nan.json"):
        with pytest.raises(CorruptData):
            files.read_json(f"data/{name}")
    with pytest.raises(CapacityExceeded):
        files.create_json("data/big.json", {"text": "x" * MAX_FILE_BYTES})
    with pytest.raises(InvalidInput):
        files.create_json("data/inf.json", {"n": float("inf")})


def test_each_user_has_their_own_files(store: LocalThingyStore) -> None:
    for user in (ALICE, BOB, "../odd/user"):
        store.user_files(user).mkdirs("shelf")
        store.user_files(user).create_json("shelf/pins.json", {"owner": user})
    assert store.user_files(ALICE).read_json("shelf/pins.json") == {"owner": ALICE}
    assert store.user_files("../odd/user").read_json("shelf/pins.json") == {"owner": "../odd/user"}


def test_metadata_with_the_wrong_shape_is_corrupt(store: LocalThingyStore, tmp_path: Path) -> None:
    thingy = store.create(ALICE, "Board")
    metadata = tmp_path / "thingies" / "thingies" / thingy.id / "thingy.json"
    metadata.write_text(json.dumps({"kind": "other", "id": thingy.id}))
    with pytest.raises(CorruptData):
        store.get(thingy.id)


def test_writes_after_a_deletion_fail_and_never_bring_its_files_back(
    store: LocalThingyStore, tmp_path: Path
) -> None:
    thingy = store.create(ALICE, "Board")
    files = store.thingy_files(thingy.id)
    files.mkdirs("data/cards")
    assert store.delete(thingy.id) is True

    with pytest.raises(NotFound, match="Thingy not found"):
        files.mkdirs("data/cards/c1")
    with pytest.raises(NotFound, match="Thingy not found"):
        files.create_json("data/cards/x.json", {"a": 1})
    assert store.rename(thingy.id, "Renamed") is None
    assert not (tmp_path / "thingies" / "thingies" / thingy.id).exists()


def test_deletion_racing_renames_and_writes_leaves_nothing_behind(
    store: LocalThingyStore, tmp_path: Path
) -> None:
    root = tmp_path / "thingies" / "thingies"
    for trial in range(8):
        thingy = store.create(ALICE, "Board")
        files = store.thingy_files(thingy.id)
        start = threading.Barrier(21)
        failures: list[BaseException] = []

        def rename(
            number: int, thingy_id: str = thingy.id, barrier=start, failures=failures
        ) -> None:
            barrier.wait()
            try:
                store.rename(thingy_id, f"Name {number}")
            except BaseException as exc:
                failures.append(exc)

        def write(number: int, scope=files, barrier=start, failures=failures) -> None:
            barrier.wait()
            try:
                scope.mkdirs(f"data/cards/c{number}")
                scope.create_json(f"data/cards/c{number}/00000001.json", {"n": number})
            except NotFound:
                pass  # Deleted first: refused, as it should be.
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=rename, args=(n,)) for n in range(10)]
        threads += [threading.Thread(target=write, args=(n,)) for n in range(10)]
        for thread in threads:
            thread.start()
        start.wait()
        store.delete(thingy.id)
        for thread in threads:
            thread.join()
        assert failures == [], (trial, failures)
        assert not (root / thingy.id).exists(), trial
        assert store.get(thingy.id) is None


def test_reopening_removes_what_an_interrupted_deletion_left(tmp_path: Path) -> None:
    location = tmp_path / "thingies"
    store = LocalThingyStore(str(location))
    kept = store.create(ALICE, "Board")
    (location / ".trash" / "leftover" / "files").mkdir(parents=True)
    orphan = location / "thingies" / ("a" * 32)
    (orphan / "files" / "data").mkdir(parents=True)
    fresh = location / "thingies" / ("b" * 32)
    (fresh / "files").mkdir(parents=True)
    old = 1_000_000_000
    os.utime(orphan, (old, old))

    LocalThingyStore(str(location))
    assert not (location / ".trash" / "leftover").exists()
    assert not orphan.exists()
    # Maybe a create still in progress, and live thingies, are left alone.
    assert fresh.exists()
    assert (location / "thingies" / kept.id / "thingy.json").exists()


def test_deleting_a_users_files_empties_their_scope(store: LocalThingyStore) -> None:
    files = store.user_files(ALICE)
    files.mkdirs("shelf/pins")
    files.create_json("shelf/pins/all.json", {"items": []})
    store.user_files(BOB).mkdirs("shelf")

    store.delete_user_files(ALICE)
    store.delete_user_files(ALICE)
    with pytest.raises(NotFound):
        files.list_paths("shelf")
    assert store.user_files(BOB).list_paths("shelf") == []


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("0123456789abcdef0123456789abcdef", True),  # this store's IDs
        ("102717306144392", True),  # another store's decimal IDs
        ("board-1", True),
        ("", False),
        ("-board", False),
        ("Board", False),
        ("a/b", False),
        ("a" * 65, False),
        (123, False),
    ],
)
def test_thingy_ids_follow_the_store_contract(value: object, valid: bool) -> None:
    assert is_thingy_id(value) is valid
