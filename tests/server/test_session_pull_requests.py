"""Server-side PR state: parsing host payloads, rewriting them, and the turn-end refresh."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.server import session_pull_requests as server_prs
from omnigent.server.routes._sessions import helpers
from omnigent.stores.session_pull_request_store import SessionPullRequestStore

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


def _pr(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Add offline PRs",
        "state": "OPEN",
        "url": A,
        "is_draft": True,
        "body": "A long description that stays on the host.",
        "labels": [{"name": "bug", "color": "d73a4a"}],
    }


@pytest.fixture()
def store(db_uri: str) -> SessionPullRequestStore:
    return SessionPullRequestStore(db_uri)


@pytest.mark.parametrize(
    "bad",
    [
        {"url": "javascript:alert(1)"},
        {"url": "https://github.com/example/one/pull/42 extra"},
        {"number": -1},
        {"relationship": "owned"},
        {"host": ""},
        {"repository": "x" * 513},
        {"first_seen_at": "yesterday"},
    ],
)
def test_malformed_entries_are_skipped(bad: dict[str, Any]) -> None:
    reports = server_prs.reports_from_info(
        {"prs": [{**_entry(A, 42), **bad}, _entry(B, 7), "junk"]}
    )
    assert [report.url for report in reports] == [B]


def test_entries_from_hosts_that_predate_providers_are_github() -> None:
    legacy = _entry(A, 42)
    del legacy["provider"]
    [report] = server_prs.reports_from_info({"prs": [legacy]})
    assert report.provider == "github"


def test_only_the_state_draft_flag_and_title_are_kept() -> None:
    assert server_prs.fetched_state(_pr(42)) == ("OPEN", True, "Add offline PRs")
    assert server_prs.fetched_state(None) is None
    assert server_prs.fetched_state({"number": 1}) is None


def test_ingest_stores_the_selected_prs_state(store: SessionPullRequestStore) -> None:
    info = {"prs": [_entry(A, 42), _entry(B, 7)], "selected_pr_url": A, "pr": _pr(42)}
    assert server_prs.ingest_info(store, SESSION, info, synced_at=50.0)
    entries = {entry.url: entry for entry in store.list_for_session(SESSION)}
    assert (entries[A].state, entries[A].is_draft, entries[A].state_checked_at) == (
        "OPEN",
        True,
        50.0,
    )
    assert entries[A].title == "Add offline PRs"
    assert entries[B].state is None


def test_ingest_ignores_a_pr_that_is_not_the_selected_one(store: SessionPullRequestStore) -> None:
    info = {"prs": [_entry(A, 42)], "selected_pr_url": A, "pr": _pr(99)}
    server_prs.ingest_info(store, SESSION, info)
    assert store.list_for_session(SESSION)[0].state is None


def test_answers_with_the_server_list(store: SessionPullRequestStore) -> None:
    store.record(SESSION, server_prs.reports_from_info({"prs": [_entry(B, 7)]}))
    info = {"available": True, "prs": [_entry(A, 42)], "selected_pr_url": A, "pr": _pr(42)}
    server_prs.ingest_info(store, SESSION, info)
    answered = server_prs.apply_server_state(store, SESSION, info)
    assert [entry["url"] for entry in answered["prs"]] == [A, B]
    assert (answered["prs"][0]["state"], answered["prs"][0]["is_draft"]) == ("OPEN", True)
    assert answered["prs"][0]["provider_display"]["number_prefix"] == "#"
    assert answered["selected_pr_url"] == A
    # Payloads without session tracking are passed through untouched.
    assert server_prs.apply_server_state(store, SESSION, {"available": False}) == {
        "available": False
    }


def test_hosts_that_ignore_unlinks_cannot_reselect_an_unlinked_pr(
    store: SessionPullRequestStore,
) -> None:
    info = {"prs": [_entry(A, 42), _entry(B, 7)], "selected_pr_url": A, "pr": _pr(42)}
    server_prs.ingest_info(store, SESSION, info)
    store.remove(SESSION, A)
    server_prs.ingest_info(store, SESSION, info)
    answered = server_prs.apply_server_state(store, SESSION, info)
    assert [entry["url"] for entry in answered["prs"]] == [B]
    assert "selected_pr_url" not in answered
    assert answered["pr"] is None


async def test_turn_end_refresh_runs_once_per_session_at_a_time() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def refresh(session_id: str) -> None:
        started.append(session_id)
        await release.wait()
        raise RuntimeError("host went away")

    sync = server_prs.TurnEndSync(refresh)
    sync.notify(SESSION)
    sync.notify(SESSION)
    sync.notify("other")
    await asyncio.sleep(0)
    assert started == [SESSION, "other"]
    release.set()
    for _ in range(3):
        await asyncio.sleep(0)
    sync.notify(SESSION)
    await asyncio.sleep(0)
    assert started == [SESSION, "other", SESSION]


def test_turn_end_refresh_is_a_noop_outside_an_event_loop() -> None:
    async def refresh(session_id: str) -> None:
        raise AssertionError(session_id)

    server_prs.TurnEndSync(refresh).notify(SESSION)


def test_only_a_turn_ending_notifies_the_refresher(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert notified == [session]
