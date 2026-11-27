"""LocalGadgetStore: gadget metadata, owner index, create-only files and path safety."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import pytest

import omnigent.stores.gadget_store.local as local_module
from omnigent.stores.gadget_store import (
    MAX_FILE_BYTES,
    CapacityExceeded,
    Conflict,
    CorruptData,
    InvalidInput,
    NotFound,
    is_gadget_id,
)
from omnigent.stores.gadget_store.local import LocalGadgetStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def store(tmp_path: Path) -> LocalGadgetStore:
    return LocalGadgetStore(str(tmp_path / "gadgets"))


def test_gadgets_are_created_listed_renamed_and_deleted_per_owner(
    store: LocalGadgetStore,
) -> None:
    first = store.create(ALICE, "Board")
    second = store.create(ALICE, "Report")
    other = store.create(BOB, "Tracker")
    assert len(first.id) == 32 and first.owner_id == ALICE

    assert sorted(gadget.id for gadget in store.iter_owned(ALICE)) == sorted([first.id, second.id])
    assert [gadget.id for gadget in store.iter_owned(BOB)] == [other.id]
    assert list(store.iter_owned("carol@example.com")) == []
    assert store.get_many([first.id, "f" * 32, other.id]) == {first.id: first, other.id: other}

    renamed = store.rename(first.id, "Launch board")
    assert renamed is not None and renamed.name == "Launch board"
    assert store.get(first.id) == renamed
    assert store.rename("f" * 32, "Nothing") is None

    store.gadget_files(first.id).mkdirs("data/cards")
    store.gadget_files(first.id).create_json("data/cards/one.json", {"title": "Ship"})
    assert store.delete(first.id) is True
    assert store.get(first.id) is None
    assert store.delete(first.id) is False
    assert [gadget.id for gadget in store.iter_owned(ALICE)] == [second.id]
    # The deleted gadget's files are gone too, and a new one starts empty.
    with pytest.raises(NotFound):
        store.gadget_files(first.id).read_json("data/cards/one.json")


@pytest.mark.parametrize("gadget_id", ["", "../x", "A" * 32, "-a", "a" * 65, 7])
def test_malformed_ids_name_no_gadget(store: LocalGadgetStore, gadget_id: object) -> None:
    assert store.get(gadget_id) is None  # type: ignore[arg-type]


def test_a_store_reopened_on_the_same_directory_sees_the_same_gadgets(tmp_path: Path) -> None:
    created = LocalGadgetStore(str(tmp_path)).create(ALICE, "Board")
    LocalGadgetStore(str(tmp_path)).gadget_files(created.id).mkdirs("code")
    assert LocalGadgetStore(str(tmp_path)).get(created.id) == created


def test_files_are_create_only_and_read_back(store: LocalGadgetStore) -> None:
    files = store.gadget_files(store.create(ALICE, "Board").id)
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
    store: LocalGadgetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = store.gadget_files(store.create(ALICE, "Board").id)
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


def test_concurrent_creates_of_one_file_have_exactly_one_winner(store: LocalGadgetStore) -> None:
    files = store.gadget_files(store.create(ALICE, "Board").id)
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
def test_unsafe_paths_are_refused(store: LocalGadgetStore, path: str) -> None:
    files = store.gadget_files(store.create(ALICE, "Board").id)
    with pytest.raises(InvalidInput):
        files.read_json(path)
    with pytest.raises(InvalidInput):
        files.mkdirs(path)


def test_a_symlink_cannot_lead_a_scope_outside_itself(
    store: LocalGadgetStore, tmp_path: Path
) -> None:
    gadget = store.create(ALICE, "Board")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text('{"secret": true}')
    root = tmp_path / "gadgets" / "gadgets" / gadget.id / "files"
    os.symlink(outside, root / "link")
    with pytest.raises(InvalidInput):
        store.gadget_files(gadget.id).read_json("link/secret.json")


def test_corrupt_and_oversized_files_fail_closed(store: LocalGadgetStore, tmp_path: Path) -> None:
    gadget = store.create(ALICE, "Board")
    files = store.gadget_files(gadget.id)
    files.mkdirs("data")
    root = tmp_path / "gadgets" / "gadgets" / gadget.id / "files" / "data"
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


def test_each_user_has_their_own_files(store: LocalGadgetStore) -> None:
    for user in (ALICE, BOB, "../odd/user"):
        store.user_files(user).mkdirs("shelf")
        store.user_files(user).create_json("shelf/pins.json", {"owner": user})
    assert store.user_files(ALICE).read_json("shelf/pins.json") == {"owner": ALICE}
    assert store.user_files("../odd/user").read_json("shelf/pins.json") == {"owner": "../odd/user"}


def test_a_user_id_holding_a_separator_never_names_another_registrations_files(
    store: LocalGadgetStore,
) -> None:
    store.user_files("a", owner_registration="b").mkdirs("shelf")
    store.user_files("a", owner_registration="b").create_json("shelf/pins.json", {"owner": "a"})
    # An ID that spells "a" and "b" joined by a newline is another person's scope.
    with pytest.raises(NotFound):
        store.user_files("a\nb").read_json("shelf/pins.json")


def test_metadata_with_the_wrong_shape_is_corrupt(store: LocalGadgetStore, tmp_path: Path) -> None:
    gadget = store.create(ALICE, "Board")
    metadata = tmp_path / "gadgets" / "gadgets" / gadget.id / "gadget.json"
    metadata.write_text(json.dumps({"kind": "other", "id": gadget.id}))
    with pytest.raises(CorruptData):
        store.get(gadget.id)


def test_writes_after_a_deletion_fail_and_never_bring_its_files_back(
    store: LocalGadgetStore, tmp_path: Path
) -> None:
    gadget = store.create(ALICE, "Board")
    files = store.gadget_files(gadget.id)
    files.mkdirs("data/cards")
    assert store.delete(gadget.id) is True

    with pytest.raises(NotFound, match="Gadget not found"):
        files.mkdirs("data/cards/c1")
    with pytest.raises(NotFound, match="Gadget not found"):
        files.create_json("data/cards/x.json", {"a": 1})
    assert store.rename(gadget.id, "Renamed") is None
    assert not (tmp_path / "gadgets" / "gadgets" / gadget.id).exists()


def test_deletion_racing_renames_and_writes_leaves_nothing_behind(
    store: LocalGadgetStore, tmp_path: Path
) -> None:
    root = tmp_path / "gadgets" / "gadgets"
    for trial in range(8):
        gadget = store.create(ALICE, "Board")
        files = store.gadget_files(gadget.id)
        start = threading.Barrier(21)
        failures: list[BaseException] = []

        def rename(
            number: int, gadget_id: str = gadget.id, barrier=start, failures=failures
        ) -> None:
            barrier.wait()
            try:
                store.rename(gadget_id, f"Name {number}")
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
        store.delete(gadget.id)
        for thread in threads:
            thread.join()
        assert failures == [], (trial, failures)
        assert not (root / gadget.id).exists(), trial
        assert store.get(gadget.id) is None


def test_reopening_removes_what_an_interrupted_deletion_left(tmp_path: Path) -> None:
    location = tmp_path / "gadgets"
    store = LocalGadgetStore(str(location))
    kept = store.create(ALICE, "Board")
    (location / ".trash" / "leftover" / "files").mkdir(parents=True)
    # Creates that stopped before their metadata: each left its note.
    old = 1_000_000_000
    orphan = location / "gadgets" / ("a" * 32)
    (orphan / "files" / "data").mkdir(parents=True)
    fresh = location / "gadgets" / ("b" * 32)
    (fresh / "files").mkdir(parents=True)
    for gadget_id, stamp in (("a" * 32, old), ("b" * 32, None)):
        note = location / ".creating" / gadget_id
        note.touch()
        if stamp is not None:
            os.utime(note, (stamp, stamp))

    LocalGadgetStore(str(location))
    assert not (location / ".trash" / "leftover").exists()
    assert not orphan.exists()
    assert not (location / ".creating" / ("a" * 32)).exists()
    # Maybe a create still in progress, and live gadgets, are left alone.
    assert fresh.exists()
    assert (location / "gadgets" / kept.id / "gadget.json").exists()


def test_reopening_removes_a_create_that_stopped_before_its_owner_marker(tmp_path: Path) -> None:
    location = tmp_path / "gadgets"
    store = LocalGadgetStore(str(location))
    stopped = store.create(ALICE, "Half made")
    finished = store.create(ALICE, "Board")
    # One create stopped after its metadata but before its owner marker; the other
    # finished but couldn't clear its note. Both notes are old.
    [marker] = (location / "owners").rglob(stopped.id)
    marker.unlink()
    old = 1_000_000_000
    for gadget in (stopped, finished):
        note = location / ".creating" / gadget.id
        note.touch()
        os.utime(note, (old, old))

    reopened = LocalGadgetStore(str(location))
    # The gadget no one was handed is gone, so owner pages and account cleanup agree.
    assert reopened.get(stopped.id) is None
    assert not (location / "gadgets" / stopped.id).exists()
    assert reopened.get(finished.id) == finished
    assert [gadget.id for gadget in reopened.iter_owned(ALICE)] == [finished.id]
    assert not list((location / ".creating").iterdir())


def _hold_owner_index(store: LocalGadgetStore, location: Path, owner: str):  # type: ignore[no-untyped-def]
    """Another process's hold on *owner*'s owner index."""
    from filelock import FileLock

    name = store._owner_dir(owner).name
    (location / ".locks").mkdir(exist_ok=True)
    return FileLock(str(location / ".locks" / f"{name}.lock"))


def test_a_create_succeeds_when_its_cleanup_of_an_old_note_finds_a_busy_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After the create commits, its sweep repairs another owner's stopped create, whose
    # index another process holds: that repair waits for a later sweep; the create stands.
    from omnigent.stores.gadget_store import local

    location = tmp_path / "gadgets"
    store = LocalGadgetStore(str(location))
    stopped = store.create(BOB, "Half made")
    [marker] = (location / "owners").rglob(stopped.id)
    marker.unlink()
    note = location / ".creating" / stopped.id
    note.touch()
    os.utime(note, (1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(local, "_OWNER_INDEX_WAIT_S", 0.05)

    with _hold_owner_index(store, location, BOB):
        board = store.create(ALICE, "Board")

    assert [gadget.id for gadget in store.iter_owned(ALICE)] == [board.id]
    assert note.exists()  # Left for a later sweep.
    LocalGadgetStore(str(location))
    assert not note.exists() and store.get(stopped.id) is None


def test_a_delete_succeeds_when_retiring_its_emptied_segment_finds_a_busy_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.stores.gadget_store import local

    monkeypatch.setattr(local, "_OWNER_SEGMENT_SIZE", 1)
    location = tmp_path / "gadgets"
    store = LocalGadgetStore(str(location))
    first = store.create(ALICE, "First")
    second = store.create(ALICE, "Second")
    monkeypatch.setattr(local, "_OWNER_INDEX_WAIT_S", 0.05)

    with _hold_owner_index(store, location, ALICE):
        assert store.delete(first.id) is True

    assert store.get(first.id) is None
    assert [gadget.id for gadget in store.iter_owned(ALICE)] == [second.id]


def test_a_start_cleans_up_a_bounded_amount_and_never_lists_every_gadget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.stores.gadget_store import local

    location = tmp_path / "gadgets"
    store = LocalGadgetStore(str(location))
    for _ in range(5):
        store.create(ALICE, "Board")
    # A large deleted tree, and many creates that stopped long ago.
    for n in range(50):
        (location / ".trash" / "big" / f"d{n}").mkdir(parents=True)
        (location / ".trash" / "big" / f"d{n}" / "f.json").write_text("{}")
    old = 1_000_000_000
    (location / ".creating").mkdir(exist_ok=True)
    for n in range(80):
        note = location / ".creating" / f"{n:032x}"
        note.touch()
        os.utime(note, (old, old))
    monkeypatch.setattr(local, "_TRASH_ENTRIES_PER_CALL", 30)
    listed: list[str] = []
    real_scandir = os.scandir

    def watching(path):  # type: ignore[no-untyped-def]
        listed.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(local.os, "scandir", watching)
    LocalGadgetStore(str(location))
    # One step: some of the trash and some notes, and the gadgets directory isn't listed.
    assert not any(path.endswith("/gadgets") for path in listed)
    assert (location / ".trash" / "big").exists()
    assert 0 < len(list((location / ".creating").iterdir())) < 80
    # Later steps finish it.
    for _ in range(20):
        LocalGadgetStore(str(location))
    assert not (location / ".trash" / "big").exists()
    assert list((location / ".creating").iterdir()) == []


def test_deleting_a_users_files_empties_their_scope(store: LocalGadgetStore) -> None:
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
def test_gadget_ids_follow_the_store_contract(value: object, valid: bool) -> None:
    assert is_gadget_id(value) is valid


def test_delete_tree_removes_a_file_or_a_directory_at_once(store: LocalGadgetStore) -> None:
    files = store.gadget_files(store.create(ALICE, "Board").id)
    assert files.supports_delete
    files.mkdirs("data/cards/c1")
    files.create_json("data/cards/c1/00000001.json", {"n": 1})
    files.create_json("data/cards/one.json", {"n": 1})
    files.delete_tree("data/cards/one.json")
    assert files.list_paths("data/cards") == ["c1"]
    files.delete_tree("data/cards/c1")
    assert files.list_paths("data/cards") == []
    files.delete_tree("data/cards/missing")  # nothing to delete is not an error
    # The emptied path can be used again, even while the trash still holds the old tree.
    files.mkdirs("data/cards/c1")
    files.create_json("data/cards/c1/00000001.json", {"n": 2})
    assert files.read_json("data/cards/c1/00000001.json") == {"n": 2}


def test_delete_tree_detaches_at_once_and_removes_files_a_step_at_a_time(
    store: LocalGadgetStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.stores.gadget_store import local

    monkeypatch.setattr(local, "_TRASH_ENTRIES_PER_CALL", 5)
    files = store.gadget_files(store.create(ALICE, "Board").id)
    files.mkdirs("data/big")
    for n in range(40):
        files.create_json(f"data/big/{n:03d}.json", {"n": n})
    files.delete_tree("data/big")
    # Gone from the scope at once; its files go a few at a time.
    with pytest.raises(NotFound):
        files.list_paths("data/big")
    trash = tmp_path / "gadgets" / ".trash"
    assert sum(1 for _ in trash.rglob("*.json")) > 30
    files.mkdirs("data/big")
    files.create_json("data/big/000.json", {"n": "new"})
    assert files.read_json("data/big/000.json") == {"n": "new"}
    # Later deletions each remove a few more until the trash is empty.
    for n in range(20):
        files.create_json(f"data/tmp{n}.json", {})
        files.delete_tree(f"data/tmp{n}.json")
    assert not any(trash.rglob("*.json"))


def _owned_pages(store: LocalGadgetStore, owner: str, limit: int) -> list[list[str]]:
    pages: list[list[str]] = []
    cursor = None
    while True:
        page, cursor = store.list_owned_page(owner, limit=limit, after=cursor)
        pages.append([gadget.id for gadget in page])
        if cursor is None:
            return pages


def test_owned_gadgets_page_through_once_and_skip_markers_left_by_a_deletion(
    store: LocalGadgetStore, tmp_path: Path
) -> None:
    made = sorted(store.create(ALICE, f"Board {n}").id for n in range(5))
    # A deletion that stopped before removing its owner marker.
    stale = made[1]
    store.delete(stale)
    (segment,) = (tmp_path / "gadgets" / "owners").glob("*/s00000000")
    (segment / stale).touch()
    seen = [gadget_id for page in _owned_pages(store, ALICE, limit=2) for gadget_id in page]
    assert seen == [gadget_id for gadget_id in made if gadget_id != stale]
    assert sorted(gadget.id for gadget in store.iter_owned(ALICE)) == seen
    with pytest.raises(InvalidInput):
        store.list_owned_page(ALICE, limit=0)
    for cursor in ("../x", "s00000000", "s1/abc", made[0]):
        with pytest.raises(InvalidInput):
            store.list_owned_page(ALICE, limit=2, after=cursor)


def test_a_page_of_owned_gadgets_reads_a_bounded_part_of_a_large_account(
    store: LocalGadgetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_module, "_OWNER_SEGMENT_SIZE", 3)
    made = [store.create(ALICE, f"Board {n}").id for n in range(10)]
    owner_dir = next((tmp_path / "gadgets" / "owners").iterdir())
    assert sorted(path.name for path in owner_dir.iterdir()) == [
        "head.json",
        "s00000000",
        "s00000001",
        "s00000002",
        "s00000003",
    ]
    listed: list[str] = []
    real_listdir = os.listdir

    def counting_listdir(path: object) -> list[str]:
        listed.append(Path(str(path)).name)
        return real_listdir(path)

    monkeypatch.setattr(local_module.os, "listdir", counting_listdir)
    page, cursor = store.list_owned_page(ALICE, limit=2)
    # Only the first segment's markers: the head names where the segments start and end.
    assert listed == ["s00000000"] and len(page) == 2 and cursor
    listed.clear()

    # Deleting every gadget in the next two segments leaves them empty: a page lists at
    # most two of them, returns short, and still continues.
    for gadget_id in made[3:9]:
        store.delete(gadget_id)
    listed.clear()
    page, cursor = store.list_owned_page(ALICE, limit=2, after=cursor)
    assert len(listed) <= 2 and cursor is not None
    monkeypatch.setattr(local_module.os, "listdir", real_listdir)
    seen = [gadget_id for page in _owned_pages(store, ALICE, limit=2) for gadget_id in page]
    assert sorted(seen) == sorted([*made[:3], made[9]])
    # The emptied segments are gone, so later pages don't read them again.
    assert sorted(path.name for path in owner_dir.iterdir()) == [
        "head.json",
        "s00000000",
        "s00000003",
    ]


def test_retiring_a_first_segment_probes_a_bounded_number_of_missing_ones(
    store: LocalGadgetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first segment stays alive while many after it fill and empty; when it finally
    # empties, one request skips at most _HEAD_PROBES_PER_CALL missing segments and records
    # how far it got, and later pages carry on from there.
    monkeypatch.setattr(local_module, "_OWNER_SEGMENT_SIZE", 1)
    first = store.create(ALICE, "First").id
    passing = [store.create(ALICE, f"Passing {n}").id for n in range(30)]
    kept = store.create(ALICE, "Kept").id
    for gadget_id in passing:
        store.delete(gadget_id)
    owner_dir = next((tmp_path / "gadgets" / "owners").iterdir())
    probes = 0
    real_exists = Path.exists

    def counting_exists(self: Path, *args: object, **kwargs: object) -> bool:
        nonlocal probes
        if self.parent == owner_dir and self.name.startswith("s"):
            probes += 1
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", counting_exists)
    store.delete(first)
    assert probes <= local_module._HEAD_PROBES_PER_CALL
    first_after, last = store._read_head(owner_dir)
    assert 0 < first_after < last
    # Each page probes no more than the bound, however many pages it takes.
    real_page = store.list_owned_page
    most = 0

    def page(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal probes, most
        probes = 0
        result = real_page(*args, **kwargs)  # type: ignore[arg-type]
        most = max(most, probes)
        return result

    monkeypatch.setattr(store, "list_owned_page", page)
    seen = [gadget_id for page in _owned_pages(store, ALICE, limit=10) for gadget_id in page]
    assert seen == [kept]
    assert most <= local_module._HEAD_PROBES_PER_CALL


def test_emptied_segments_and_markers_a_deletion_left_dont_count_as_owned(
    store: LocalGadgetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_module, "_OWNER_SEGMENT_SIZE", 3)
    made = [store.create(ALICE, f"Board {n}").id for n in range(10)]
    # Three segments emptied, and in the newest a deletion that stopped after moving the
    # gadget away, leaving its marker.
    for gadget_id in made[:9]:
        store.delete(gadget_id)
    shutil.rmtree(tmp_path / "gadgets" / "gadgets" / made[9])
    assert store.owns_any(ALICE) is False
    owner_dir = next((tmp_path / "gadgets" / "owners").iterdir())
    assert sorted(path.name for path in owner_dir.iterdir()) == ["head.json", "s00000003"]
    assert list((owner_dir / "s00000003").iterdir()) == []
    # A new gadget is owned again.
    store.create(ALICE, "Again")
    assert store.owns_any(ALICE) is True


def test_two_stores_on_one_directory_never_lose_each_others_owner_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Store B retires an emptied segment while store A, another process on the same
    # directory, starts a new segment for a create: B must not write back the head it read.
    monkeypatch.setattr(local_module, "_OWNER_SEGMENT_SIZE", 1)
    store_a = LocalGadgetStore(str(tmp_path / "gadgets"))
    store_b = LocalGadgetStore(str(tmp_path / "gadgets"))
    first = store_a.create(ALICE, "First").id
    kept = store_a.create(ALICE, "Kept").id
    # A deletion that stopped after removing the gadget and its marker left s00000000 empty.
    (owner_dir,) = (tmp_path / "gadgets" / "owners").iterdir()
    (owner_dir / "s00000000" / first).unlink()
    shutil.rmtree(tmp_path / "gadgets" / "gadgets" / first)
    real_write_head = LocalGadgetStore._write_head
    created: list[str] = []
    workers: list[threading.Thread] = []

    def write_head(self: LocalGadgetStore, owner_dir: Path, first_n: int, last_n: int) -> None:
        if self is store_b and not workers:
            # A's create starts while B holds the head it is about to rewrite, and gets as far
            # as it can before B writes.
            worker = threading.Thread(
                target=lambda: created.append(store_a.create(ALICE, "Racing").id)
            )
            workers.append(worker)
            worker.start()
            worker.join(timeout=0.5)
        real_write_head(self, owner_dir, first_n, last_n)

    monkeypatch.setattr(LocalGadgetStore, "_write_head", write_head)
    store_b.list_owned_page(ALICE, limit=10)
    workers[0].join(timeout=10)
    assert created
    seen = [gadget_id for page in _owned_pages(store_a, ALICE, limit=10) for gadget_id in page]
    assert sorted(seen) == sorted([kept, *created])


def test_an_owner_registration_is_its_own_account(store: LocalGadgetStore) -> None:
    old = store.create(ALICE, "Old", owner_registration="g1")
    store.user_files(ALICE, owner_registration="g1").mkdirs("shelf")
    new = store.create(ALICE, "New", owner_registration="g2")

    assert [g.id for g in store.iter_owned(ALICE, owner_registration="g2")] == [new.id]
    assert store.get(old.id).owner_registration == "g1"
    with pytest.raises(NotFound):
        store.user_files(ALICE, owner_registration="g2").list_paths("shelf")
    store.delete_user_files(ALICE, owner_registration="g2")
    assert store.user_files(ALICE, owner_registration="g1").list_paths("shelf") == []


def test_a_deleted_gadgets_files_are_removed_a_bounded_number_at_a_time(
    store: LocalGadgetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_module, "_TRASH_ENTRIES_PER_CALL", 12)
    big = store.create(ALICE, "Board")
    files = store.gadget_files(big.id)
    files.mkdirs("data")
    for n in range(40):
        files.create_json(f"data/{n:02d}.json", {"n": n})
    trash = tmp_path / "gadgets" / ".trash"

    def left() -> int:
        return sum(len(names) + len(dirs) for _, dirs, names in os.walk(trash))

    # The gadget is gone at once; its files go a few at a time, with each later deletion.
    assert store.delete(big.id) is True
    assert store.get(big.id) is None
    assert left() > 0
    while left():
        before = left()
        store.delete(store.create(ALICE, "Scratch").id)
        assert left() < before


def test_draining_a_huge_trash_directory_reads_only_its_budget_of_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One detached gadget with a huge directory (frontend revisions have no lifetime bound):
    # each step looks at no more entries than its budget, wherever they are in the tree.
    trash = tmp_path / ".trash"
    huge = trash / "tree-1" / "files" / "revisions"
    huge.mkdir(parents=True)
    for n in range(300):
        (huge / f"{n:04d}.json").write_text("{}")
    examined = 0
    real_scandir = os.scandir

    class CountingScandir:
        def __init__(self, path: object) -> None:
            self._it = real_scandir(path)

        def __enter__(self) -> CountingScandir:
            return self

        def __exit__(self, *exc: object) -> None:
            self._it.close()

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal examined
            entry = next(self._it)
            examined += 1
            return entry

    monkeypatch.setattr(local_module.os, "scandir", CountingScandir)
    steps = 0
    while trash.exists() and any(trash.iterdir()):
        examined = 0
        removed = local_module._drain_trash(trash, budget=20)
        assert removed > 0
        # The budget, plus the levels walked back down after removing a directory.
        assert examined <= 20 + 4
        steps += 1
        assert steps < 100
    assert steps >= 300 // 20
    assert list(trash.iterdir()) == []


def test_a_store_opened_without_maintenance_touches_nothing_until_written(tmp_path: Path) -> None:
    location = tmp_path / "gadgets"
    (location / ".trash" / "left").mkdir(parents=True)
    store = LocalGadgetStore(str(location / "absent"), maintain=False)
    assert not (location / "absent").exists()
    LocalGadgetStore(str(location), maintain=False)
    # Its trash is left for a store that maintains it.
    assert (location / ".trash" / "left").exists()
    files = store.user_files("alice")
    files.mkdirs("pending")
    files.create_json("pending/note.json", {"user_id": "alice"})
    assert files.read_json("pending/note.json") == {"user_id": "alice"}
