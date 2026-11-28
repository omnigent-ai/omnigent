"""ImmutableDocumentStore: segments, capacity reclaim, idempotency and fail-closed reads."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import omnigent.stores.gadget_store.local as local_module
from omnigent.gadgets.documents import ImmutableDocumentStore, StoreLimits
from omnigent.stores.gadget_store import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    GadgetFiles,
    InvalidInput,
    NotFound,
)
from omnigent.stores.gadget_store.local import LocalGadgetStore

ACTOR = "alice@example.com"
SMALL = StoreLimits(
    max_revisions_per_record=12,
    revisions_per_segment=3,
    max_records_per_collection=2,
    max_list_entries=4,
    max_scan_per_list=4,
    idempotency_window=2,
    tombstone_retention_s=60,
    purge_abandon_s=30,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Env:
    def __init__(self, tmp_path: Path, limits: StoreLimits = SMALL) -> None:
        self.store = LocalGadgetStore(str(tmp_path / "gadgets"))
        self.gadget = self.store.create(ACTOR, "Board")
        self.files = self.store.gadget_files(self.gadget.id)
        self.clock = Clock()
        self.docs = ImmutableDocumentStore(self.files, "data", limits=limits, clock=self.clock)
        self.root = tmp_path / "gadgets" / "gadgets" / self.gadget.id / "files" / "data"

    def write(self, doc_id: str, times: int) -> str:
        """Create *doc_id* and replace it until it has *times* revisions; its last version."""
        version = self.docs.create("cards", doc_id, {"n": 1}, f"{doc_id}-1", ACTOR).version
        for n in range(2, times + 1):
            version = self.docs.replace(
                "cards", doc_id, {"n": n}, version, f"{doc_id}-{n}", ACTOR
            ).version
        return version


def _list_all(docs: ImmutableDocumentStore, collection: str) -> list[str]:
    """Every live record's ID, following cursors past pages a scan budget cut short."""
    ids: list[str] = []
    cursor = None
    while True:
        page = docs.list(collection, cursor=cursor)
        ids.extend(item.id for item in page.items)
        if page.next_cursor is None:
            return ids
        cursor = page.next_cursor


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_a_long_history_rolls_into_segments_and_stays_readable(env: Env) -> None:
    versions = [env.docs.create("cards", "c1", {"n": 1}, "op-1", ACTOR).version]
    for n in range(2, 11):
        versions.append(
            env.docs.replace("cards", "c1", {"n": n}, versions[-1], f"op-{n}", ACTOR).version
        )
    record = env.root / "cards" / "c1"
    assert sorted(path.name for path in record.iterdir()) == [
        "00000001.json",
        "00000002.json",
        "00000003.json",
        "s00000002",
        "s00000003",
        "s00000004",
    ]
    assert sorted(path.name for path in (record / "s00000004").iterdir()) == ["00000010.json"]
    current = env.docs.get("cards", "c1")
    assert current is not None and current.value == {"n": 10}
    summary = env.docs.verify_record("cards", "c1")
    assert summary is not None and summary.revisions == 10
    old = env.docs.get_version("cards", "c1", versions[0])
    assert old is not None and old.value == {"n": 1}
    # A retry of the newest write, whose predecessor sits in the previous segment, replays.
    again = env.docs.replace("cards", "c1", {"n": 10}, versions[8], "op-10", ACTOR)
    assert again.version == versions[9]


def test_reading_a_record_examines_a_bounded_number_of_entries_as_its_history_grows(
    tmp_path: Path,
) -> None:
    limits = StoreLimits(**{**vars(WIDE), "revisions_per_segment": 10})
    files, docs, _clock = _counting(tmp_path, limits)
    version = docs.create("cards", "c1", {"n": 0}, "op-0", ACTOR).version
    costs: dict[int, int] = {}
    for n in range(1, 400):
        version = docs.replace("cards", "c1", {"n": n}, version, f"op-{n}", ACTOR).version
        if n + 1 in (10, 100, 400):
            files.reset()
            assert docs.get("cards", "c1").value == {"n": n}  # type: ignore[union-attr]
            costs[n + 1] = files.examined
    # 40 segments of 10: a read lists one segment and probes segment starts, never the
    # record's directory with every segment in it.
    assert all(cost <= 10 + 2 * 7 + 4 for cost in costs.values()), costs
    assert costs[400] - costs[10] <= 2 * 6, costs


def test_a_record_stops_at_its_total_revision_limit_and_stays_readable(env: Env) -> None:
    version = env.write("c1", 12)
    with pytest.raises(CapacityExceeded):
        env.docs.replace("cards", "c1", {"n": 13}, version, "c1-13", ACTOR)
    assert env.docs.get("cards", "c1").value == {"n": 12}  # type: ignore[union-attr]


def test_an_empty_newest_segment_left_by_a_stopped_writer_is_continued(env: Env) -> None:
    version = env.write("c1", 3)
    (env.root / "cards" / "c1" / "s00000002").mkdir()
    assert env.docs.get("cards", "c1").version == version  # type: ignore[union-attr]
    nxt = env.docs.replace("cards", "c1", {"n": 4}, version, "c1-4", ACTOR)
    assert env.docs.get("cards", "c1").version == nxt.version  # type: ignore[union-attr]


def test_tampered_revisions_and_numbering_gaps_fail_closed(env: Env) -> None:
    env.write("c1", 5)
    record = env.root / "cards" / "c1"
    path = record / "s00000002" / "00000004.json"
    raw = json.loads(path.read_text())
    raw["value"] = {"n": 400}
    path.write_text(json.dumps(raw))
    with pytest.raises(CorruptData):
        env.docs.get("cards", "c1")
    with pytest.raises(CorruptData):
        env.docs.verify_record("cards", "c1")

    # A gap in the newest segment fails a read; one in an older, full segment is found by
    # verifying the record, since a read lists only the newest segment.
    env.write("c2", 5)
    (env.root / "cards" / "c2" / "s00000002" / "00000004.json").unlink()
    with pytest.raises(CorruptData):
        env.docs.get("cards", "c2")


def test_a_gap_in_an_older_segment_is_found_by_verifying_the_record(env: Env) -> None:
    env.write("c1", 5)
    (env.root / "cards" / "c1" / "00000002.json").unlink()
    with pytest.raises(CorruptData):
        env.docs.verify_record("cards", "c1")


def test_a_gap_between_segments_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path, StoreLimits(**{**vars(SMALL), "max_records_per_collection": 4}))
    env.write("c3", 8)
    (env.root / "cards" / "c3" / "s00000002").rename(env.root / "cards" / "c3" / "moved")
    with pytest.raises(CorruptData):
        env.docs.get("cards", "c3")


def test_a_retry_beyond_the_idempotency_window_conflicts(env: Env) -> None:
    created = env.docs.create("cards", "c1", {"n": 1}, "op-1", ACTOR)
    v2 = env.docs.replace("cards", "c1", {"n": 2}, created.version, "op-2", ACTOR)
    # Within the window the retry answers with the original result.
    assert env.docs.replace("cards", "c1", {"n": 2}, created.version, "op-2", ACTOR) == v2
    v3 = env.docs.replace("cards", "c1", {"n": 3}, v2.version, "op-3", ACTOR)
    env.docs.replace("cards", "c1", {"n": 4}, v3.version, "op-4", ACTOR)
    with pytest.raises(Conflict) as error:
        env.docs.replace("cards", "c1", {"n": 2}, created.version, "op-2", ACTOR)
    assert error.value.reason == "version_mismatch"


def test_a_recreated_record_keeps_its_history_and_refuses_stale_versions(env: Env) -> None:
    first = env.docs.create("cards", "c1", {"n": 1}, "op-1", ACTOR)
    env.docs.delete("cards", "c1", first.version, "op-del", ACTOR)
    again = env.docs.create("cards", "c1", {"n": 2}, "op-again", ACTOR)
    assert again.version != first.version
    with pytest.raises((Conflict, NotFound)):
        env.docs.replace("cards", "c1", {"n": 3}, first.version, "op-stale", ACTOR)
    # A late retry of the first create neither resurrects that generation nor adds one.
    with pytest.raises(Conflict) as error:
        env.docs.create("cards", "c1", {"n": 1}, "op-1", ACTOR)
    assert error.value.reason == "already_exists"
    assert env.docs.verify_record("cards", "c1").generation == 2  # type: ignore[union-attr]


def test_an_operation_id_used_a_few_revisions_ago_is_reuse_even_at_the_current_version(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, StoreLimits(**{**vars(SMALL), "idempotency_window": 16}))
    first = env.docs.create("cards", "a", {"n": 1}, "op-create", ACTOR)
    second = env.docs.replace("cards", "a", {"n": 2}, first.version, "op-two", ACTOR)
    third = env.docs.replace("cards", "a", {"n": 3}, second.version, "op-three", ACTOR)
    fourth = env.docs.replace("cards", "a", {"n": 4}, third.version, "op-four", ACTOR)

    # "op-two" names another change, three revisions back, inside the replay window.
    with pytest.raises(Conflict) as reused:
        env.docs.replace("cards", "a", {"n": 99}, fourth.version, "op-two", ACTOR)
    assert reused.value.reason == "operation_reuse"
    assert env.docs.get("cards", "a").value == {"n": 4}  # type: ignore[union-attr]
    # Its own retry still replays.
    again = env.docs.replace("cards", "a", {"n": 2}, first.version, "op-two", ACTOR)
    assert again.version == second.version


def test_a_page_can_be_empty_and_still_continue(tmp_path: Path) -> None:
    limits = StoreLimits(
        **{**vars(SMALL), "max_records_per_collection": 4, "max_scan_per_list": 2}
    )
    env = Env(tmp_path, limits)
    for doc_id in ("a", "b", "c"):
        created = env.docs.create("cards", doc_id, {"id": doc_id}, f"op-{doc_id}", ACTOR)
        if doc_id != "c":
            env.docs.delete("cards", doc_id, created.version, f"del-{doc_id}", ACTOR)
    page = env.docs.list("cards", limit=2)
    assert page.items == () and page.next_cursor is not None
    rest = env.docs.list("cards", limit=2, cursor=page.next_cursor)
    assert [item.id for item in rest.items] == ["c"] and rest.next_cursor is None
    # A well-formed cursor naming a position past any collection is refused before any
    # file is read, instead of naming an order file too long for the file system.
    far = env.docs._encode_cursor("cards", 10**300)
    assert len(far) <= 512
    with pytest.raises(InvalidInput):
        env.docs.list("cards", limit=2, cursor=far)


def test_records_deleted_past_retention_give_their_capacity_back(env: Env) -> None:
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.create("cards", "b", {"n": 1}, "op-b", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    # Within the retention the tombstone still answers retries, so it keeps its slot.
    with pytest.raises(CapacityExceeded):
        env.docs.create("cards", "c", {"n": 1}, "op-c", ACTOR)
    env.clock.advance(61)
    created = env.docs.create("cards", "c", {"n": 1}, "op-c", ACTOR)
    assert created.id == "c"
    assert not (env.root / "cards" / "a").exists()
    assert _list_all(env.docs, "cards") == ["b", "c"]
    # Churn never exhausts the collection: delete and create again, many times over.
    current = created
    for n in range(6):
        env.docs.delete("cards", current.id, current.version, f"del-{n}", ACTOR)
        env.clock.advance(61)
        current = env.docs.create("cards", f"n{n}", {"n": n}, f"op-n{n}", ACTOR)
    assert sorted(_list_all(env.docs, "cards")) == ["b", "n5"]


def test_a_removed_record_can_be_created_again_from_scratch(env: Env) -> None:
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.create("cards", "b", {"n": 1}, "op-b", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    c = env.docs.create("cards", "c", {"n": 1}, "op-c", ACTOR)
    env.docs.delete("cards", "c", c.version, "del-c", ACTOR)
    env.clock.advance(61)
    again = env.docs.create("cards", "a", {"n": 2}, "op-a2", ACTOR)
    assert env.docs.verify_record("cards", "a").revisions == 1  # type: ignore[union-attr]
    assert again.value == {"n": 2}


def _tombstone_ending_a_segment(env: Env) -> None:
    """Record ``a`` with three revisions, a full first segment, the last a tombstone."""
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    a = env.docs.replace("cards", "a", {"n": 2}, a.version, "op-a-2", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)


def test_a_stale_writer_never_revives_a_record_reclaimed_meanwhile(env: Env) -> None:
    _tombstone_ending_a_segment(env)
    ref = env.docs._ref("cards", "a")
    stale = env.docs._load_head(ref)
    assert stale is not None and stale.count == 3
    # The writer read the tombstone, then paused while another create reclaimed it.
    env.docs._purge(ref, stale)
    assert env.docs.verify_record("cards", "a") is None
    with pytest.raises(Conflict):
        env.docs._append(ref, stale, "create", {"n": 9}, "op-stale", ACTOR, "d" * 64)
    # Nothing acknowledged is left unreadable, and the record starts again from scratch.
    assert env.docs.verify_record("cards", "a") is None
    assert not (env.root / "cards" / "a" / "s00000002").exists()
    again = env.docs.create("cards", "a", {"n": 3}, "op-a3", ACTOR)
    assert env.docs.get("cards", "a") == again
    assert env.docs.verify_record("cards", "a").revisions == 1  # type: ignore[union-attr]


def test_a_stale_writer_that_stops_after_writing_leaves_the_new_record_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    ref = env.docs._ref("cards", "a")
    stale = env.docs._load_head(ref)
    assert stale is not None and stale.count == 2
    # While the writer is paused, the record is removed and created again.
    env.docs._purge(ref, stale)
    fresh = env.docs.create("cards", "a", {"n": 2}, "op-a2", ACTOR)
    # The writer resumes and would stop (crash) right after writing its revision.
    real_create = env.files.create_json

    def create_then_crash(path: str, value: dict) -> None:
        real_create(path, value)
        if path.endswith("cards/a/00000003.json"):
            raise RuntimeError("stopped right after writing")

    monkeypatch.setattr(env.files, "create_json", create_then_crash)
    with pytest.raises(Conflict):
        env.docs._append(ref, stale, "create", {"n": 9}, "op-stale", ACTOR, "d" * 64)
    # Reopened, the new record is intact and still writable.
    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    docs = ImmutableDocumentStore(
        store.gadget_files(env.gadget.id), "data", limits=SMALL, clock=env.clock
    )
    assert docs.get("cards", "a") == fresh
    assert docs.verify_record("cards", "a").revisions == 1  # type: ignore[union-attr]
    assert docs.replace("cards", "a", {"n": 3}, fresh.version, "op-a3", ACTOR).value == {"n": 3}


def test_a_stale_writer_holding_an_old_create_never_indexes_it_in_the_new_record(
    env: Env,
) -> None:
    # The writer read a record whose newest revision is its create, then paused while the
    # record was deleted, removed and created again by another operation.
    old = env.docs.create("cards", "a", {"n": 1}, "op-old", ACTOR)
    ref = env.docs._ref("cards", "a")
    stale = env.docs._load_head(ref)
    assert stale is not None and stale.latest.kind == "create"
    env.docs.delete("cards", "a", old.version, "del-a", ACTOR)
    env.clock.advance(61)
    env.docs._purge(ref, env.docs._load_head(ref))
    fresh = env.docs.create("cards", "a", {"n": 2}, "op-new", ACTOR)
    index = env.root.parent / env.docs._create_index_path(ref, "op-old")

    with pytest.raises(Conflict):
        env.docs._append(ref, stale, "replace", {"n": 9}, "op-stale", ACTOR, "d" * 64)

    # The new record's index never names the old create, whose operation ID is free for
    # a later create of this record to record its own receipt under.
    assert not index.exists()
    assert env.docs.get("cards", "a") == fresh


def test_a_reclaim_racing_a_writer_waits_for_it(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_module, "_EXCLUSIVE_WAIT_S", 10)
    _tombstone_ending_a_segment(env)
    ref = env.docs._ref("cards", "a")
    head = env.docs._load_head(ref)
    assert head is not None
    # A reclaim starts while the writer is between its check and its write.
    real_mkdirs = type(env.files).mkdirs
    raced: list[BaseException | None] = []

    def reclaim() -> None:
        try:
            env.docs._purge(ref, head)
            raced.append(None)
        except Conflict as error:
            raced.append(error)

    thread = threading.Thread(target=reclaim)

    def mkdirs_while_a_reclaim_starts(self, path: str) -> None:
        if path.endswith("cards/a/s00000002") and not thread.is_alive() and not raced:
            thread.start()
            thread.join(0.5)
            assert thread.is_alive()  # It waits for the writer's lock.
        real_mkdirs(self, path)

    monkeypatch.setattr(type(env.files), "mkdirs", mkdirs_while_a_reclaim_starts)
    created = env.docs._append(ref, head, "create", {"n": 9}, "op-new", ACTOR, "d" * 64)
    thread.join(10)
    # The writer won; the reclaim found the record changed and left it alone.
    assert isinstance(raced[0], Conflict)
    assert env.docs.get("cards", "a") == created
    assert env.docs.verify_record("cards", "a").revisions == 4  # type: ignore[union-attr]


def test_a_create_that_stopped_after_claiming_capacity_gives_it_back(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    # A create of "b" claims its slot, then stops before writing the record.
    env.docs._ensure_record_capacity("cards", "b")
    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    docs = ImmutableDocumentStore(
        store.gadget_files(env.gadget.id), "data", limits=SMALL, clock=env.clock
    )
    # Until it is older than the abandon time, the slot stays the stopped create's.
    env.clock.advance(10)
    with pytest.raises(CapacityExceeded):
        docs.create("cards", "c", {"n": 1}, "op-c", ACTOR)
    env.clock.advance(21)
    assert docs.create("cards", "c", {"n": 1}, "op-c", ACTOR).id == "c"
    assert [item.id for item in docs.list("cards").items] == ["a", "c"]
    # The collection is full again with readable records; "b" is now refused.
    with pytest.raises(CapacityExceeded):
        docs.create("cards", "b", {"n": 1}, "op-b", ACTOR)


def test_a_slow_create_whose_claim_was_released_is_refused(env: Env) -> None:
    env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    claim = env.docs._ensure_record_capacity("cards", "b")
    env.clock.advance(31)
    # Another create frees the slow create's claim and takes the slot.
    env.docs.create("cards", "c", {"n": 1}, "op-c", ACTOR)
    ref = env.docs._ref("cards", "b")
    with pytest.raises(Conflict):
        env.docs._append(ref, None, "create", {"n": 1}, "op-b", ACTOR, "d" * 64, claim=claim)
    assert env.docs.get("cards", "b") is None
    assert sorted(item.id for item in env.docs.list("cards").items) == ["a", "c"]


def test_a_live_create_keeps_its_claim(env: Env) -> None:
    env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    claim = env.docs._ensure_record_capacity("cards", "b")
    env.clock.advance(20)
    with pytest.raises(CapacityExceeded):
        env.docs.create("cards", "c", {"n": 1}, "op-c", ACTOR)
    ref = env.docs._ref("cards", "b")
    created = env.docs._append(ref, None, "create", {"n": 1}, "op-b", ACTOR, "d" * 64, claim=claim)
    assert env.docs.get("cards", "b") == created


class _CreatedWhileReading(GadgetFiles):
    """A scope where another writer creates a record just after a reader found it missing."""

    def __init__(self, inner: GadgetFiles, on_miss: Callable[[str], None]) -> None:
        self.inner = inner
        self.supports_delete = inner.supports_delete
        self.on_miss = on_miss

    def exclusive(self, name: str):  # type: ignore[override]
        return self.inner.exclusive(name)

    def read_json(self, path: str) -> dict:
        try:
            return self.inner.read_json(path)
        except NotFound:
            self.on_miss(path)
            raise

    def create_json(self, path: str, value: dict) -> None:
        self.inner.create_json(path, value)

    def list_paths(self, path: str) -> list[str]:
        return self.inner.list_paths(path)

    def list_page(self, path: str, *, after: str | None, limit: int) -> tuple[list[str], bool]:
        return self.inner.list_page(path, after=after, limit=limit)

    def mkdirs(self, path: str) -> None:
        self.inner.mkdirs(path)

    def delete_tree(self, path: str) -> None:
        self.inner.delete_tree(path)


def test_a_record_created_while_it_is_being_read_is_not_reported_damaged(env: Env) -> None:
    created: list[str] = []

    def create_now(path: str) -> None:
        # The reader has just found the first revision missing; the record appears before
        # it lists the record's directory.
        if not created and path.endswith("/cards/c1/00000001.json"):
            created.append(path)
            env.docs.create("cards", "c1", {"n": 1}, "c1-1", ACTOR)

    reader = ImmutableDocumentStore(
        _CreatedWhileReading(env.files, create_now), "data", limits=SMALL, clock=env.clock
    )
    got = reader.get("cards", "c1")
    assert created
    assert got is not None and got.value == {"n": 1}


def test_a_late_remover_never_deletes_the_record_created_again(env: Env) -> None:
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    ref = env.docs._ref("cards", "a")
    head = env.docs._load_head(ref)
    assert head is not None
    # Two removers race: both write or see the same purge revision; the first finishes.
    original = env.docs._finish_purge
    env.docs._finish_purge = lambda ref, marker: None  # type: ignore[method-assign]
    env.docs._purge(ref, head)
    env.docs._finish_purge = original  # type: ignore[method-assign]
    purged = env.docs._load_head(ref)
    assert purged is not None
    marker = purged.latest
    env.docs._finish_purge(ref, marker)
    again = env.docs.create("cards", "a", {"n": 2}, "op-a2", ACTOR)
    # The slower remover resumes with the old purge revision: the new record survives.
    env.docs._finish_purge(ref, marker)
    assert env.docs.get("cards", "a") == again
    assert env.docs.verify_record("cards", "a").revisions == 1  # type: ignore[union-attr]


@pytest.mark.parametrize("through", ["the same path", "a symlink"])
def test_a_remover_paused_just_before_deleting_never_deletes_the_record_created_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, through: str
) -> None:
    monkeypatch.setattr(local_module, "_EXCLUSIVE_WAIT_S", 0.2)
    env = Env(tmp_path)
    if through == "a symlink":
        # Another server process opened the same storage through a different path.
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path / "gadgets")
        other_files = LocalGadgetStore(str(alias)).gadget_files(env.gadget.id)
    else:
        other_files = env.files
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    ref = env.docs._ref("cards", "a")
    head = env.docs._load_head(ref)
    assert head is not None
    original = env.docs._finish_purge
    env.docs._finish_purge = lambda ref, marker: None  # type: ignore[method-assign]
    env.docs._purge(ref, head)
    env.docs._finish_purge = original  # type: ignore[method-assign]
    marker = env.docs._load_head(ref).latest  # type: ignore[union-attr]

    # The first remover passes its last check, then stalls right before deleting the record.
    at_delete, resume = threading.Event(), threading.Event()
    real_delete = env.files.delete_tree
    stalled: list[str] = []

    def delete_tree(path: str) -> None:
        if path == ref.directory and not stalled:
            stalled.append(path)
            at_delete.set()
            resume.wait(60)
        real_delete(path)

    env.files.delete_tree = delete_tree  # type: ignore[method-assign]
    slow = threading.Thread(target=env.docs._finish_purge, args=(ref, marker))
    slow.start()
    assert at_delete.wait(10)
    # Its ticket is old enough to take over, but it still holds the record: the next
    # remover leaves the record alone, and nothing can create it again meanwhile.
    env.clock.advance(31)
    taker = ImmutableDocumentStore(other_files, "data", limits=SMALL, clock=env.clock)
    taker._finish_purge(ref, marker)
    assert taker._load_head(ref) is not None
    with pytest.raises(Conflict):
        taker.create("cards", "a", {"n": 2}, "op-a2", ACTOR)
    resume.set()
    slow.join(10)
    again = env.docs.create("cards", "a", {"n": 2}, "op-a3", ACTOR)
    env.docs._finish_purge(ref, marker)
    assert env.docs.get("cards", "a") == again


def test_only_the_ticket_holder_removes_a_record_until_it_stops(env: Env) -> None:
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    ref = env.docs._ref("cards", "a")
    head = env.docs._load_head(ref)
    assert head is not None
    original = env.docs._finish_purge
    env.docs._finish_purge = lambda ref, marker: None  # type: ignore[method-assign]
    env.docs._purge(ref, head)
    env.docs._finish_purge = original  # type: ignore[method-assign]
    marker = env.docs._load_head(ref).latest  # type: ignore[union-attr]
    tickets = f"{env.docs._root}/.removals/cards/a/{marker.digest[:32]}"
    # A remover took the ticket and stopped before deleting anything.
    assert env.docs._take_removal_ticket(tickets) == 1
    env.docs._finish_purge(ref, marker)
    assert env.docs.verify_record("cards", "a") is not None
    # Once its ticket is older than the abandon time, the next remover takes over.
    env.clock.advance(31)
    env.docs._finish_purge(ref, marker)
    assert env.docs.verify_record("cards", "a") is None
    # The stopped remover resuming later finds nothing of its purge left to delete.
    assert env.docs._still_removing(ref, marker, tickets, 1) is False


def test_removers_that_keep_stopping_are_taken_over_in_bounded_work(env: Env) -> None:
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    ref = env.docs._ref("cards", "a")
    head = env.docs._load_head(ref)
    assert head is not None
    original = env.docs._finish_purge
    env.docs._finish_purge = lambda ref, marker: None  # type: ignore[method-assign]
    env.docs._purge(ref, head)
    env.docs._finish_purge = original  # type: ignore[method-assign]
    marker = env.docs._load_head(ref).latest  # type: ignore[union-attr]
    tickets = f"{env.docs._root}/.removals/cards/a/{marker.digest[:32]}"
    # Many removers in turn take over the abandoned ticket and stop too.
    for number in range(1, 65):
        assert env.docs._take_removal_ticket(tickets) == number
        env.clock.advance(env.docs._limits.purge_abandon_s + 1)
    listed: list[str] = []
    reads: list[str] = []
    files = env.docs._files
    real_list, real_read = files.list_paths, files.read_json
    files.list_paths = lambda path: listed.append(path) or real_list(path)  # type: ignore[method-assign]
    files.read_json = lambda path: reads.append(path) or real_read(path)  # type: ignore[method-assign]

    env.docs._finish_purge(ref, marker)

    assert env.docs.verify_record("cards", "a") is None
    # The tickets are never listed, and finding the newest reads a few of them, not all 64.
    assert not any(path.startswith(tickets) for path in listed)
    # Two lookups (take a ticket, then check it is still the newest), each O(log tickets).
    assert sum(path.startswith(tickets) for path in reads) <= 2 * (2 * (65).bit_length() + 1) + 2


def test_a_record_being_removed_makes_its_writers_wait(env: Env) -> None:
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    ref = env.docs._ref("cards", "a")
    head = env.docs._load_head(ref)
    assert head is not None
    # A removal that wrote its purge revision and stopped before deleting the record.
    original = env.docs._finish_purge
    env.docs._finish_purge = lambda ref, marker: None  # type: ignore[method-assign]
    env.docs._purge(ref, head)
    env.docs._finish_purge = original  # type: ignore[method-assign]
    assert env.docs.get("cards", "a") is None
    with pytest.raises(Conflict) as error:
        env.docs.create("cards", "a", {"n": 2}, "op-a2", ACTOR)
    assert error.value.reason == "concurrent_write"
    env.clock.advance(31)
    assert env.docs.create("cards", "a", {"n": 2}, "op-a2", ACTOR).value == {"n": 2}


def test_a_store_that_cannot_delete_keeps_the_fixed_capacity(env: Env) -> None:
    class CreateOnly(GadgetFiles):
        def __init__(self, inner: GadgetFiles) -> None:
            self.inner = inner

        def read_json(self, path: str) -> dict:
            return self.inner.read_json(path)

        def create_json(self, path: str, value: dict) -> None:
            self.inner.create_json(path, value)

        def list_paths(self, path: str) -> list[str]:
            return self.inner.list_paths(path)

        def mkdirs(self, path: str) -> None:
            self.inner.mkdirs(path)

    docs = ImmutableDocumentStore(CreateOnly(env.files), "data", limits=SMALL, clock=env.clock)
    a = docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    docs.create("cards", "b", {"n": 1}, "op-b", ACTOR)
    docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    with pytest.raises(CapacityExceeded):
        docs.create("cards", "c", {"n": 1}, "op-c", ACTOR)
    assert (env.root / "cards" / "a").exists()


class CountingFiles(GadgetFiles):
    """Wraps a scope and counts the entries each read examines."""

    def __init__(self, inner: GadgetFiles) -> None:
        self.inner = inner
        self.supports_delete = inner.supports_delete
        self.examined = 0
        self.reads = 0
        self.full_listings: list[str] = []

    def exclusive(self, name: str):  # type: ignore[override]
        # The wrapped store's real lock, so tests exercise its exclusion.
        return self.inner.exclusive(name)

    def reset(self) -> None:
        self.examined = 0
        self.reads = 0
        self.full_listings = []

    def read_json(self, path: str) -> dict:
        self.examined += 1
        self.reads += 1
        return self.inner.read_json(path)

    def create_json(self, path: str, value: dict) -> None:
        self.inner.create_json(path, value)

    def list_paths(self, path: str) -> list[str]:
        names = self.inner.list_paths(path)
        self.full_listings.append(path)
        self.examined += len(names)
        return names

    def list_page(self, path: str, *, after: str | None, limit: int) -> tuple[list[str], bool]:
        names, more = self.inner.list_page(path, after=after, limit=limit)
        # The local store reads the whole directory to seek, so count every entry in it,
        # not only the page returned.
        self.examined += len(self.inner.list_paths(path))
        self.full_listings.append(path)
        return names, more

    def mkdirs(self, path: str) -> None:
        self.inner.mkdirs(path)

    def delete_tree(self, path: str) -> None:
        self.inner.delete_tree(path)


WIDE = StoreLimits(
    max_records_per_collection=1000,
    max_list_entries=1000,
    max_scan_per_list=20,
    idempotency_window=2,
    tombstone_retention_s=60,
    purge_abandon_s=30,
)


def _counting(
    tmp_path: Path, limits: StoreLimits = WIDE
) -> tuple[CountingFiles, ImmutableDocumentStore, Clock]:
    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    files = CountingFiles(store.gadget_files(store.create(ACTOR, "Board").id))
    clock = Clock()
    return files, ImmutableDocumentStore(files, "data", limits=limits, clock=clock), clock


def test_a_page_examines_a_bounded_number_of_records_however_many_were_deleted(
    tmp_path: Path,
) -> None:
    files, docs, _clock = _counting(tmp_path)
    for n in range(200):
        version = docs.create("cards", f"c{n:03d}", {"n": n}, f"create-{n}", ACTOR).version
        if n < 190:
            docs.delete("cards", f"c{n:03d}", version, f"delete-{n}", ACTOR)
    files.reset()

    page = docs.list("cards", limit=5)

    # Twenty order entries looked at (the scan budget), each with its record's head.
    assert page.items == () and page.next_cursor is not None
    assert not any(path.endswith("/cards") for path in files.full_listings)
    assert files.examined <= WIDE.max_scan_per_list * 6
    # Following the cursor reaches the live records.
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = docs.list("cards", limit=5, cursor=cursor)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [f"c{n:03d}" for n in range(190, 200)]


def test_a_page_costs_the_same_however_large_the_collection_grows(tmp_path: Path) -> None:
    files, docs, _clock = _counting(tmp_path)
    costs: dict[int, int] = {}
    for n in range(600):
        docs.create("cards", f"c{n:03d}", {"n": n}, f"create-{n}", ACTOR)
        if n + 1 in (30, 300, 600):
            files.reset()
            first = docs.list("cards", limit=10)
            later = docs.list("cards", limit=10, cursor=first.next_cursor)
            assert [item.id for item in later.items] == [f"c{k:03d}" for k in range(10, 20)]
            costs[n + 1] = files.examined
            # Pages list only each record's own newest segment, never the collection's
            # directory or its order.
            assert all(
                path.startswith("data/cards/c") and ".order" not in path
                for path in files.full_listings
            ), files.full_listings
    assert costs[600] == costs[30], costs


def test_a_late_create_retry_after_a_delete_is_answered_without_reading_the_history(
    tmp_path: Path,
) -> None:
    files, docs, _clock = _counting(tmp_path)
    version = docs.create("cards", "c1", {"n": 0}, "create-once", ACTOR).version
    for n in range(1, 40):
        version = docs.replace("cards", "c1", {"n": n}, version, f"replace-{n}", ACTOR).version
    docs.delete("cards", "c1", version, "delete-1", ACTOR)
    files.reset()

    # A retry of the first create, long after: answered, and the record stays deleted.
    replay = docs.create("cards", "c1", {"n": 0}, "create-once", ACTOR)

    assert replay.value == {"n": 0}
    assert docs.get("cards", "c1") is None
    # Of its 41 revisions only the newest few, the one the index names and a probe for a
    # second segment are read.
    assert files.reads <= 10


@pytest.mark.parametrize("retried_at_once", [False, True])
def test_a_create_whose_index_write_stopped_still_cannot_come_back_after_a_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retried_at_once: bool
) -> None:
    _files, docs, _clock = _counting(tmp_path)
    real_index = ImmutableDocumentStore._index_create
    stopped = {"once": True}

    def stop_once(self, ref, operation_id, number):  # type: ignore[no-untyped-def]
        if stopped["once"] and operation_id == "create-once":
            stopped["once"] = False
            raise OSError("process stopped after the revision committed")
        return real_index(self, ref, operation_id, number)

    monkeypatch.setattr(ImmutableDocumentStore, "_index_create", stop_once)
    with pytest.raises(OSError):
        docs.create("cards", "c1", {"n": 0}, "create-once", ACTOR)
    version = docs.get("cards", "c1").version  # type: ignore[union-attr]
    if retried_at_once:
        # A retry answered right away notes the create too.
        assert docs.create("cards", "c1", {"n": 0}, "create-once", ACTOR).value == {"n": 0}
    # Push the create far past the replay window, then delete the record.
    for n in range(1, 40):
        version = docs.replace("cards", "c1", {"n": n}, version, f"replace-{n}", ACTOR).version
    docs.delete("cards", "c1", version, "delete-1", ACTOR)

    replay = docs.create("cards", "c1", {"n": 0}, "create-once", ACTOR)

    assert replay.value == {"n": 0}
    assert docs.get("cards", "c1") is None


def test_a_create_probes_a_bounded_run_of_capacity_slots(tmp_path: Path) -> None:
    files, docs, _clock = _counting(tmp_path)
    for n in range(300):
        docs.create("cards", f"c{n:03d}", {"n": n}, f"create-{n}", ACTOR)
    files.reset()

    docs.create("cards", "fresh", {"n": 1}, "create-fresh", ACTOR)

    assert not any(path.endswith("/cards") for path in files.full_listings)
    # 64 capacity slots, plus about 2*log2(300) reads to find the end of the collection's
    # order for its new entry.
    assert files.examined <= 100


@pytest.mark.parametrize("history", [1, 400])
def test_a_full_collection_checks_live_claimants_without_reading_their_history(
    tmp_path: Path, history: int
) -> None:
    # Aged live records, one with a long history: deciding a claimant was written never
    # lists its revisions, so the work a full create does doesn't grow with them.
    limits = StoreLimits(
        max_records_per_collection=4,
        max_list_entries=64,
        max_scan_per_list=10,
        revisions_per_segment=4,
        tombstone_retention_s=60,
        purge_abandon_s=30,
    )
    files, docs, clock = _counting(tmp_path, limits)
    for n in range(4):
        current = docs.create("cards", f"c{n}", {"n": 0}, f"create-{n}", ACTOR)
    for edit in range(history - 1):
        current = docs.replace("cards", "c3", {"n": edit + 1}, current.version, f"e-{edit}", ACTOR)
    clock.advance(61)
    files.reset()

    with pytest.raises(CapacityExceeded):
        docs.create("cards", "fresh", {"n": 1}, "create-fresh", ACTOR)

    # Fixed apart from a log-sized head lookup: 400 revisions cost about what 1 does.
    assert files.examined <= 60


def test_reclaiming_capacity_looks_at_a_bounded_number_of_records(tmp_path: Path) -> None:
    limits = StoreLimits(
        max_records_per_collection=64,
        max_list_entries=64,
        max_scan_per_list=10,
        idempotency_window=2,
        tombstone_retention_s=60,
        purge_abandon_s=30,
        max_reclaim_per_create=2,
    )
    files, docs, clock = _counting(tmp_path, limits)
    created = 0
    with pytest.raises(CapacityExceeded):
        while True:
            docs.create("cards", f"c{created:03d}", {"n": created}, f"create-{created}", ACTOR)
            created += 1
    for n in range(created):
        record = docs.get("cards", f"c{n:03d}")
        assert record is not None
        docs.delete("cards", f"c{n:03d}", record.version, f"delete-{n}", ACTOR)
    clock.advance(61)
    files.reset()

    docs.create("cards", "fresh", {"n": 1}, "create-fresh", ACTOR)

    assert docs.get("cards", "fresh") is not None
    assert not any(path.endswith("/cards") for path in files.full_listings)
    # Ten candidates looked at, two removed (each probing at most 64 slots for its claim),
    # and the new slot claimed: a fixed budget, whatever the collection holds.
    assert files.examined <= 10 * 6 + 2 * 90 + 70
