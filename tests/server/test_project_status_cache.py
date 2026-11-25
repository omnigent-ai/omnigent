"""The project status cache: single-flight misses, invalidation after commit, write hooks."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from omnigent.server.project_status_cache import (
    ProjectStatusCache,
    cached_source,
    invalidate_after_commit,
    project_status_cache,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    project_agents,
    project_app,
    project_client,
)


def test_concurrent_misses_compute_once() -> None:
    cache = ProjectStatusCache()
    calls: list[int] = []
    release = threading.Event()

    def compute() -> int:
        calls.append(1)
        release.wait(5)
        return 42

    results: list[int] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(cache.get_or_compute("p", "spend", 1, compute))
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(5)
    assert results == [42] * 4 and len(calls) == 1
    assert cache.get_or_compute("p", "spend", 1, lambda: 0) == 42


def test_invalidation_clears_only_its_project_and_inputs() -> None:
    cache = ProjectStatusCache()
    cache.get_or_compute("p", "spend", "k", lambda: "spend")
    cache.get_or_compute("p", "tickets", "k", lambda: "tickets")
    cache.get_or_compute("q", "spend", "k", lambda: "other")
    cache.invalidate("p", ("spend",))
    assert cache.get_or_compute("p", "spend", "k", lambda: "new") == "new"
    assert cache.get_or_compute("p", "tickets", "k", lambda: "new") == "tickets"
    assert cache.get_or_compute("q", "spend", "k", lambda: "new") == "other"


def test_a_write_during_a_computation_keeps_its_result_out() -> None:
    cache = ProjectStatusCache()

    def compute() -> str:
        cache.invalidate("p")  # A write commits while the numbers are being read.
        return "before the write"

    assert cache.get_or_compute("p", "prs", "k", compute) == "before the write"
    assert cache.get_or_compute("p", "prs", "k", lambda: "after") == "after"


def test_failures_are_not_cached_and_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = ProjectStatusCache(ttl_s=10, max_entries=2)

    def fail() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cache.get_or_compute("p", "spend", "k", fail)
    assert cache.get_or_compute("p", "spend", "k", lambda: "ok") == "ok"
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 11)
    assert cache.get_or_compute("p", "spend", "k", lambda: "fresh") == "fresh"
    for key in ("a", "b", "c"):
        cache.get_or_compute("p", "spend", key, lambda key=key: key)
    assert cache.get_or_compute("p", "spend", "k", lambda: "evicted") == "evicted"


def test_invalidation_waits_for_commit_and_skips_rollback() -> None:
    project_status_cache.get_or_compute("commit-p", "spend", "k", lambda: "old")
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        session.execute(text("select 1"))
        invalidate_after_commit(session, "commit-p", ("spend",))
        assert project_status_cache.get_or_compute("commit-p", "spend", "k", lambda: "x") == "old"
        session.rollback()
        assert project_status_cache.get_or_compute("commit-p", "spend", "k", lambda: "x") == "old"
        invalidate_after_commit(session, "commit-p", ("spend",))
        session.commit()
    assert project_status_cache.get_or_compute("commit-p", "spend", "k", lambda: "new") == "new"


def test_cached_source_keeps_optional_methods_optional() -> None:
    class Source:
        calls = 0

        def spend(self, project, period):  # type: ignore[no-untyped-def]
            Source.calls += 1
            return Source.calls

    period = type("Period", (), {"window": "7d", "today_utc": "d", "since_day_utc": "s"})()
    project = type("Project", (), {"id": "source-p"})()
    wrapped = cached_source(Source())
    assert not hasattr(wrapped, "root_days")
    assert wrapped.spend(project, period) == wrapped.spend(project, period) == 1


async def test_new_ticket_moves_and_spend_show_on_the_next_load(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "zz-cache"})).json()
    project = f"/v1/projects/{created['id']}"

    async def totals() -> dict:
        response = await project_client.get(f"{project}/status/summary", params={"window": "7d"})
        return response.json()["totals"]

    assert (await totals())["tickets_done"] == 0
    stages = (await project_client.get(f"{project}/tickets")).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    ticket = (await project_client.post(f"{project}/tickets", json={"title": "T"})).json()
    await project_client.patch(f"{project}/tickets/{ticket['id']}", json={"stage_id": done})
    assert (await totals())["tickets_done"] == 1

    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation(project_id=created["id"])
    sub_agent = conversations.create_conversation(parent_conversation_id=root.id)
    today = datetime.now(UTC).date().isoformat()
    conversations.add_session_daily_cost(root.id, today, 1.0)
    assert (await totals())["spend_cents"] == 100
    # A sub-agent's spend counts for its root's project.
    conversations.add_session_daily_cost(sub_agent.id, today, 0.5)
    assert (await totals())["spend_cents"] == 150


async def test_moving_a_session_out_shows_on_the_next_load(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    """A spend rebuild, here from filing a session elsewhere, clears the cached spend."""
    created = (await project_client.post("/v1/projects", json={"name": "zz-cache-move"})).json()
    project = f"/v1/projects/{created['id']}"
    conversations = SqlAlchemyConversationStore(db_uri)
    kept = conversations.create_conversation(project_id=created["id"])
    moved = conversations.create_conversation(project_id=created["id"])
    today = datetime.now(UTC).date().isoformat()
    conversations.add_session_daily_cost(kept.id, today, 1.0)
    conversations.add_session_daily_cost(moved.id, today, 2.0)

    async def spend_cents() -> int:
        response = await project_client.get(f"{project}/status/summary", params={"window": "7d"})
        return response.json()["totals"]["spend_cents"]

    assert await spend_cents() == 300
    assert conversations.set_conversation_project(moved.id, None)
    assert await spend_cents() == 100
