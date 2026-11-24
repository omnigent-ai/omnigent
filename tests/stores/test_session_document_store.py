"""Shared membership against migrated SQL, with competing independent stores."""

from __future__ import annotations

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, update

from omnigent.db import compression
from omnigent.db.db_models import SqlConversationMetadata, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.stores import session_document_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_document_store import (
    MAX_DOCUMENTS_PER_SESSION,
    SessionDocumentLimitError,
    SessionDocumentStore,
    UnreadableSessionDocumentsError,
)

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER = "5d29bee4350489d66feafecfebd94a97"
URL = "https://docs.google.com/document/d/example/edit"


@pytest.fixture
def documents(
    db_uri: str, conversation_store: SqlAlchemyConversationStore
) -> SessionDocumentStore:
    for identifier in (SESSION, OTHER):
        conversation_store.create_conversation(conversation_id=identifier)
    conversation_store.set_labels(SESSION, {"unrelated": "keep"})
    return SessionDocumentStore(db_uri)


def _stored(db_uri: str) -> str | None:
    with get_or_create_engine(db_uri).connect() as conn:
        return conn.execute(
            select(SqlConversationMetadata.session_documents).where(
                SqlConversationMetadata.workspace_id == 0, SqlConversationMetadata.id == SESSION
            )
        ).scalar_one()


def test_durable_idempotent_membership_and_session_isolation(
    documents, db_uri, conversation_store
) -> None:
    assert documents.read(SESSION) == []
    assert _stored(db_uri) is None
    original = URL + "?usp=sharing#tab=t.one"
    first = documents.add(SESSION, original)
    assert first[0].document_url == original
    assert first[0].added_at > 1_000_000_000_000_000
    documents.add(SESSION, URL.replace("example", "other"))
    before = _stored(db_uri)
    assert documents.add(SESSION, URL + "?tab=t.two")[-1] == first[0]
    assert _stored(db_uri) == before
    assert SessionDocumentStore(db_uri).read(SESSION) == documents.read(SESSION)
    assert documents.read(OTHER) == []
    assert documents.read("f" * 32) is None
    assert documents.add("f" * 32, URL) is None
    assert documents.remove("f" * 32, "example") is None
    assert conversation_store.get_conversation(SESSION).labels == {"unrelated": "keep"}
    assert len(documents.remove(SESSION, "example")) == 1
    before = _stored(db_uri)
    assert len(documents.remove(SESSION, "example")) == 1
    assert _stored(db_uri) == before


def test_simultaneous_adds_and_removes_preserve_unrelated_entries(documents, db_uri) -> None:
    urls = [URL.replace("example", f"doc_{index}") for index in range(12)]
    barrier = Barrier(len(urls))

    def add(url: str):
        store = SessionDocumentStore(db_uri)
        barrier.wait(timeout=10)
        return store.add(SESSION, url)

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        assert all(pool.map(add, urls))
    assert len(documents.read(SESSION)) == len(urls)
    barrier = Barrier(8)

    def mutate(index: int):
        store = SessionDocumentStore(db_uri)
        barrier.wait(timeout=10)
        return (
            store.remove(SESSION, f"doc_{index}")
            if index < 4
            else store.add(SESSION, URL.replace("example", f"new_{index}"))
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(mutate, range(8)))
    assert {entry.document_url for entry in documents.read(SESSION)} == {
        *urls[4:],
        *(URL.replace("example", f"new_{index}") for index in range(4, 8)),
    }


def test_simultaneous_tab_adds_deduplicate_once(documents, db_uri) -> None:
    barrier = Barrier(6)

    def add(index: int):
        barrier.wait(timeout=10)
        return SessionDocumentStore(db_uri).add(SESSION, URL + f"?tab=t.{index}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(add, range(6)))
    assert all(result == results[0] for result in results)
    assert len(documents.read(SESSION)) == 1


def test_capacity_conflicts_roll_back_without_evicting_entries(documents, db_uri) -> None:
    for index in range(MAX_DOCUMENTS_PER_SESSION):
        documents.add(SESSION, URL.replace("example", f"doc_{index}"))
    before = _stored(db_uri)
    with pytest.raises(SessionDocumentLimitError):
        documents.add(SESSION, URL)
    assert _stored(db_uri) == before
    assert (
        len(documents.add(SESSION, URL.replace("example", "doc_0") + "?tab=t.other"))
        == MAX_DOCUMENTS_PER_SESSION
    )
    documents.remove(SESSION, "doc_0")
    assert len(documents.add(SESSION, URL)) == MAX_DOCUMENTS_PER_SESSION


def test_competing_adds_at_capacity_commit_only_available_slots(
    documents, db_uri, monkeypatch
) -> None:
    monkeypatch.setattr(session_document_store, "MAX_DOCUMENTS_PER_SESSION", 2)
    documents.add(SESSION, URL)
    barrier = Barrier(4)

    def add(index: int) -> bool:
        barrier.wait(timeout=10)
        try:
            SessionDocumentStore(db_uri).add(SESSION, URL.replace("example", f"doc_{index}"))
            return True
        except SessionDocumentLimitError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(add, range(4)))
    assert results.count(True) == 1
    assert len(documents.read(SESSION)) == 2
    assert any(entry.document_url == URL for entry in documents.read(SESSION))


def test_real_storage_byte_budget_rejects_atomically(documents, db_uri) -> None:
    for _ in range(MAX_DOCUMENTS_PER_SESSION):
        # Independent random identities are deliberately hard to compress.
        url = URL.replace("example", secrets.token_urlsafe(1400))
        before = _stored(db_uri)
        try:
            documents.add(SESSION, url)
        except SessionDocumentLimitError:
            assert _stored(db_uri) == before
            assert len(documents.read(SESSION)) < MAX_DOCUMENTS_PER_SESSION
            assert len(compression.encode(before)) <= 16_000
            break
    else:
        pytest.fail("The compressed byte budget was not enforced")


def test_raw_size_budget_rejects_atomically(documents, db_uri, monkeypatch) -> None:
    documents.add(SESSION, URL)
    before = _stored(db_uri)
    monkeypatch.setattr(session_document_store, "_MAX_JSON_BYTES", len(before.encode()))
    with pytest.raises(SessionDocumentLimitError):
        documents.add(SESSION, URL.replace("example", "other"))
    assert _stored(db_uri) == before


def test_unreadable_state_is_preserved(documents, db_uri) -> None:
    with get_or_create_engine(db_uri).begin() as conn:
        conn.execute(
            update(SqlConversationMetadata)
            .where(SqlConversationMetadata.id == SESSION)
            .values(session_documents='{"future":true}')
        )
    for action in [
        lambda: documents.read(SESSION),
        lambda: documents.add(SESSION, URL),
        lambda: documents.remove(SESSION, "example"),
    ]:
        with pytest.raises(UnreadableSessionDocumentsError):
            action()
    assert _stored(db_uri) == '{"future":true}'


def test_workspace_isolation_even_for_identical_session_ids(documents, db_uri) -> None:
    first = documents.add(SESSION, URL)
    with workspace_scope(17):
        conversations = SqlAlchemyConversationStore(db_uri)
        conversations.create_conversation(conversation_id=SESSION)
        assert documents.read(SESSION) == []
        documents.add(SESSION, URL.replace("example", "workspace_other"))
        documents.remove(SESSION, "example")
        assert len(documents.read(SESSION)) == 1
    assert documents.read(SESSION) == first


def test_forks_start_empty_and_deletion_removes_membership(documents, conversation_store) -> None:
    first = documents.add(SESSION, URL)
    fork = conversation_store.fork_conversation(SESSION)
    assert documents.read(fork.id) == []
    documents.add(fork.id, URL.replace("example", "fork"))
    documents.remove(fork.id, "example")
    assert documents.read(SESSION) == first
    assert asyncio.run(conversation_store.delete_conversation(fork.id))
    assert documents.read(fork.id) is None
    assert documents.read(SESSION) == first


def test_server_timestamp_ties_use_document_id_order(documents, monkeypatch) -> None:
    monkeypatch.setattr(session_document_store, "now_epoch_us", lambda: 100)
    urls = [URL.replace("example", name) for name in ("a", "B", "C")]
    for url in urls:
        documents.add(SESSION, url)
    entries = documents.read(SESSION)
    assert [entry.document_url for entry in entries] == [urls[1], urls[2], urls[0]]
    assert [entry.added_at for entry in entries] == [100, 100, 100]
    assert documents.add(SESSION, urls[0] + "?tab=t.other") == entries
