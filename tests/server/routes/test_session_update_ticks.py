"""Tests for :class:`SessionUpdateTicks` and :class:`ConversationCache`,
the per-user tick sharing behind ``WS /v1/sessions/updates``.

The tick builder is a recording fake so each test can count exactly which ids
were built, for which user, and how many times. The cache tests read through
a real SQLite store that records which conversations it loads.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

import omnigent.server.routes.sessions as sessions_routes
from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation
from omnigent.server.routes._sessions.update_ticks import ConversationCache, SessionUpdateTicks
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


class _RecordingBuilder:
    """Row builder that records each call and can be gated or failed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[str]]] = []
        self.release = asyncio.Event()
        self.release.set()
        self.error: Exception | None = None
        # Ids the builder treats as inaccessible (omitted from its result).
        self.hidden: set[str] = set()

    async def __call__(
        self, ids: list[str], user_id: str | None, conversations: ConversationCache
    ) -> list[dict[str, Any]]:
        del conversations
        self.calls.append((user_id, list(ids)))
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return [{"id": sid, "user": user_id} for sid in ids if sid not in self.hidden]


@pytest.fixture
def builder() -> _RecordingBuilder:
    return _RecordingBuilder()


class _RecordingStore(SqlAlchemyConversationStore):
    """Real store that records the ids of every full conversation load."""

    def __init__(self, db_uri: str) -> None:
        super().__init__(db_uri)
        self.loaded: list[list[str]] = []
        # Runs once, between a cache call's fingerprint read and its load.
        self.write_before_load: Callable[[], object] | None = None

    def get_conversations_with_fingerprints(
        self, conversation_ids: list[str]
    ) -> dict[str, tuple[object, Conversation]]:
        self.loaded.append(sorted(conversation_ids))
        if self.write_before_load is not None:
            self.write_before_load()
            self.write_before_load = None
        return super().get_conversations_with_fingerprints(conversation_ids)


@pytest.fixture
def store(db_uri: str) -> _RecordingStore:
    return _RecordingStore(db_uri)


@pytest.fixture
def ticks(builder: _RecordingBuilder, store: _RecordingStore) -> SessionUpdateTicks:
    return SessionUpdateTicks(builder, store)


async def test_one_users_sockets_share_one_build_per_tick(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """Two sockets of one user waking on the same tick build their union once,
    and each receives exactly its own watch-set in its own order."""
    ticks.attach(ALICE)
    ticks.attach(ALICE)
    rows_a, rows_b = await asyncio.gather(
        ticks.rows(ALICE, ["s1", "s2"], tick=1),
        ticks.rows(ALICE, ["s3", "s2"], tick=1),
    )
    assert builder.calls == [(ALICE, ["s1", "s2", "s3"])]
    assert [row["id"] for row in rows_a] == ["s1", "s2"]
    assert [row["id"] for row in rows_b] == ["s3", "s2"]


async def test_later_socket_in_same_tick_reuses_built_rows(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """A socket reaching the tick after the build finished reuses those rows
    and builds only the ids nobody built yet."""
    ticks.attach(ALICE)
    ticks.attach(ALICE)
    await ticks.rows(ALICE, ["s1", "s2"], tick=1)
    rows = await ticks.rows(ALICE, ["s2", "s3"], tick=1)
    assert builder.calls == [(ALICE, ["s1", "s2"]), (ALICE, ["s3"])]
    assert [row["id"] for row in rows] == ["s2", "s3"]


async def test_new_tick_rebuilds(ticks: SessionUpdateTicks, builder: _RecordingBuilder) -> None:
    """Rows never outlive their tick: the next tick builds again."""
    ticks.attach(ALICE)
    await ticks.rows(ALICE, ["s1"], tick=1)
    await ticks.rows(ALICE, ["s1"], tick=2)
    assert builder.calls == [(ALICE, ["s1"]), (ALICE, ["s1"])]


async def test_late_socket_reuses_newer_tick(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """A socket whose tick ran late (its lock was held past the next tick)
    reuses the newer rows instead of discarding them."""
    ticks.attach(ALICE)
    ticks.attach(ALICE)
    await ticks.rows(ALICE, ["s1"], tick=2)
    await ticks.rows(ALICE, ["s1"], tick=1)
    await ticks.rows(ALICE, ["s1"], tick=2)
    assert builder.calls == [(ALICE, ["s1"])]


async def test_inaccessible_rows_are_omitted_for_every_sharing_socket(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """An id the build omits (lost access, deleted) is absent for every socket
    that shares the build, so each one reports it removed."""
    ticks.attach(ALICE)
    ticks.attach(ALICE)
    builder.hidden = {"s2"}
    rows_a, rows_b = await asyncio.gather(
        ticks.rows(ALICE, ["s1", "s2"], tick=1),
        ticks.rows(ALICE, ["s2"], tick=1),
    )
    assert [row["id"] for row in rows_a] == ["s1"]
    assert rows_b == []


async def test_users_never_share_rows(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """The same id watched by two users is built separately for each user."""
    ticks.attach(ALICE)
    ticks.attach(BOB)
    rows_alice, rows_bob = await asyncio.gather(
        ticks.rows(ALICE, ["s1"], tick=1),
        ticks.rows(BOB, ["s1"], tick=1),
    )
    assert sorted(builder.calls) == [(ALICE, ["s1"]), (BOB, ["s1"])]
    assert rows_alice[0]["user"] == ALICE
    assert rows_bob[0]["user"] == BOB


async def test_workspaces_never_share_rows(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """One user id in two workspaces is two users."""
    with workspace_scope(1):
        ticks.attach(ALICE)
        await ticks.rows(ALICE, ["s1"], tick=1)
    with workspace_scope(2):
        ticks.attach(ALICE)
        await ticks.rows(ALICE, ["s1"], tick=1)
    assert builder.calls == [(ALICE, ["s1"]), (ALICE, ["s1"])]


async def test_closing_socket_does_not_cancel_shared_build(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """A socket that disconnects while awaiting a shared build leaves the
    build running for the other socket waiting on it."""
    ticks.attach(ALICE)
    ticks.attach(ALICE)
    builder.release.clear()
    first = asyncio.ensure_future(ticks.rows(ALICE, ["s1"], tick=1))
    second = asyncio.ensure_future(ticks.rows(ALICE, ["s1"], tick=1))
    while not builder.calls:
        await asyncio.sleep(0)
    first.cancel()
    builder.release.set()
    assert [row["id"] for row in await second] == ["s1"]
    assert first.cancelled()


async def test_failed_build_fails_its_tick_only(
    ticks: SessionUpdateTicks, builder: _RecordingBuilder
) -> None:
    """A build error reaches every socket sharing it; the next tick retries."""
    ticks.attach(ALICE)
    builder.error = RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        await ticks.rows(ALICE, ["s1"], tick=1)
    builder.error = None
    assert [row["id"] for row in await ticks.rows(ALICE, ["s1"], tick=2)] == ["s1"]


async def test_state_dropped_with_last_socket(ticks: SessionUpdateTicks) -> None:
    """Each user's open-socket count rises and falls independently, and nothing
    stays behind once their last socket detaches."""
    assert ticks.attach(ALICE) == 1
    assert ticks.attach(ALICE) == 2
    assert ticks.attach(BOB) == 1
    await ticks.rows(ALICE, ["s1"], tick=1)
    assert ticks.detach(ALICE) == 1
    assert ticks.detach(BOB) == 0
    assert ticks._users
    assert ticks.detach(ALICE) == 0
    assert ticks._users == {}


async def test_users_sockets_wake_on_the_same_tick(ticks: SessionUpdateTicks) -> None:
    """Sockets of one user that start sleeping at different moments wake for
    the same tick, so a shared build is equally fresh for each."""
    interval = 1.0
    ticks.attach(ALICE)
    (user,) = ticks._users.values()
    # Start well before the next tick boundary so the second sleeper, starting
    # 20 ms later, deterministically targets the same boundary.
    remaining = interval - ((time.monotonic() - user.phase * interval) % interval)
    if remaining < interval / 2:
        await asyncio.sleep(remaining)
    first = asyncio.ensure_future(ticks.next_tick(ALICE, interval))
    await asyncio.sleep(0.02)
    second = asyncio.ensure_future(ticks.next_tick(ALICE, interval))
    assert await first == await second


def test_cache_reloads_only_changed_conversations(store: _RecordingStore) -> None:
    """An unchanged tick loads nothing; a change reloads exactly that row and
    the cache returns its new value."""
    a = store.create_conversation(title="a").id
    b = store.create_conversation(title="b").id
    cache = ConversationCache(store)
    assert cache.get_conversations([a, b])[a].title == "a"
    assert store.loaded == [sorted([a, b])]

    store.loaded.clear()
    assert set(cache.get_conversations([a, b])) == {a, b}
    assert store.loaded == []

    store.set_session_live_status(b, "running")
    result = cache.get_conversations([a, b])
    assert store.loaded == [[b]]
    assert result[b].live_status == "running"


async def test_cache_omits_deleted_conversations(store: _RecordingStore) -> None:
    """A row that stops resolving is omitted, so the stream reports it removed."""
    a = store.create_conversation(title="a").id
    b = store.create_conversation(title="b").id
    cache = ConversationCache(store)
    cache.get_conversations([a, b])
    assert await store.delete_conversation(b)
    assert set(cache.get_conversations([a, b])) == {a}


def test_cache_reloads_everything_after_max_age(
    store: _RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The age limit reloads unchanged rows too, bounding staleness from any
    input the fingerprint misses."""
    a = store.create_conversation(title="a").id
    cache = ConversationCache(store)
    cache.get_conversations([a])
    monkeypatch.setattr(sessions_routes, "_SESSION_UPDATES_FULL_RELOAD_INTERVAL_S", 0.0)
    cache.get_conversations([a])
    assert store.loaded == [[a], [a]]


def test_cache_never_pairs_a_row_with_an_older_fingerprint(store: _RecordingStore) -> None:
    """A row that changes between the fingerprint read and the load, then
    changes back, is reloaded rather than served stale from the cache."""
    a = store.create_conversation(title="a").id
    store.set_session_live_status(a, "idle")
    cache = ConversationCache(store)
    store.write_before_load = lambda: store.set_session_live_status(a, "running")
    assert cache.get_conversations([a])[a].live_status == "running"
    store.set_session_live_status(a, "idle")
    assert cache.get_conversations([a])[a].live_status == "idle"
