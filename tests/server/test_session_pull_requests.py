"""Server-side PR state: parsing reports, vouching, rewriting payloads, and refreshes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.server import session_pull_requests as server_prs
from omnigent.server.routes._sessions import helpers
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_pull_request_store import (
    SessionPullRequests,
    SessionPullRequestStore,
    SessionSummary,
    StoredPullRequest,
)

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
A = "https://github.com/example/one/pull/42"
B = "https://github.com/example/two/pull/7"


def _entry(url: str, number: int, **overrides: Any) -> dict[str, Any]:
    return {
        "url": url,
        "provider": "github",
        "host": "github.com",
        "repository": url.removeprefix("https://github.com/").split("/pull/")[0],
        "number": number,
        "relationship": "created",
        "source": "native_hook",
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "title": None,
        "title_checked_at": 0,
        "title_lookup_timed_out": False,
        **overrides,
    }


def _info(*entries: dict[str, Any], selected: str | None = A, number: int = 42) -> dict[str, Any]:
    return {
        "available": True,
        "tracking_available": True,
        "prs": list(entries),
        "selected_pr_url": selected,
        "pr": {
            "number": number,
            "title": "Add offline PRs",
            "state": "OPEN",
            "url": selected,
            "is_draft": True,
            "body": "A long description that stays on the host.",
        },
    }


@pytest.fixture()
def store(db_uri: str) -> SessionPullRequestStore:
    SqlAlchemyConversationStore(db_uri).create_conversation(conversation_id=SESSION)
    return SessionPullRequestStore(db_uri)


@pytest.mark.parametrize(
    "bad",
    [
        {"url": "javascript:alert(1)"},
        {"url": "https://github.com/example/one/pull/42 extra"},
        {"number": -1},
        {"host": ""},
        {"repository": "x" * 513},
        {"first_seen_at": "yesterday"},
    ],
)
def test_malformed_reports_are_skipped(bad: dict[str, Any]) -> None:
    reports = server_prs.parse_reports([{**_entry(A, 42), **bad}, _entry(B, 7), "junk"])
    assert [report.url for report in reports] == [B]


def test_reports_from_older_and_newer_hosts_are_kept() -> None:
    legacy = _entry(A, 42)
    del legacy["provider"]
    [older, newer] = server_prs.parse_reports([legacy, _entry(B, 7, relationship="reviewed")])
    assert older.provider == "github"
    assert newer.relationship == "worked_on"


def test_ingest_stores_the_selected_prs_state_and_answers_with_the_server_list(
    store: SessionPullRequestStore,
) -> None:
    info = _info(_entry(A, 42), _entry(B, 7))
    stored = server_prs.ingest_info(store, SESSION, info, requested_at=50.0)
    assert stored is not None and stored.synced
    entries = {entry.url: entry for entry in stored.entries}
    assert (entries[A].state, entries[A].is_draft, entries[A].state_checked_at) == (
        "OPEN",
        True,
        50.0,
    )
    assert entries[A].title == "Add offline PRs"
    assert entries[B].state is None

    answered = server_prs.answer(info, stored)
    assert [entry["url"] for entry in answered["prs"]] == [A, B]
    assert answered["prs"][0]["provider_display"]["number_prefix"] == "#"
    assert answered["selected_pr_url"] == A
    # Payloads without session tracking are passed through untouched.
    assert server_prs.answer({"available": False}, stored) == {"available": False}


def test_ingest_stores_the_states_a_title_refresh_read_for_unselected_prs(
    store: SessionPullRequestStore,
) -> None:
    """The selected PR's full read wins over its own listed entry from the same response."""
    info = _info(
        _entry(A, 42, state="CLOSED"),
        _entry(B, 7, state="MERGED", title="Shipped"),
        _entry("https://github.com/example/three/pull/3", 3),
    )
    stored = server_prs.ingest_info(store, SESSION, info, requested_at=50.0)
    assert stored is not None
    states = {entry.url: (entry.state, entry.state_checked_at) for entry in stored.entries}
    assert states == {
        A: ("OPEN", 50.0),
        B: ("MERGED", 50.0),
        "https://github.com/example/three/pull/3": (None, None),
    }
    assert stored.get(B).title == "Shipped"  # type: ignore[union-attr]


def test_a_branch_pr_the_selection_does_not_name_keeps_no_state(
    store: SessionPullRequestStore,
) -> None:
    stored = server_prs.ingest_info(
        store, SESSION, _info(_entry(A, 42), number=99), requested_at=50.0
    )
    assert stored is not None and stored.entries[0].state is None


def test_hosts_that_still_select_an_unlinked_pr_cannot_bring_it_back(
    store: SessionPullRequestStore,
) -> None:
    info = _info(_entry(A, 42), _entry(B, 7))
    server_prs.ingest_info(store, SESSION, info, requested_at=50.0)
    store.remove(SESSION, A)
    stored = server_prs.ingest_info(store, SESSION, info, requested_at=60.0)
    answered = server_prs.answer(info, stored)
    assert [entry["url"] for entry in answered["prs"]] == [B]
    assert "selected_pr_url" not in answered
    assert answered["pr"] is None


def test_the_server_vouches_only_for_prs_it_links(store: SessionPullRequestStore) -> None:
    # Until the session is imported, the host's own list decides.
    assert server_prs.vouch(store.read(SESSION), A) == {}
    assert server_prs.vouch(None, A) == {}

    server_prs.ingest_info(store, SESSION, _info(_entry(A, 42), _entry(B, 7)), requested_at=50.0)
    stored = store.read(SESSION)
    assert server_prs.vouch(stored, B) == {"server_tracked": True}
    assert server_prs.vouch(stored, None) == {"server_tracked": True, "default_pr_url": A}
    with pytest.raises(server_prs.NotAssociatedError):
        server_prs.vouch(stored, "https://github.com/example/none/pull/1")


def _summary(
    state: str | None, checked_at: float | None, *, synced: bool = True
) -> SessionSummary:
    primary = StoredPullRequest(
        url=A,
        provider="github",
        host="github.com",
        repository="example/one",
        number=42,
        relationship="created",
        first_seen_at=1.0,
        last_seen_at=1.0,
        state=state,
        state_checked_at=checked_at,
    )
    return SessionSummary(primary=primary, synced=synced)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (SessionSummary(primary=None, synced=False), True),
        (SessionSummary(primary=None, synced=True), False),
        (_summary("OPEN", 1_000.0 - server_prs.FRESHNESS_S), True),
        (_summary("OPEN", 999.0), False),
        (_summary(None, None), True),
        (_summary("MERGED", 0.0), False),
    ],
    ids=["never-imported", "no-prs", "stale-open", "fresh-open", "unknown-state", "merged"],
)
def test_listed_sessions_refresh_until_imported_and_while_open_prs_are_stale(
    summary: SessionSummary, expected: bool
) -> None:
    assert server_prs.needs_refresh(summary, now=1_000.0) is expected


async def test_turn_end_refresh_runs_once_more_for_a_turn_that_ends_during_it() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def refresh(session_id: str) -> None:
        started.append(session_id)
        await release.wait()
        raise RuntimeError("host went away")

    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0)
    refresher.bind(refresh)
    refresher.turn_ended(SESSION)
    refresher.turn_ended(SESSION)
    refresher.turn_ended("other")
    for _ in range(3):
        await asyncio.sleep(0)
    assert started == [SESSION, "other"]
    # A turn that ends during the refresh gets one more, not one per notification.
    refresher.turn_ended(SESSION)
    refresher.turn_ended(SESSION)
    release.set()
    for _ in range(6):
        await asyncio.sleep(0)
    assert started == [SESSION, "other", SESSION]
    await refresher.aclose()


async def test_turn_end_refresh_is_skipped_after_a_read_that_covers_the_turn() -> None:
    started: list[str] = []

    async def refresh(session_id: str) -> None:
        started.append(session_id)

    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0.01)
    refresher.bind(refresh)
    refresher.turn_ended(SESSION)
    refresher.turn_ended("other")
    refresher.note_read(SESSION, time.time())
    # A read sent before the turn ended doesn't cover it.
    refresher.note_read("other", time.time() - 60)
    while refresher._running:
        await asyncio.gather(*refresher._running.values())
    assert started == ["other"]


async def test_listed_sessions_refresh_at_most_once_per_window() -> None:
    started: list[str] = []

    async def refresh(session_id: str) -> None:
        started.append(session_id)

    refresher = server_prs.PullRequestRefresher()
    refresher.bind(refresh)
    for _ in range(3):
        refresher.request(SESSION)
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == [SESSION]
    await refresher.aclose()
    refresher.request("other")
    refresher.turn_ended("other")
    await asyncio.sleep(0)
    assert started == [SESSION]


async def test_the_same_session_id_is_refreshed_separately_in_each_workspace() -> None:
    started: list[tuple[int, str]] = []

    async def refresh(session_id: str) -> None:
        started.append((current_workspace_id(), session_id))

    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0)
    refresher.bind(refresh)
    # Session ids are unique only within a workspace.
    for workspace_id in (1, 2):
        with workspace_scope(workspace_id):
            refresher.request(SESSION)
    while refresher._running:
        await asyncio.gather(*refresher._running.values())
    assert sorted(started) == [(1, SESSION), (2, SESSION)]

    # A read in one workspace covers only that workspace's turn end.
    started.clear()
    with workspace_scope(1):
        refresher.turn_ended(SESSION)
    with workspace_scope(2):
        refresher.turn_ended(SESSION)
        refresher.note_read(SESSION, time.time())
    while refresher._running:
        await asyncio.gather(*refresher._running.values())
    assert started == [(1, SESSION)]


def test_refreshes_are_noops_outside_an_event_loop() -> None:
    async def refresh(session_id: str) -> None:
        raise AssertionError(session_id)

    refresher = server_prs.PullRequestRefresher()
    refresher.bind(refresh)
    refresher.turn_ended(SESSION)
    refresher.request(SESSION)


def test_only_a_completed_turn_notifies_the_refresher(monkeypatch: pytest.MonkeyPatch) -> None:
    notified: list[str] = []
    monkeypatch.setattr(server_prs, "notify_turn_end", notified.append)
    monkeypatch.setattr(helpers.session_live_state, "persist_live_status", lambda *_: None)
    session = "c0ffee00c0ffee00c0ffee00c0ffee00"
    monkeypatch.delitem(helpers._session_status_cache, session, raising=False)

    helpers._publish_status(session, "idle")
    helpers._publish_status(session, "running")
    helpers._publish_status(session, "running")
    assert notified == []
    helpers._publish_status(session, "idle")
    assert notified == [session]
    helpers._publish_status(session, "idle")
    # A stop or archive publishes idle without persisting it.
    helpers._publish_status(session, "waiting")
    helpers._publish_status(session, "idle", persist_live_status=False)
    assert notified == [session]


def _stored(url: str, **overrides: Any) -> StoredPullRequest:
    fields = {
        key: value
        for key, value in _entry(url, int(url.rsplit("/", 1)[1])).items()
        if key not in ("source", "title_checked_at", "title_lookup_timed_out")
    }
    return StoredPullRequest(**{**fields, **overrides})


def _run(*, owns_session: bool, finished_at: float | None = 2_000.0) -> server_prs.RunWindow:
    return server_prs.RunWindow(
        run_id="run", started_at=1_000.0, finished_at=finished_at, owns_session=owns_session
    )


def _picked(window: server_prs.RunWindow, *entries: StoredPullRequest) -> list[str]:
    stored = SessionPullRequests(entries=list(entries), synced=True)
    return [entry.url for entry in server_prs.run_pull_requests(window, stored, now=5_000.0)]


C = "https://github.com/example/three/pull/3"


def test_a_run_in_a_continued_session_gets_the_prs_first_seen_while_it_ran() -> None:
    entries = (
        _stored(A, relationship="worked_on", first_seen_at=500.0),
        _stored(B, relationship="worked_on", first_seen_at=1_500.0),
        # Reports of the turn's last calls land shortly after it ends.
        _stored(C, first_seen_at=2_000.0 + server_prs.RUN_ATTRIBUTION_GRACE_S),
        _stored("https://github.com/example/four/pull/4", first_seen_at=2_500.0),
    )
    assert _picked(_run(owns_session=False), *entries) == [C, B]
    # A run still going covers everything seen since it started.
    assert _picked(_run(owns_session=False, finished_at=None), *entries) == [
        C,
        "https://github.com/example/four/pull/4",
        B,
    ]


def test_a_run_that_owns_its_session_gets_its_prs_created_first() -> None:
    entries = (
        _stored(A, relationship="attached", first_seen_at=3_000.0),
        _stored(B, relationship="worked_on", first_seen_at=1_200.0),
        _stored(C, relationship="created", first_seen_at=1_800.0),
        _stored("https://github.com/example/four/pull/4", relationship="inferred"),
    )
    assert _picked(_run(owns_session=True), *entries) == [C, B, A]


def test_a_branch_inferred_pr_shows_only_as_the_lone_pr_of_a_runs_own_session() -> None:
    inferred = _stored(A, relationship="inferred", first_seen_at=1_500.0)
    assert _picked(_run(owns_session=True), inferred) == [A]
    assert _picked(_run(owns_session=False), inferred) == []
    other = _stored(B, relationship="inferred", first_seen_at=1_600.0)
    assert _picked(_run(owns_session=True), inferred, other) == []


@pytest.mark.parametrize(
    ("entries", "synced", "expected"),
    [
        ((), False, True),
        ((), True, False),
        ((_stored(A, state="MERGED", state_checked_at=0.0),), True, False),
        ((_stored(A, state="OPEN", state_checked_at=900.0),), True, False),
        ((_stored(A, state="OPEN", state_checked_at=600.0),), True, True),
        ((_stored(A, state="MERGED", state_checked_at=990.0), _stored(B)), True, True),
    ],
)
def test_any_open_or_unknown_pr_past_freshness_makes_a_session_stale(
    entries: tuple[StoredPullRequest, ...], synced: bool, expected: bool
) -> None:
    stored = SessionPullRequests(entries=list(entries), synced=synced)
    assert server_prs.has_stale_state(stored, now=1_000.0) is expected
