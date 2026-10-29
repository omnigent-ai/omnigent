"""Tests for :class:`SessionBrowserTabStore` against a migrated SQLite database."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_browser_tab_store import (
    MAX_BROWSER_TABS,
    BrowserTab,
    BrowserTabsConflictError,
    SessionBrowserTabStore,
    UnreadableBrowserTabsError,
)

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER = "5d29bee4350489d66feafecfebd94a97"
ALICE = "alice@example.com"
BOB = "bob@example.com"
DOCS = BrowserTab(id="agent-browser", url="https://docs.example/guide", title="Guide")
MAIL = BrowserTab(id="2b1c", url="https://mail.example/inbox")
BLANK = BrowserTab(id="9f0e")


@pytest.fixture()
def conversations(db_uri: str) -> SqlAlchemyConversationStore:
    conversations = SqlAlchemyConversationStore(db_uri)
    for session in (SESSION, OTHER):
        conversations.create_conversation(conversation_id=session)
    return conversations


@pytest.fixture()
def store(db_uri: str, conversations: SqlAlchemyConversationStore) -> SessionBrowserTabStore:
    return SessionBrowserTabStore(db_uri)


def test_reads_no_tabs_at_revision_zero_before_the_first_save(
    store: SessionBrowserTabStore,
) -> None:
    stored = store.read(SESSION, ALICE)
    assert (stored.revision, stored.tabs) == (0, ())


def test_saves_tabs_in_order_and_bumps_the_revision(store: SessionBrowserTabStore) -> None:
    first = store.replace(SESSION, ALICE, (DOCS, BLANK), base_revision=0)
    second = store.replace(SESSION, ALICE, (MAIL, DOCS), base_revision=1)

    assert (first.revision, second.revision) == (1, 2)
    assert store.read(SESSION, ALICE) == second
    assert second.tabs == (MAIL, DOCS)


def test_unchanged_tabs_keep_the_revision(store: SessionBrowserTabStore) -> None:
    store.replace(SESSION, ALICE, (DOCS,), base_revision=0)
    assert store.replace(SESSION, ALICE, (DOCS,), base_revision=1).revision == 1


@pytest.mark.parametrize("base_revision", [0, 2])
def test_rejects_a_write_based_on_another_revision(
    store: SessionBrowserTabStore, base_revision: int
) -> None:
    saved = store.replace(SESSION, ALICE, (DOCS,), base_revision=0)

    with pytest.raises(BrowserTabsConflictError) as raised:
        store.replace(SESSION, ALICE, (MAIL,), base_revision=base_revision)

    assert raised.value.current == saved
    assert store.read(SESSION, ALICE) == saved


def test_rejects_a_first_write_that_assumes_saved_tabs(store: SessionBrowserTabStore) -> None:
    with pytest.raises(BrowserTabsConflictError) as raised:
        store.replace(SESSION, ALICE, (DOCS,), base_revision=3)
    assert raised.value.current.revision == 0


def test_keeps_each_user_and_session_separate(store: SessionBrowserTabStore) -> None:
    store.replace(SESSION, ALICE, (DOCS,), base_revision=0)
    store.replace(SESSION, BOB, (MAIL,), base_revision=0)
    store.replace(OTHER, ALICE, (BLANK,), base_revision=0)

    assert store.read(SESSION, ALICE).tabs == (DOCS,)
    assert store.read(SESSION, BOB).tabs == (MAIL,)
    assert store.read(OTHER, ALICE).tabs == (BLANK,)


@pytest.mark.parametrize(
    ("tabs", "message"),
    [
        (tuple(BrowserTab(id=f"t{index}") for index in range(MAX_BROWSER_TABS + 1)), "At most"),
        ((BLANK, BLANK), "Duplicate tab id"),
        ((BrowserTab(id=""),), "Tab ids"),
        ((BrowserTab(id="x" * 65),), "Tab ids"),
        ((BrowserTab(id="a", url="file:///etc/passwd"),), "http or https"),
        ((BrowserTab(id="a", url="https://x/" + "a" * 8192),), "at most 8192"),
        ((BrowserTab(id="a", title="t" * 513),), "at most 512"),
    ],
)
def test_rejects_tabs_over_the_limits(
    store: SessionBrowserTabStore, tabs: tuple[BrowserTab, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        store.replace(SESSION, ALICE, tabs, base_revision=0)
    assert store.read(SESSION, ALICE).revision == 0


def test_an_unreadable_document_raises(db_uri: str, store: SessionBrowserTabStore) -> None:
    store.replace(SESSION, ALICE, (DOCS,), base_revision=0)
    with sa.create_engine(db_uri).begin() as conn:
        conn.execute(sa.text("UPDATE session_browser_tabs SET tabs = :tabs"), {"tabs": b"{"})

    with pytest.raises(UnreadableBrowserTabsError):
        store.read(SESSION, ALICE)


async def test_deleting_the_session_drops_its_tabs(
    store: SessionBrowserTabStore, conversations: SqlAlchemyConversationStore
) -> None:
    store.replace(SESSION, ALICE, (DOCS,), base_revision=0)
    store.replace(SESSION, BOB, (MAIL,), base_revision=0)
    store.replace(OTHER, ALICE, (BLANK,), base_revision=0)

    assert await conversations.delete_conversation(SESSION)

    assert store.read(SESSION, ALICE).revision == 0
    assert store.read(SESSION, BOB).revision == 0
    assert store.read(OTHER, ALICE).tabs == (BLANK,)
