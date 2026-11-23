"""ImmutableDocumentStore: segments, capacity reclaim, idempotency and fail-closed reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnigent.gadgets.documents import ImmutableDocumentStore, StoreLimits
from omnigent.stores.gadget_store import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    GadgetFiles,
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

    env.write("c2", 5)
    (env.root / "cards" / "c2" / "00000002.json").unlink()
    with pytest.raises(CorruptData):
        env.docs.get("cards", "c2")


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
    assert [item.id for item in env.docs.list("cards").items] == ["b", "c"]
    # Churn never exhausts the collection: delete and create again, many times over.
    current = created
    for n in range(6):
        env.docs.delete("cards", current.id, current.version, f"del-{n}", ACTOR)
        env.clock.advance(61)
        current = env.docs.create("cards", f"n{n}", {"n": n}, f"op-n{n}", ACTOR)
    assert sorted(item.id for item in env.docs.list("cards").items) == ["b", "n5"]


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


def test_a_record_being_removed_makes_its_writers_wait(env: Env) -> None:
    a = env.docs.create("cards", "a", {"n": 1}, "op-a", ACTOR)
    env.docs.delete("cards", "a", a.version, "del-a", ACTOR)
    env.clock.advance(61)
    ref = env.docs._ref("cards", "a")
    head = env.docs._load_head(ref)
    assert head is not None
    # A removal that wrote its purge revision and stopped before deleting the record.
    original = env.docs._finish_purge
    env.docs._finish_purge = lambda ref: None  # type: ignore[method-assign]
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
