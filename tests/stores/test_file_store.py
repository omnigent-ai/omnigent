"""Tests for SqlAlchemyFileStore."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from omnigent.db.utils import NamedManagedSessionMaker
from omnigent.stores.file_store import sqlalchemy_store
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

# Files are always listed per session, so pagination/ordering tests
# scope their fixtures to a single session id.
_SID = "94c349190e241f85a984b3df8f129696"
_PROJECT = "7d3b1f4e2a9c4b6d8e0f1a2b3c4d5e6f"
_OTHER_PROJECT = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


@pytest.fixture()
def file_store(db_uri: str) -> SqlAlchemyFileStore:
    return SqlAlchemyFileStore(db_uri)


def test_create_and_get(file_store: SqlAlchemyFileStore) -> None:
    f = file_store.create(filename="data.csv", bytes=1024)
    assert len(f.id) == 32
    assert f.filename == "data.csv"
    assert f.bytes == 1024

    fetched = file_store.get(f.id)
    assert fetched is not None
    assert fetched.filename == "data.csv"


def test_get_nonexistent(file_store: SqlAlchemyFileStore) -> None:
    assert file_store.get("0017bdafc0b60ad65aff5ea44c8e294d") is None


def test_create_with_content_type(file_store: SqlAlchemyFileStore) -> None:
    f = file_store.create(
        filename="img.png",
        bytes=2048,
        content_type="image/png",
    )
    assert f.content_type == "image/png"


def test_create_with_caller_chosen_id(file_store: SqlAlchemyFileStore) -> None:
    """A caller-chosen file_id is stored verbatim (fork copy pre-allocation)."""
    chosen = "5b2f8f0f36cd08e10b2f3a89ab6a41d7"
    f = file_store.create(
        filename="copy.png",
        bytes=2048,
        content_type="image/png",
        session_id=_SID,
        file_id=chosen,
    )
    assert f.id == chosen

    fetched = file_store.get(chosen, session_id=_SID)
    assert fetched is not None
    assert fetched.filename == "copy.png"


def test_source_metadata_round_trips(file_store: SqlAlchemyFileStore) -> None:
    """source_metadata is persisted as JSON and read back as a dict."""
    f = file_store.create(
        filename="shot.webp",
        bytes=2048,
        content_type="image/webp",
        source_metadata={"width": 6000, "height": 4000},
    )
    assert f.source_metadata == {"width": 6000, "height": 4000}

    fetched = file_store.get(f.id)
    assert fetched is not None
    assert fetched.source_metadata == {"width": 6000, "height": 4000}


def test_source_metadata_defaults_none(file_store: SqlAlchemyFileStore) -> None:
    """A file created without source_metadata reads back None (not {})."""
    f = file_store.create(filename="plain.txt", bytes=10)
    assert f.source_metadata is None
    fetched = file_store.get(f.id)
    assert fetched is not None
    assert fetched.source_metadata is None


def test_delete(file_store: SqlAlchemyFileStore) -> None:
    f = file_store.create(filename="temp.txt", bytes=10)
    assert file_store.delete(f.id) is True
    assert file_store.get(f.id) is None
    assert file_store.delete(f.id) is False


def test_list_pagination(file_store: SqlAlchemyFileStore) -> None:
    for i in range(4):
        file_store.create(filename=f"f{i}.txt", bytes=i, session_id=_SID)

    page1 = file_store.list(session_id=_SID, limit=2)
    assert len(page1.data) == 2
    assert page1.has_more is True

    page2 = file_store.list(session_id=_SID, limit=2, after=page1.last_id)
    assert len(page2.data) == 2
    assert page2.has_more is False


def test_list_order_asc(file_store: SqlAlchemyFileStore) -> None:
    for i in range(3):
        file_store.create(filename=f"f{i}.txt", bytes=i, session_id=_SID)
    page_desc = file_store.list(session_id=_SID, order="desc")
    page_asc = file_store.list(session_id=_SID, order="asc")
    assert [f.id for f in page_asc.data] == list(reversed([f.id for f in page_desc.data]))


def test_list_asc_with_after_cursor(file_store: SqlAlchemyFileStore) -> None:
    for i in range(5):
        file_store.create(filename=f"f{i}.txt", bytes=i, session_id=_SID)

    page1 = file_store.list(session_id=_SID, limit=2, order="asc")
    page2 = file_store.list(session_id=_SID, limit=2, order="asc", after=page1.last_id)
    page3 = file_store.list(session_id=_SID, limit=2, order="asc", after=page2.last_id)

    all_ids = [f.id for f in page1.data + page2.data + page3.data]
    full_asc = file_store.list(session_id=_SID, limit=100, order="asc")
    assert all_ids == [f.id for f in full_asc.data]


# ── Phase 1c: session-scoped file store methods ─────────────────


def test_create_for_session(file_store: SqlAlchemyFileStore) -> None:
    """Session-scoped create records session_id on the file."""
    f = file_store.create(
        session_id="4e92b5a0c0ee6db3f874f9c4a3f855a5",
        filename="report.pdf",
        bytes=5000,
        content_type="application/pdf",
    )
    assert len(f.id) == 32
    assert f.session_id == "4e92b5a0c0ee6db3f874f9c4a3f855a5"
    assert f.filename == "report.pdf"
    assert f.bytes == 5000


def test_get_for_session_validates_ownership(
    file_store: SqlAlchemyFileStore,
) -> None:
    """get_for_session returns None if file belongs to another session."""
    f = file_store.create(
        session_id="4e92b5a0c0ee6db3f874f9c4a3f855a5",
        filename="owned.txt",
        bytes=10,
    )
    assert file_store.get(f.id, session_id="4e92b5a0c0ee6db3f874f9c4a3f855a5") is not None
    assert file_store.get(f.id, session_id="aef8aa8b6e9cf6eda406cb88cf33708c") is None


def test_list_for_session_scopes_to_session(
    file_store: SqlAlchemyFileStore,
) -> None:
    """list_for_session only returns files owned by that session."""
    file_store.create("a1.txt", 1, session_id="94c349190e241f85a984b3df8f129696")
    file_store.create("a2.txt", 2, session_id="94c349190e241f85a984b3df8f129696")
    file_store.create("b1.txt", 3, session_id="bfcc6c068875253adf2f20bf30a19015")
    file_store.create("global.txt", 4)

    page_a = file_store.list(session_id="94c349190e241f85a984b3df8f129696")
    assert len(page_a.data) == 2
    assert all(f.session_id == "94c349190e241f85a984b3df8f129696" for f in page_a.data)

    page_b = file_store.list(session_id="bfcc6c068875253adf2f20bf30a19015")
    assert len(page_b.data) == 1
    assert page_b.data[0].filename == "b1.txt"


def test_delete_for_session_validates_ownership(
    file_store: SqlAlchemyFileStore,
) -> None:
    """delete_for_session refuses to delete a file from another session."""
    f = file_store.create("mine.txt", 10, session_id="4e92b5a0c0ee6db3f874f9c4a3f855a5")
    assert file_store.delete(f.id, session_id="aef8aa8b6e9cf6eda406cb88cf33708c") is False
    assert file_store.get(f.id) is not None
    assert file_store.delete(f.id, session_id="4e92b5a0c0ee6db3f874f9c4a3f855a5") is True
    assert file_store.get(f.id) is None


def test_delete_all_for_session(
    file_store: SqlAlchemyFileStore,
) -> None:
    """delete_all_for_session removes all session files and returns ids."""
    f1 = file_store.create("a.txt", 1, session_id="4e92b5a0c0ee6db3f874f9c4a3f855a5")
    f2 = file_store.create("b.txt", 2, session_id="4e92b5a0c0ee6db3f874f9c4a3f855a5")
    file_store.create("c.txt", 3, session_id="aef8aa8b6e9cf6eda406cb88cf33708c")
    global_f = file_store.create("global.txt", 4)

    deleted_ids: list[str] = []
    file_store.delete_all_for_session("4e92b5a0c0ee6db3f874f9c4a3f855a5", deleted_ids.extend)
    assert set(deleted_ids) == {f1.id, f2.id}
    assert file_store.get(f1.id) is None
    assert file_store.get(f2.id) is None
    other_page = file_store.list(session_id="aef8aa8b6e9cf6eda406cb88cf33708c")
    assert (
        file_store.get(other_page.data[0].id, session_id="aef8aa8b6e9cf6eda406cb88cf33708c")
        is not None
    )
    assert file_store.get(global_f.id) is not None


# ── session listing ───────────────────────────────────────────────


def test_list_returns_only_the_sessions_own_files(
    file_store: SqlAlchemyFileStore,
) -> None:
    """
    A session's listing leaves out other sessions', project and global files.
    """
    file_store.create("session.txt", 10, session_id="8af356d908005a65f872c246158c6293")
    file_store.create("global.txt", 20)
    file_store.create("plan.md", 25, project_id=_PROJECT)
    file_store.create("other.txt", 30, session_id="1dacb16c401901ed250049177f59e84e")

    page = file_store.list(session_id="8af356d908005a65f872c246158c6293")

    assert [f.filename for f in page.data] == ["session.txt"]


def test_list_excludes_global_files(
    file_store: SqlAlchemyFileStore,
) -> None:
    """list excludes global files, which belong to no session."""
    file_store.create("session.txt", 10, session_id="8af356d908005a65f872c246158c6293")
    file_store.create("global.txt", 20)

    page = file_store.list(session_id="8af356d908005a65f872c246158c6293")
    filenames = {f.filename for f in page.data}
    assert "session.txt" in filenames
    assert "global.txt" not in filenames


# ── list edge cases ───────────────────────────────────────────────


def test_list_empty(file_store: SqlAlchemyFileStore) -> None:
    """list on an empty store returns empty PagedList."""
    page = file_store.list(session_id=_SID)
    assert page.data == []
    assert page.first_id is None
    assert page.last_id is None
    assert page.has_more is False


def test_delete_nonexistent_returns_false(file_store: SqlAlchemyFileStore) -> None:
    """delete returns False for an ID that was never created."""
    result = file_store.delete("e36b9de8c847c2a002707eaf64724dbf")
    assert result is False


# ── blob_key sharing + reference-counted deletion ─────────────────


def test_create_defaults_blob_key_to_id(file_store: SqlAlchemyFileStore) -> None:
    """A normal upload owns its blob: blob_key defaults to the row id."""
    f = file_store.create(filename="own.png", bytes=8, session_id=_SID)
    assert f.blob_key == f.id
    fetched = file_store.get(f.id, session_id=_SID)
    assert fetched is not None
    assert fetched.blob_key == f.id


def test_create_with_shared_blob_key(file_store: SqlAlchemyFileStore) -> None:
    """A fork copy stores a blob_key pointing at the source's blob."""
    source_blob = "aa11bb22cc33dd44ee55ff6677889900"
    fork = file_store.create(
        filename="shared.png",
        bytes=8,
        session_id="1111111111111111e9ed9298fd5a3e21",
        file_id="2222222222222222e9ed9298fd5a3e22",
        blob_key=source_blob,
    )
    assert fork.blob_key == source_blob
    fetched = file_store.get(fork.id, session_id="1111111111111111e9ed9298fd5a3e21")
    assert fetched is not None
    assert fetched.blob_key == source_blob


def test_is_blob_key_orphaned_tracks_shared_references(
    file_store: SqlAlchemyFileStore,
) -> None:
    """A shared blob is orphaned only once its last referencing row is gone."""
    source = file_store.create(filename="img.png", bytes=8, session_id=_SID)
    # A fork copy in another session references the SAME blob.
    fork = file_store.create(
        filename="img.png",
        bytes=8,
        session_id="9999999999999999e9ed9298fd5a3e29",
        blob_key=source.blob_key,
    )

    assert file_store.is_blob_key_orphaned(source.blob_key or source.id) is False

    # Deleting the source row leaves the fork referencing the blob.
    assert file_store.delete(source.id, session_id=_SID) is True
    assert file_store.is_blob_key_orphaned(source.id) is False

    # Only after the fork row is gone is the blob orphaned.
    assert file_store.delete(fork.id, session_id="9999999999999999e9ed9298fd5a3e29") is True
    assert file_store.is_blob_key_orphaned(source.id) is True


def test_is_blob_key_orphaned_true_for_unreferenced_blob(
    file_store: SqlAlchemyFileStore,
) -> None:
    """A blob no row references is reported orphaned."""
    assert file_store.is_blob_key_orphaned("deadbeefdeadbeefdeadbeefdeadbeef") is True


def test_delete_all_for_session_returns_only_orphaned_blobs(
    file_store: SqlAlchemyFileStore,
) -> None:
    """Deleting a source session does not orphan a blob a fork still shares.

    The returned keys are what the caller deletes from the artifact store, so
    a fork's shared blob must be withheld until the fork session is deleted too.
    """
    src_session = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
    fork_session = "aef8aa8b6e9cf6eda406cb88cf33708c"
    shared = file_store.create("shared.png", 8, session_id=src_session)
    solo = file_store.create("solo.png", 8, session_id=src_session)
    file_store.create(
        "shared.png",
        8,
        session_id=fork_session,
        blob_key=shared.blob_key,
    )

    # Deleting the source returns solo's blob (now unreferenced) but NOT the
    # shared blob the fork still points at.
    orphaned: list[str] = []
    file_store.delete_all_for_session(src_session, orphaned.extend)
    assert set(orphaned) == {solo.id}

    # Deleting the fork finally orphans the shared blob.
    orphaned_after: list[str] = []
    file_store.delete_all_for_session(fork_session, orphaned_after.extend)
    assert set(orphaned_after) == {shared.blob_key}


# ── project files ────────────────────────────────────────────────


def test_project_files_are_reachable_only_through_their_project(
    file_store: SqlAlchemyFileStore,
) -> None:
    """A project file is listed, fetched and deleted only through its own project."""
    mine = file_store.create("plan.md", 5, project_id=_PROJECT)
    file_store.create("other.md", 6, project_id=_OTHER_PROJECT)
    file_store.create("notes.md", 7, session_id=_SID)

    assert (mine.project_id, mine.session_id) == (_PROJECT, None)
    assert [f.filename for f in file_store.list_for_project(_PROJECT).data] == ["plan.md"]
    assert file_store.get(mine.id, project_id=_PROJECT) is not None
    assert file_store.get(mine.id, project_id=_OTHER_PROJECT) is None
    assert file_store.get(mine.id, session_id=_SID) is None
    assert file_store.delete(mine.id, project_id=_OTHER_PROJECT) is False
    assert file_store.delete(mine.id, project_id=_PROJECT) is True
    assert file_store.list_for_project(_PROJECT).data == []


def test_create_refuses_a_file_owned_by_a_session_and_a_project(
    file_store: SqlAlchemyFileStore,
) -> None:
    with pytest.raises(ValueError, match="a session or a project"):
        file_store.create("plan.md", 1, session_id=_SID, project_id=_PROJECT)


def test_list_for_project_paginates(
    file_store: SqlAlchemyFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Cursor pages follow ``(created_at, id)`` order across distinct and tied creation times.
    """
    # Fixed creation times: three files tie at 200, between one older and one newer file.
    created_at = iter([100, 200, 200, 200, 300])
    monkeypatch.setattr(
        "omnigent.stores.file_store.sqlalchemy_store.now_epoch", lambda: next(created_at)
    )
    for index in range(5):
        file_store.create(f"f{index}.txt", index, project_id=_PROJECT)
    newest_first = [f.id for f in file_store.list_for_project(_PROJECT, limit=10).data]

    first = file_store.list_for_project(_PROJECT, limit=2)
    second = file_store.list_for_project(_PROJECT, limit=2, after=first.last_id)
    third = file_store.list_for_project(_PROJECT, limit=2, after=second.last_id)
    newer = file_store.list_for_project(_PROJECT, limit=2, before=newest_first[2])
    ascending = file_store.list_for_project(_PROJECT, limit=2, order="asc", after=newest_first[3])

    assert len(newest_first) == 5
    assert [f.id for f in first.data + second.data + third.data] == newest_first
    assert [first.has_more, second.has_more, third.has_more] == [True, True, False]
    assert [f.id for f in newer.data] == newest_first[:2]
    assert [f.id for f in ascending.data] == [newest_first[2], newest_first[1]]


def test_delete_all_for_project_keeps_blobs_other_rows_share(
    file_store: SqlAlchemyFileStore,
) -> None:
    """Deleting a project returns only the blobs no surviving row references."""
    shared = file_store.create("shared.md", 8, project_id=_PROJECT)
    solo = file_store.create("solo.md", 8, project_id=_PROJECT)
    file_store.create("shared.md", 8, session_id=_SID, blob_key=shared.blob_key)
    file_store.create("kept.md", 8, project_id=_OTHER_PROJECT)

    orphaned: list[str] = []
    file_store.delete_all_for_project(_PROJECT, orphaned.extend)
    assert set(orphaned) == {solo.id}
    assert file_store.list_for_project(_PROJECT).data == []
    assert [f.filename for f in file_store.list_for_project(_OTHER_PROJECT).data] == ["kept.md"]


def test_a_failed_batch_leaves_its_rows_after_reporting_the_earlier_batches(
    file_store: SqlAlchemyFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Each committed batch's orphaned blobs reach the caller before a later batch fails.
    """
    monkeypatch.setattr(sqlalchemy_store, "_DELETE_BATCH_SIZE", 2)
    created = {file_store.create(f"f{index}.md", 1, project_id=_PROJECT).id for index in range(3)}
    real_transaction = sqlalchemy_store.run_write_transaction
    delete_batches: list[str] = []

    def fail_the_second_batch(
        session_maker: NamedManagedSessionMaker,
        operation_name: str,
        callback: Callable[[Session], object],
    ) -> object:
        if operation_name == "delete_project_files":
            delete_batches.append(operation_name)
            if len(delete_batches) == 2:
                raise RuntimeError("database unavailable")
        return real_transaction(session_maker, operation_name, callback)

    monkeypatch.setattr(sqlalchemy_store, "run_write_transaction", fail_the_second_batch)
    reported: list[str] = []

    with pytest.raises(RuntimeError, match="database unavailable"):
        file_store.delete_all_for_project(_PROJECT, reported.extend)

    remaining = {f.id for f in file_store.list_for_project(_PROJECT).data}
    assert len(reported) == 2
    assert set(reported) | remaining == created
    assert not set(reported) & remaining


def test_rows_without_a_blob_key_still_reference_their_blob(
    db_uri: str, file_store: SqlAlchemyFileStore
) -> None:
    """Rows written before ``blob_key`` existed keep their bytes under ``id`` and still count."""
    legacy = file_store.create("old.png", 8, session_id=_SID)
    engine = sqlalchemy.create_engine(db_uri)
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text("UPDATE files SET blob_key = NULL"))
    engine.dispose()

    fetched = file_store.get(legacy.id)
    assert fetched is not None and fetched.blob_key is None
    assert file_store.is_blob_key_orphaned(legacy.id) is False
    orphaned: list[str] = []
    file_store.delete_all_for_session(_SID, orphaned.extend)
    assert orphaned == [legacy.id]
