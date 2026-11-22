"""The server's PR refreshes: listed sessions ask for a summary, turn ends for a full read."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter

from omnigent import runtime
from omnigent.server import session_pull_requests as server_prs
from omnigent.server.routes.sessions import routes_resources
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

URL = "https://github.com/example/repo/pull/42"
SESSION = "c0ffee00c0ffee00c0ffee00c0ffee00"


class Calls:
    """Records refreshes; ``on_full`` runs inside each full refresh."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_full: Any = None

    async def full(self, session_id: str) -> None:
        del session_id
        self.calls.append("full")
        if self.on_full is not None:
            self.on_full()

    async def listed(self, session_id: str) -> None:
        del session_id
        self.calls.append("listed")


async def _settle(refresher: server_prs.PullRequestRefresher) -> None:
    while refresher._running:
        await asyncio.gather(*refresher._running.values())


async def test_listed_sessions_use_the_listed_refresh_and_turn_ends_the_full_one() -> None:
    calls = Calls()
    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0)
    refresher.bind(calls.full, calls.listed)
    refresher.request(SESSION)
    await _settle(refresher)
    refresher.turn_ended(SESSION)
    await _settle(refresher)
    await refresher.aclose()
    assert calls.calls == ["listed", "full"]


async def test_without_a_listed_refresh_listed_sessions_get_the_full_one() -> None:
    calls = Calls()
    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0)
    refresher.bind(calls.full)
    refresher.request(SESSION)
    await _settle(refresher)
    await refresher.aclose()
    assert calls.calls == ["full"]


async def test_a_turn_its_refresh_did_not_cover_makes_the_next_listed_refresh_full() -> None:
    # The turn-end refresh failed (or reached an offline host), so no read noted the turn;
    # a summary would miss PRs the turn opened.
    calls = Calls()
    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0)
    refresher.bind(calls.full, calls.listed)
    refresher.turn_ended(SESSION)
    await _settle(refresher)
    refresher.request(SESSION)
    await _settle(refresher)
    await refresher.aclose()
    assert calls.calls == ["full", "full"]


async def test_a_read_that_covers_the_turn_restores_the_listed_refresh() -> None:
    calls = Calls()
    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0)
    refresher.bind(calls.full, calls.listed)
    refresher.turn_ended(SESSION)
    await _settle(refresher)
    refresher.note_read(SESSION, time.time())
    refresher.request(SESSION)
    await _settle(refresher)
    await refresher.aclose()
    assert calls.calls == ["full", "listed"]


async def test_a_covering_turn_end_refresh_leaves_the_listed_refresh() -> None:
    calls = Calls()
    refresher = server_prs.PullRequestRefresher(turn_end_delay_s=0)
    refresher.bind(calls.full, calls.listed)
    calls.on_full = lambda: refresher.note_read(SESSION, time.time())
    refresher.turn_ended(SESSION)
    await _settle(refresher)
    refresher.request(SESSION)
    await _settle(refresher)
    await refresher.aclose()
    assert calls.calls == ["full", "listed"]


class _Refresher(server_prs.PullRequestRefresher):
    """The real refresher, exposing the refreshes the routes bind and the reads it is told of."""

    def __init__(self) -> None:
        super().__init__(turn_end_delay_s=0)
        self.noted: list[float] = []

    def bind(self, refresh: Any, listed: Any = None) -> None:
        super().bind(refresh, listed)
        self.full, self.listed = refresh, listed

    def note_read(self, session_id: str, requested_at: float) -> None:
        self.noted.append(requested_at)
        super().note_read(session_id, requested_at)


class _Router:
    def runner_is_online(self, runner_id: str) -> bool:
        return True


class Routes:
    """The server's bound refreshes against real stores and a fake runner."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        conversations = SqlAlchemyConversationStore(f"sqlite:///{tmp}/omnigent.db")
        self.store = server_prs.open_store(conversations)
        self.session_id = conversations.create_conversation(
            workspace=str(tmp), runner_id="runner-1", git_branch="feature"
        ).id
        self.params: list[dict[str, str]] = []
        self.prs: list[dict[str, Any]] = []
        self.full_extra: dict[str, Any] = {}
        self.summary_pr: dict[str, Any] | None = None
        self.runner = httpx.AsyncClient(
            base_url="http://runner.test", transport=httpx.MockTransport(self._respond)
        )

        async def runner_client(session_id: str, *, conversation: Any) -> httpx.AsyncClient:
            return self.runner

        # Replace only the runner transport and routing; routes and stores stay real.
        monkeypatch.setattr(
            routes_resources, "_get_runner_client_for_resource_access", runner_client
        )
        monkeypatch.setattr(routes_resources, "get_server_runner_router", lambda: None)
        monkeypatch.setattr(runtime, "get_runner_router", _Router)
        self.refresher = _Refresher()
        routes_resources.register_resources_routes(
            APIRouter(),
            conversation_store=conversations,
            agent_store=SqlAlchemyAgentStore(f"sqlite:///{tmp}/omnigent.db"),
            pull_request_refresher=self.refresher,
        )

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.params.append(dict(request.url.params))
        if "summary" in request.url.params:
            return httpx.Response(
                200,
                json={
                    "available": True,
                    "selected_pr_url": URL,
                    "pr": self.summary_pr,
                    "pr_states": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "available": True,
                "tracking_available": True,
                "prs": self.prs,
                **self.full_extra,
            },
        )

    def report(self, *urls: str) -> None:
        """Have full reads report ``urls`` as the session's complete PR list."""
        self.prs = [
            {
                "url": url,
                "host": "github.com",
                "repository": "example/repo",
                "number": int(url.rsplit("/", 1)[1]),
                "relationship": "worked_on",
                "first_seen_at": 1.0,
                "last_seen_at": 1.0,
            }
            for url in urls
        ]

    async def sync(self, *urls: str) -> None:
        """Store ``urls`` as the session's complete PR list through a full read."""
        self.report(*urls)
        await self.refresher.full(self.session_id)
        self.params.clear()
        self.refresher.noted.clear()


@pytest.fixture
async def routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Routes]:
    routes = Routes(tmp_path, monkeypatch)
    yield routes
    await routes.refresher.aclose()
    await routes.runner.aclose()


async def test_a_listed_session_with_synced_prs_asks_for_a_summary(routes: Routes) -> None:
    await routes.sync(URL)
    routes.summary_pr = {
        "number": 42,
        "url": URL,
        "title": "Renamed",
        "state": "MERGED",
        "is_draft": False,
    }
    await routes.refresher.listed(routes.session_id)
    assert [params.get("summary") for params in routes.params] == ["true"]

    # The summary updates the stored state and title, and leaves the list and its sync.
    stored = routes.store.read(routes.session_id)
    assert stored is not None and stored.synced
    assert [(pr.url, pr.state, pr.title) for pr in stored.entries] == [(URL, "MERGED", "Renamed")]


async def test_a_listed_session_gets_full_reads_until_it_is_synced_and_discovered(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Listed refreshes alone, with no turn ending: discovery that failed is still retried.
    monkeypatch.setattr(server_prs, "FRESHNESS_S", 0.0)
    routes.report(URL)
    routes.full_extra = {"discovery_incomplete": True}
    modes: list[str | None] = []

    async def listed_refresh() -> None:
        routes.refresher.request(routes.session_id)
        await _settle(routes.refresher)
        [params] = routes.params
        routes.params.clear()
        modes.append(params.get("summary"))

    await listed_refresh()  # Not synced yet: a full read, whose discovery fails.
    await listed_refresh()  # Synced, but discovery did not finish: a full read again.
    routes.full_extra = {}
    await listed_refresh()  # Discovery finishes.
    await listed_refresh()
    assert modes == [None, None, None, "true"]


async def test_a_listed_session_synced_without_prs_gets_the_full_read(routes: Routes) -> None:
    await routes.sync()
    await routes.refresher.listed(routes.session_id)
    assert len(routes.params) == 1
    assert "summary" not in routes.params[0]


async def test_only_a_full_read_covers_an_ended_turn(routes: Routes) -> None:
    await routes.sync(URL)
    await routes.refresher.listed(routes.session_id)
    assert routes.refresher.noted == []

    before = time.time()
    await routes.refresher.full(routes.session_id)
    assert "summary" not in routes.params[-1]
    assert len(routes.refresher.noted) == 1
    assert routes.refresher.noted[0] >= before


async def test_a_full_read_whose_discovery_did_not_finish_does_not_cover_a_turn(
    routes: Routes,
) -> None:
    await routes.sync(URL)
    routes.full_extra = {"discovery_incomplete": True}
    await routes.refresher.full(routes.session_id)
    assert routes.refresher.noted == []

    routes.full_extra = {}
    await routes.refresher.full(routes.session_id)
    assert len(routes.refresher.noted) == 1
