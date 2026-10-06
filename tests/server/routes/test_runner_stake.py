"""The per-runner timer weighs a silent drop with bounded reads of the runner's sessions."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.common import _ACP_SUBAGENT_ID_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_RUNNER_ID = "runner-stake"
_HOST_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_HOST_B = "b1b2c3d4e5f60718293a4b5c6d7e8f90"


@pytest.fixture(autouse=True)
def _isolated_status_cache() -> Iterator[None]:
    saved = dict(orchestration._session_status_cache)
    orchestration._session_status_cache.clear()
    try:
        yield
    finally:
        orchestration._session_status_cache.clear()
        orchestration._session_status_cache.update(saved)


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


def _session(
    store: SqlAlchemyConversationStore,
    *,
    status: str | None = "running",
    host_id: str | None = None,
    runner_id: str | None = _RUNNER_ID,
    **create_kwargs: Any,
) -> str:
    conv = store.create_conversation(runner_id=runner_id, **create_kwargs)
    if status is not None:
        store.set_session_live_status(conv.id, status)
    if host_id is not None:
        store.set_host_id(conv.id, host_id, workspace="/tmp/runner-stake")
    return conv.id


class _Reads:
    """Records every call the stake makes to the named store methods."""

    def __init__(
        self,
        store: SqlAlchemyConversationStore,
        monkeypatch: pytest.MonkeyPatch,
        *names: str,
    ) -> None:
        self.calls: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
        for name in names:
            monkeypatch.setattr(store, name, self._recording(name, getattr(store, name)))

    def _recording(self, name: str, real: Any) -> Any:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.calls[name].append(kwargs)
            return real(*args, **kwargs)

        return record


def _stake(
    store: SqlAlchemyConversationStore, reference: int | None = None
) -> orchestration._RunnerStake:
    return orchestration._RunnerStake(_RUNNER_ID, reference, store)


async def test_only_mid_turn_sessions_bound_to_the_runner_are_at_stake(
    store: SqlAlchemyConversationStore,
) -> None:
    running = _session(store, status="running")
    waiting = _session(store, status="waiting")
    _session(store, status="idle")
    _session(store, status="failed")
    _session(store, status=None)
    _session(store, status="running", runner_id="runner-other")
    # The local cache holds the newest edge, in either direction.
    cache_running = _session(store, status="idle")
    orchestration._session_status_cache[cache_running] = "running"
    cache_idle = _session(store, status="running")
    orchestration._session_status_cache[cache_idle] = "idle"
    # A native parent's runtime drives a mirrored sub-agent's turn.
    _session(
        store,
        kind="sub_agent",
        parent_conversation_id=running,
        labels={_ACP_SUBAGENT_ID_LABEL_KEY: "acp-subagent-1"},
    )

    at_stake = await orchestration._runner_sessions_at_stake(_RUNNER_ID, store)

    assert {conv.id for conv in at_stake} == {running, waiting, cache_running}
    assert await _stake(store).turn_at_stake() is True


async def test_a_runner_with_nothing_mid_turn_has_nothing_at_stake(
    store: SqlAlchemyConversationStore,
) -> None:
    _session(store, status="idle")
    assert await _stake(store).turn_at_stake() is False
    assert await _stake(store).host_ids() == []
    assert await _stake(store).live_elsewhere() is False


@pytest.mark.parametrize("status", ["idle", "running"])
async def test_however_many_sessions_share_the_runner_the_reads_stay_capped(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setattr(orchestration, "_STAKE_PAGE_SIZE", 3)
    monkeypatch.setattr(orchestration, "_STAKE_MAX_PAGES", 2)
    monkeypatch.setattr(orchestration, "_STAKE_MAX_SESSIONS", 4)
    for _ in range(20):
        _session(store, status=status)
    reads = _Reads(
        store,
        monkeypatch,
        "list_runner_session_statuses",
        "list_conversations_by_runner_id",
        "get_conversation",
        "get_runner_liveness",
    )
    stake = _stake(store)

    for _ in range(5):  # the hold asks again on every recheck
        await stake.turn_at_stake()
        await stake.host_ids()
        await stake.live_elsewhere()

    pages = reads.calls["list_runner_session_statuses"]
    assert len(pages) <= 2, "at most two pages, and only on the first ask"
    assert all(page["limit"] == 3 for page in pages)
    assert reads.calls["list_conversations_by_runner_id"] == [], "never the whole session list"
    hydrated = len(reads.calls["get_conversation"])
    if status == "idle":
        assert hydrated == 0, "a session that is not mid-turn is never read in full"
        assert not await stake.turn_at_stake()
    else:
        assert hydrated == 4, "no more than the cap of mid-turn sessions are read in full"
        assert len(await orchestration._runner_sessions_at_stake(_RUNNER_ID, store)) == 4


async def test_the_sessions_are_read_once_and_each_recheck_reads_a_few_stamps(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(5):
        _session(store)
    reads = _Reads(
        store,
        monkeypatch,
        "list_runner_session_statuses",
        "get_conversation",
        "get_runner_liveness",
    )
    stake = _stake(store)

    for _ in range(4):
        assert await stake.turn_at_stake() is True
        await stake.host_ids()
        await stake.live_elsewhere()

    assert len(reads.calls["list_runner_session_statuses"]) == 1
    assert len(reads.calls["get_conversation"]) == 5, "each session is read in full once"
    witnesses = orchestration._STAKE_LIVENESS_WITNESSES
    assert len(reads.calls["get_runner_liveness"]) == 4 * witnesses


async def test_a_failed_read_is_not_remembered(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(store)
    real = store.list_runner_session_statuses
    failures: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> Any:
        if not failures:
            failures.append(1)
            raise RuntimeError("conversations table unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "list_runner_session_statuses", flaky)
    stake = _stake(store)

    with pytest.raises(RuntimeError):
        await stake.turn_at_stake()
    assert await stake.turn_at_stake() is True


async def test_hosts_come_from_the_sessions_at_stake_only(
    store: SqlAlchemyConversationStore,
) -> None:
    mid_turn = _session(store, host_id=_HOST_A)
    # Neither an idle session nor a mirror on another host says anything about the turn.
    _session(store, status="idle", host_id=_HOST_B)
    _session(
        store,
        host_id=_HOST_B,
        kind="sub_agent",
        parent_conversation_id=mid_turn,
        labels={_ACP_SUBAGENT_ID_LABEL_KEY: "acp-subagent-1"},
    )

    assert await _stake(store).host_ids() == [_HOST_A]


async def test_a_hostless_child_takes_the_host_of_its_rebound_parent(
    store: SqlAlchemyConversationStore,
) -> None:
    """The child stays on the old runner; its parent, with the host, moved to another."""
    parent = _session(store, status="idle", host_id=_HOST_B, runner_id="runner-rebound")
    _session(store, kind="sub_agent", parent_conversation_id=parent)

    stake = _stake(store)

    assert await stake.turn_at_stake() is True
    assert await stake.host_ids() == [_HOST_B]


async def test_a_session_without_any_host_leaves_the_hosts_unresolved(
    store: SqlAlchemyConversationStore,
) -> None:
    _session(store)
    stake = _stake(store)
    assert await stake.turn_at_stake() is True
    assert await stake.host_ids() == []


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("newer_than_ours", True),
        ("our_own", False),
        ("stale", False),
        ("never_stamped", False),
        ("session_moved_on", False),
    ],
)
async def test_live_elsewhere_needs_a_fresh_stamp_newer_than_this_replicas_own(
    store: SqlAlchemyConversationStore, stamp: str, expected: bool
) -> None:
    session_id = _session(store)
    now = int(time.time())
    ours = now - 5
    stake = _stake(store, reference=ours)
    assert await stake.turn_at_stake() is True

    if stamp == "newer_than_ours":
        store.touch_runner_liveness([_RUNNER_ID], now)
    elif stamp == "our_own":
        store.touch_runner_liveness([_RUNNER_ID], ours)
    elif stamp == "stale":
        store.touch_runner_liveness([_RUNNER_ID], now - 10_000)
    elif stamp == "session_moved_on":
        store.touch_runner_liveness([_RUNNER_ID], now)
        store.replace_runner_id(session_id, "runner-other")

    assert await stake.live_elsewhere() is expected
