"""Server-side PR state: parsing reports, vouching, rewriting payloads, and refreshes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from omnigent.server import session_pull_requests as server_prs
from omnigent.server.routes._sessions import helpers
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_pull_request_store import (
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
