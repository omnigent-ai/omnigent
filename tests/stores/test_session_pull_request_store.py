"""Tests for :class:`SessionPullRequestStore` against a migrated SQLite database."""

from __future__ import annotations

import json
import secrets

import pytest
import sqlalchemy as sa

from omnigent.db import compression
from omnigent.stores import session_pull_request_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_pull_request_store import (
    FetchedState,
    PullRequestReport,
    SessionPullRequestLimitError,
    SessionPullRequestStore,
    UnreadablePullRequestsError,
)

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER = "5d29bee4350489d66feafecfebd94a97"
MISSING = "f" * 32
A = "https://github.com/example/one/pull/42"
B = "https://github.com/example/two/pull/7"
MINUTE = 60.0


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


def _fetch(url: str, state: str = "OPEN", **overrides: object) -> FetchedState:
    number = int(url.rsplit("/", 1)[1])
    return FetchedState(
        **{"url": url, "number": number, "state": state, "is_draft": False, **overrides}
    )  # type: ignore[arg-type]


def _urls(store: SessionPullRequestStore, session: str = SESSION) -> list[str]:
    stored = store.read(session)
    return [] if stored is None else [entry.url for entry in stored.entries]


def _stored_document(db_uri: str, session: str = SESSION) -> bytes | None:
    with sa.create_engine(db_uri).connect() as conn:
        return conn.execute(
            sa.text("SELECT pull_requests FROM omnigent_conversation_metadata WHERE id = :id"),
            {"id": bytes.fromhex(session)},
        ).scalar_one()


@pytest.fixture()
def conversations(db_uri: str) -> SqlAlchemyConversationStore:
    conversations = SqlAlchemyConversationStore(db_uri)
    for session in (SESSION, OTHER):
        conversations.create_conversation(conversation_id=session)
    return conversations


@pytest.fixture()
def store(db_uri: str, conversations: SqlAlchemyConversationStore) -> SessionPullRequestStore:
    return SessionPullRequestStore(db_uri)


def _forbid_writes(store: SessionPullRequestStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def write_session(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("opened a write transaction")

    monkeypatch.setattr(store, "_session_immediate", write_session)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_reports_and_fetches_converge_in_either_order(
    store: SessionPullRequestStore, reverse: bool
) -> None:
    """Strongest relationship, widest seen window, and the newest fetch's state and title."""
    reports = [
        _report(A, relationship="created", first_seen_at=50.0, last_seen_at=60.0),
        _report(A, relationship="inferred", first_seen_at=70.0, last_seen_at=60.0 + 2 * MINUTE),
    ]
    fetches = [
        (_fetch(A, is_draft=True, title="Draft"), 1_000.0),
        (_fetch(A, "MERGED", title="Shipped"), 2_000.0),
    ]
    for report, (fetched, requested_at) in zip(
        reversed(reports) if reverse else reports,
        reversed(fetches) if reverse else fetches,
        strict=True,
    ):
        store.apply_report(SESSION, [report], fetched=fetched, requested_at=requested_at)

    [entry] = store.read(SESSION).entries  # type: ignore[union-attr]
    assert entry.relationship == "created"
    assert (entry.first_seen_at, entry.last_seen_at) == (50.0, 60.0 + 2 * MINUTE)
    assert (entry.state, entry.is_draft, entry.title) == ("MERGED", False, "Shipped")
    assert entry.state_checked_at == 2_000.0
    # A registry title from a request made before that fetch is older than it.
    store.apply_report(SESSION, [_report(A, title="Old")], requested_at=1_500.0)
    assert store.read(SESSION).entries[0].title == "Shipped"  # type: ignore[union-attr]


def test_a_fetch_older_than_an_unwritten_newer_one_is_ignored(
    store: SessionPullRequestStore,
) -> None:
    """The newer OPEN fetch skips its write, but still outranks a delayed CLOSED one."""
    store.apply_report(SESSION, [_report(A)], fetched=_fetch(A), requested_at=1_000.0)
    store.apply_report(SESSION, [_report(A)], fetched=_fetch(A), requested_at=1_004.0)
    store.apply_report(SESSION, [_report(A)], fetched=_fetch(A, "CLOSED"), requested_at=1_002.0)
    [entry] = store.read(SESSION).entries  # type: ignore[union-attr]
    assert (entry.state, entry.state_checked_at) == ("OPEN", 1_000.0)
    # A branch PR the request did not name does not take its state.
    store.apply_report(SESSION, [], fetched=_fetch(A, "MERGED", number=99), requested_at=1_100.0)
    assert store.read(SESSION).entries[0].state == "OPEN"  # type: ignore[union-attr]


def test_repeated_reports_and_fetches_do_not_write(
    store: SessionPullRequestStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.apply_report(
        SESSION,
        [_report(A, title="Title")],
        fetched=_fetch(A, title="Title"),
        requested_at=1_000.0,
    )
    _forbid_writes(store, monkeypatch)

    # Seen again within a minute, and fetched again unchanged within a minute.
    store.apply_report(
        SESSION,
        [_report(A, title="Title", last_seen_at=130.0)],
        fetched=_fetch(A, title="Title"),
        requested_at=1_030.0,
    )
    # Sessions without a metadata row store nothing.
    assert store.apply_report(MISSING, [_report(A)]) is None
    assert store.read(MISSING) is None


def test_titles_are_trimmed_capped_and_kept_when_a_lookup_fails(
    store: SessionPullRequestStore,
) -> None:
    store.apply_report(SESSION, [_report(A, title="  First  ")])
    assert store.read(SESSION).entries[0].title == "First"  # type: ignore[union-attr]
    store.apply_report(SESSION, [_report(A, title="x" * 2000)])
    assert store.read(SESSION).entries[0].title == "x" * 256  # type: ignore[union-attr]
    # A host whose title lookup failed reports no title.
    store.apply_report(SESSION, [_report(A, title=None)])
    assert store.read(SESSION).entries[0].title == "x" * 256  # type: ignore[union-attr]


def test_unlinks_ignore_reports_until_the_user_links_the_pr_again(
    store: SessionPullRequestStore,
) -> None:
    store.apply_report(SESSION, [_report(A), _report(B)])
    assert store.remove(SESSION, A)
    assert not store.remove(SESSION, A)
    assert _urls(store) == [B]

    # A replayed report or fetch must not undo the user's unlink.
    store.apply_report(
        SESSION,
        [_report(A, relationship="created", last_seen_at=500.0)],
        fetched=_fetch(A),
        requested_at=1.0,
    )
    assert _urls(store) == [B]

    store.attach(SESSION, _report(A, last_seen_at=600.0))
    assert [(entry.url, entry.relationship) for entry in store.read(SESSION).entries] == [  # type: ignore[union-attr]
        (A, "attached"),
        (B, "worked_on"),
    ]


def test_sessions_cap_links_and_forget_their_oldest_unlinks(
    store: SessionPullRequestStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_pull_request_store, "MAX_PRS_PER_SESSION", 2)
    monkeypatch.setattr(session_pull_request_store, "MAX_UNLINKS_PER_SESSION", 2)
    third = "https://github.com/example/three/pull/1"
    store.apply_report(SESSION, [_report(A, last_seen_at=200.0), _report(B), _report(third)])
    assert _urls(store) == [A, B]
    store.check_room(SESSION, A)
    with pytest.raises(SessionPullRequestLimitError):
        store.check_room(SESSION, third)
    with pytest.raises(SessionPullRequestLimitError):
        store.attach(SESSION, _report(third))

    clock = iter(range(1_000_000, 10_000_000, 1_000_000))
    monkeypatch.setattr(session_pull_request_store, "now_epoch_us", lambda: next(clock))
    for url in (A, B, third):
        store.remove(SESSION, url)
    # A, the oldest unlink, was forgotten: a report links it again.
    store.apply_report(SESSION, [_report(A), _report(B), _report(third)])
    assert _urls(store) == [A]


def test_refreshing_details_never_removes_a_link(
    store: SessionPullRequestStore, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the byte budget, titles and states give way; the linked PRs never change."""
    monkeypatch.setattr(session_pull_request_store, "_MAX_STORED_BYTES", 1_400)
    monkeypatch.setattr(session_pull_request_store, "_MAX_MEMBERSHIP_BYTES", 900)
    # Random repositories don't compress, so membership reaches its budget first.
    reports = [
        _report(
            f"https://github.com/example/{secrets.token_hex(12)}/pull/{index}",
            last_seen_at=1_000.0 - index,
        )
        for index in range(1, 40)
    ]
    store.apply_report(SESSION, reports)
    linked = _urls(store)
    # Membership has its own budget, checked as each PR is added; the rest are ignored.
    assert 0 < len(linked) < len(reports)
    assert linked == [report.url for report in reports if report.url in linked]

    for index, url in enumerate(linked):
        # Random titles don't compress, so they quickly fill the budget.
        store.apply_report(
            SESSION,
            [_report(url, title=secrets.token_hex(100))],
            fetched=_fetch(url, title=secrets.token_hex(100)),
            requested_at=2_000.0 + index,
        )
        assert _urls(store) == linked
    stored = _stored_document(db_uri)
    assert stored is not None and len(stored) <= 1_400
    assert store.read(SESSION).entries[0].title is not None  # type: ignore[union-attr]
    # Re-reporting details that never fit does not rewrite the document.
    _forbid_writes(store, monkeypatch)
    store.apply_report(SESSION, [_report(linked[-1], title=secrets.token_hex(100))])


def test_display_order_pages_and_summaries(store: SessionPullRequestStore) -> None:
    """Explicit PRs come first, most recent first; inferred ones follow, oldest first."""
    inferred_old = "https://github.com/example/three/pull/1"
    inferred_new = "https://github.com/example/three/pull/2"
    store.apply_report(
        SESSION,
        [
            _report(inferred_new, relationship="inferred", first_seen_at=300.0),
            _report(inferred_old, relationship="inferred", first_seen_at=200.0),
            _report(A, last_seen_at=100.0),
            _report(B, last_seen_at=400.0),
        ],
        complete=True,
    )
    assert _urls(store) == [B, A, inferred_old, inferred_new]

    first = store.list_page(SESSION, limit=3)
    assert ([entry.url for entry in first.entries], first.has_more, first.synced) == (
        [B, A, inferred_old],
        True,
        True,
    )
    rest = store.list_page(SESSION, limit=3, after_url=inferred_old)
    assert ([entry.url for entry in rest.entries], rest.has_more) == ([inferred_new], False)
    with pytest.raises(LookupError):
        store.list_page(SESSION, limit=3, after_url="https://github.com/example/none/pull/1")

    # Reports alone don't mark a session synced; only the host's whole list does.
    store.apply_report(OTHER, [_report(A, relationship="inferred")])
    summaries = store.summaries([SESSION, OTHER, MISSING])
    assert {
        session: (summary.primary.url if summary.primary else None, summary.synced)
        for session, summary in summaries.items()
    } == {SESSION: (B, True), OTHER: (A, False)}


def test_an_unreadable_document_is_left_alone(store: SessionPullRequestStore, db_uri: str) -> None:
    store.apply_report(SESSION, [_report(A)], complete=True)
    with sa.create_engine(db_uri).begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE omnigent_conversation_metadata SET pull_requests = :doc WHERE id = :id"
            ),
            {"doc": compression.encode('{"prs": "not a list"}'), "id": bytes.fromhex(SESSION)},
        )
    before = _stored_document(db_uri)

    # Reads see a session the server hasn't imported, so the host's list decides.
    stored = store.read(SESSION)
    assert stored is not None and (stored.entries, stored.synced) == ([], False)
    assert not store.summaries([SESSION])[SESSION].synced
    with pytest.raises(UnreadablePullRequestsError):
        store.apply_report(SESSION, [_report(B)])
    with pytest.raises(UnreadablePullRequestsError):
        store.remove(SESSION, A)
    assert _stored_document(db_uri) == before


def test_relationships_from_newer_servers_read_as_worked_on(
    store: SessionPullRequestStore, db_uri: str
) -> None:
    store.apply_report(SESSION, [_report(A, relationship="created")])
    document = json.loads(compression.decode(_stored_document(db_uri)) or "{}")
    document["prs"][0]["relationship"] = "reviewed"
    with sa.create_engine(db_uri).begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE omnigent_conversation_metadata SET pull_requests = :doc WHERE id = :id"
            ),
            {"doc": compression.encode(json.dumps(document)), "id": bytes.fromhex(SESSION)},
        )
    assert store.read(SESSION).entries[0].relationship == "worked_on"  # type: ignore[union-attr]


async def test_deleting_the_session_drops_its_pull_requests(
    store: SessionPullRequestStore, conversations: SqlAlchemyConversationStore
) -> None:
    store.apply_report(SESSION, [_report(A)])
    store.remove(SESSION, B)
    store.apply_report(OTHER, [_report(A)])

    assert await conversations.delete_conversation(SESSION)
    assert store.read(SESSION) is None
    assert _urls(store, OTHER) == [A]
