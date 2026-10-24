"""Tests for :class:`SessionPullRequestStore` against a migrated SQLite database."""

from __future__ import annotations

from dataclasses import replace

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_pull_request_store import PullRequestReport, SessionPullRequestStore

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER = "5d29bee4350489d66feafecfebd94a97"
A = "https://github.com/example/one/pull/42"
B = "https://github.com/example/two/pull/7"


def _report(url: str, **overrides: object) -> PullRequestReport:
    repository, number = url.removeprefix("https://github.com/").split("/pull/")
    fields: dict[str, object] = {
        "url": url,
        "provider": "github",
        "host": "github.com",
        "repository": repository,
        "number": int(number),
        "relationship": "worked_on",
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
    }
    return PullRequestReport(**{**fields, **overrides})  # type: ignore[arg-type]


@pytest.fixture()
def store(db_uri: str) -> SessionPullRequestStore:
    return SessionPullRequestStore(db_uri)


def test_reports_merge_in_any_order(store: SessionPullRequestStore) -> None:
    """Re-reporting a host registry converges: strongest relationship, widest window."""
    created = _report(A, relationship="created", first_seen_at=50.0, last_seen_at=60.0)
    later = _report(A, relationship="inferred", first_seen_at=70.0, last_seen_at=90.0)
    assert store.record(SESSION, [later])
    assert store.record(SESSION, [created])
    assert not store.record(SESSION, [later, created])

    [entry] = store.list_for_session(SESSION)
    assert entry.relationship == "created"
    assert (entry.first_seen_at, entry.last_seen_at) == (50.0, 90.0)


def test_latest_reported_title_wins_and_a_failed_lookup_keeps_it(
    store: SessionPullRequestStore,
) -> None:
    store.record(SESSION, [_report(A, title="First")])
    assert store.record(SESSION, [_report(A, title="Renamed")])
    # A host whose title lookup failed reports no title.
    assert not store.record(SESSION, [_report(A, title=None)])
    assert store.list_for_session(SESSION)[0].title == "Renamed"


def test_unlink_hides_pr_until_the_user_links_it_again(store: SessionPullRequestStore) -> None:
    store.record(SESSION, [_report(A), _report(B)])
    assert store.remove(SESSION, A)
    assert not store.remove(SESSION, A)
    assert [entry.url for entry in store.list_for_session(SESSION)] == [B]
    assert store.removed_urls(SESSION) == [A]

    # A replayed observation must not undo the user's unlink.
    assert not store.record(SESSION, [_report(A, relationship="created", last_seen_at=500.0)])
    assert not store.update_state(
        SESSION, A, state="OPEN", is_draft=False, title=None, checked_at=1.0
    )
    assert [entry.url for entry in store.list_for_session(SESSION)] == [B]

    store.attach(SESSION, _report(A, last_seen_at=600.0))
    assert store.removed_urls(SESSION) == []
    [first, _] = store.list_for_session(SESSION)
    assert (first.url, first.relationship) == (A, "attached")


def test_state_keeps_newest_fetch_and_refreshes_title(store: SessionPullRequestStore) -> None:
    store.record(SESSION, [_report(A, title="Old")])
    assert store.update_state(
        SESSION, A, state="OPEN", is_draft=True, title="New " + "x" * 2000, checked_at=20.0
    )
    assert not store.update_state(
        SESSION, A, state="CLOSED", is_draft=False, title=None, checked_at=10.0
    )
    # A state the providers never report is not stored.
    assert not store.update_state(
        SESSION, A, state="LOCKED", is_draft=False, title=None, checked_at=30.0
    )
    [entry] = store.list_for_session(SESSION)
    assert (entry.state, entry.is_draft, entry.state_checked_at) == ("OPEN", True, 20.0)
    assert entry.title is not None
    assert entry.title.startswith("New x") and len(entry.title) == 1024


def test_display_order_and_primary_pr(store: SessionPullRequestStore) -> None:
    """Explicit PRs come first, most recent first; inferred ones follow, oldest first."""
    inferred_old = "https://github.com/example/three/pull/1"
    inferred_new = "https://github.com/example/three/pull/2"
    store.record(
        SESSION,
        [
            _report(inferred_new, relationship="inferred", first_seen_at=300.0),
            _report(inferred_old, relationship="inferred", first_seen_at=200.0),
            _report(A, last_seen_at=100.0),
            _report(B, last_seen_at=400.0),
        ],
    )
    store.record(OTHER, [_report(A, relationship="inferred")])
    assert [entry.url for entry in store.list_for_session(SESSION)] == [
        B,
        A,
        inferred_old,
        inferred_new,
    ]
    primary = store.primary_for_sessions([SESSION, OTHER, "f" * 32])
    assert {session: entry.url for session, entry in primary.items()} == {SESSION: B, OTHER: A}

    store.remove(SESSION, B)
    assert store.primary_for_sessions([SESSION])[SESSION].url == A


def test_long_titles_are_truncated(store: SessionPullRequestStore) -> None:
    store.record(SESSION, [replace(_report(A), title="x" * 2000)])
    assert len(store.list_for_session(SESSION)[0].title or "") == 1024


async def test_deleting_the_session_deletes_its_pull_requests(db_uri: str) -> None:
    conversation_store = SqlAlchemyConversationStore(db_uri)
    store = SessionPullRequestStore(db_uri)
    conv = conversation_store.create_conversation()
    store.record(conv.id, [_report(A)])
    store.record(OTHER, [_report(A)])

    assert await conversation_store.delete_conversation(conv.id)
    assert store.list_for_session(conv.id) == []
    assert [entry.url for entry in store.list_for_session(OTHER)] == [A]
