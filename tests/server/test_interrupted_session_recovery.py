"""Tests for :mod:`omnigent.server.interrupted_session_recovery`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import Conversation
from omnigent.server.interrupted_session_recovery import (
    InterruptedSessionRecoverer,
    auto_resume_requested,
    init_started_turn,
    managed_host_ids,
    preserves_interruption,
)
from omnigent.server.managed_hosts import (
    ManagedSandboxAutoResumeConfig,
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
)
from omnigent.server.routes._sessions.common import (
    _intentional_stop_sessions,
    _session_status_cache,
)
from omnigent.stores.conversation_store import InterruptedSessionCursor
from omnigent.stores.host_store import Host

_DISCONNECTED = {
    "omnigent.last_task_error_code": "runner_disconnected",
    "omnigent.last_task_error_message": "Runner disconnected unexpectedly.",
}


def _deployment(
    *, enabled: bool = True, max_attempts: int = 3, max_age_s: int = 10**10
) -> ManagedSandboxDeployment:
    return ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://omnigent.example.com",
            launcher_factory=lambda: None,  # type: ignore[arg-type,return-value]
            token_ttl_s=60,
        ),
        auto_resume_interrupted=ManagedSandboxAutoResumeConfig(
            enabled=enabled,
            sweep_interval_s=60,
            max_attempts=max_attempts,
            max_interrupted_age_s=max_age_s,
        ),
    )


def _conv(
    conv_id: str,
    *,
    host_id: str | None = "host-1",
    parent: str | None = None,
    live_status: str | None = "failed",
    labels: dict[str, str] | None = None,
    updated_at: int = 0,
) -> Conversation:
    return Conversation(
        id=conv_id,
        created_at=0,
        updated_at=updated_at,
        root_conversation_id=parent or conv_id,
        parent_conversation_id=parent,
        agent_id="agent-1",
        runner_id="runner-old",
        host_id=host_id,
        live_status=live_status,
        labels=dict(_DISCONNECTED if labels is None else labels),
    )


def _host(host_id: str = "host-1", *, provider: str | None = "remote") -> Host:
    return Host(
        host_id=host_id,
        name=host_id,
        user_id="alice",
        status="offline",
        created_at=0,
        updated_at=0,
        sandbox_provider=provider,
    )


class _FakeHostStore:
    def __init__(self, hosts: list[Host]) -> None:
        self.hosts = {host.host_id: host for host in hosts}

    def get_host(self, host_id: str) -> Host | None:
        return self.hosts.get(host_id)


class _FakeConversationStore:
    def __init__(self, rows: list[tuple[int, Conversation]]) -> None:
        self.rows = rows
        self.queries: list[tuple[int, InterruptedSessionCursor | None, int]] = []

    def list_interrupted_host_sessions(
        self,
        *,
        stale_before: int,
        after: InterruptedSessionCursor | None,
        limit: int,
    ) -> tuple[list[tuple[int, Conversation]], InterruptedSessionCursor | None]:
        self.queries.append((stale_before, after, limit))
        start = 0 if after is None else after[0]
        page = self.rows[start : start + limit]
        more = start + limit < len(self.rows)
        return page, ((start + limit, 0, "") if more else None)


@pytest.fixture(autouse=True)
def _isolate_caches() -> Any:
    _session_status_cache.clear()
    _intentional_stop_sessions.clear()
    yield
    _session_status_cache.clear()
    _intentional_stop_sessions.clear()


@pytest.fixture
def woken(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []

    async def fake_ensure_runner_connected(
        *, session_id: str, conv: Conversation, **_: Any
    ) -> tuple[object, Conversation]:
        calls.append((current_workspace_id(), session_id))
        return object(), conv

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected",
        fake_ensure_runner_connected,
    )
    return calls


def _recoverer(
    rows: list[tuple[int, Conversation]],
    hosts: list[Host],
    *,
    max_attempts: int = 3,
    max_age_s: int = 10**10,
    scan_batch_size: int = 500,
) -> tuple[InterruptedSessionRecoverer, _FakeConversationStore]:
    store = _FakeConversationStore(rows)
    recoverer = InterruptedSessionRecoverer(
        conversation_store=store,  # type: ignore[arg-type]
        host_store=_FakeHostStore(hosts),  # type: ignore[arg-type]
        sandbox_config=_deployment(max_attempts=max_attempts, max_age_s=max_age_s),
        app_state=object(),
        runner_router=None,
        scan_batch_size=scan_batch_size,
    )
    return recoverer, store


async def test_sweep_pages_and_wakes_managed_sessions_in_their_workspace(
    woken: list[tuple[int, str]],
) -> None:
    rows = [
        (0, _conv("c1")),
        (7, _conv("c2", host_id="host-2")),
        (0, _conv("c3", host_id="host-local")),
        (0, _conv("c4", live_status="running", labels={})),
    ]
    recoverer, store = _recoverer(
        rows,
        [_host(), _host("host-2"), _host("host-local", provider=None)],
        scan_batch_size=2,
    )

    assert await recoverer.sweep_once(now=10_000) == 3
    assert sorted(woken) == [(0, "c1"), (0, "c4"), (7, "c2")]
    assert [query[1] for query in store.queries] == [None, (2, 0, "")]
    assert all(query[0] == 10_000 - 90 for query in store.queries)


async def test_sweep_skips_sessions_stopped_on_purpose_or_no_longer_interrupted(
    woken: list[tuple[int, str]],
) -> None:
    _intentional_stop_sessions["stopped"] = "runner-old"
    _session_status_cache["recovered"] = "idle"
    rows = [(0, _conv("stopped")), (0, _conv("recovered")), (0, _conv("live"))]
    recoverer, _ = _recoverer(rows, [_host()])

    assert await recoverer.sweep_once(now=10_000) == 1
    assert woken == [(0, "live")]


async def test_attempts_back_off_and_stop_at_the_cap(woken: list[tuple[int, str]]) -> None:
    recoverer, _ = _recoverer([(0, _conv("c1"))], [_host()], max_attempts=2)

    assert await recoverer.sweep_once(now=1_000) == 1
    # Backoff: the second attempt waits one sweep interval, the third two.
    assert await recoverer.sweep_once(now=1_059) == 0
    assert await recoverer.sweep_once(now=1_060) == 1
    assert await recoverer.sweep_once(now=10_000) == 0
    assert woken == [(0, "c1"), (0, "c1")]


async def test_attempts_reset_once_the_session_leaves_the_interrupted_set(
    woken: list[tuple[int, str]],
) -> None:
    recoverer, store = _recoverer([(0, _conv("c1"))], [_host()], max_attempts=1)
    assert await recoverer.sweep_once(now=1_000) == 1
    assert await recoverer.sweep_once(now=5_000) == 0

    rows = store.rows
    store.rows = []
    assert await recoverer.sweep_once(now=5_000) == 0
    store.rows = rows
    assert await recoverer.sweep_once(now=5_000) == 1
    assert woken == [(0, "c1"), (0, "c1")]


async def test_sweep_leaves_sessions_past_the_age_limit_alone(
    woken: list[tuple[int, str]],
) -> None:
    rows = [(0, _conv("old", updated_at=0)), (0, _conv("new", updated_at=100_000 - 3600))]
    recoverer, _ = _recoverer(rows, [_host()], max_age_s=3600)

    assert await recoverer.sweep_once(now=100_000) == 1
    assert woken == [(0, "new")]
    assert set(recoverer._attempts) == {(0, "new")}


async def test_sweep_ignores_failures_other_than_a_runner_disconnect(
    woken: list[tuple[int, str]],
) -> None:
    never_started = _conv(
        "never-started", labels={"omnigent.last_task_error_code": "runner_failed_to_start"}
    )
    recoverer, _ = _recoverer([(0, never_started)], [_host()])

    assert await recoverer.sweep_once(now=1_000) == 0
    assert woken == []


async def test_wake_failure_counts_as_an_attempt_and_does_not_stop_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def flaky(*, session_id: str, conv: Conversation, **_: Any) -> tuple[None, Conversation]:
        calls.append(session_id)
        if session_id == "broken":
            raise RuntimeError("provider down")
        return None, conv

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", flaky
    )
    rows = [(0, _conv("broken")), (0, _conv("other", host_id="host-2"))]
    recoverer, _ = _recoverer(rows, [_host(), _host("host-2")], max_attempts=1)

    assert await recoverer.sweep_once(now=1_000) == 2
    assert await recoverer.sweep_once(now=99_999) == 0
    assert sorted(calls) == ["broken", "other"]


@pytest.mark.parametrize(("status", "cleared"), [("running", []), ("idle", ["c1"])])
async def test_sweep_resends_the_continuation_to_an_already_connected_runner(
    woken: list[tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    cleared: list[str],
) -> None:
    """A declined continuation clears the disconnect; a started one leaves it to the turn."""
    calls: list[dict[str, Any]] = []
    recovered: list[str] = []

    class _Initializer:
        async def initialize(self, conv: Conversation, client: object, **kwargs: Any) -> Any:
            calls.append({"session_id": conv.id, **kwargs})
            return httpx.Response(201, json={"status": status}, request=httpx.Request("POST", "/"))

    async def record_recovered(session_id: str, store: object, **kwargs: Any) -> None:
        assert kwargs == {"require_disconnect_code": True}
        recovered.append(session_id)

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_runner_recovered_status", record_recovered
    )
    recoverer, _ = _recoverer([(0, _conv("c1"))], [_host()])
    recoverer._app_state = SimpleNamespace(runner_session_initializer=_Initializer())

    assert await recoverer.sweep_once(now=1_000) == 1
    assert calls == [
        {
            "session_id": "c1",
            "timeout": 10.0,
            "resume_interrupted_turn": True,
            "recovery_prompt": _deployment().auto_resume_interrupted.recovery_prompt,
        }
    ]
    assert recovered == cleared


async def test_one_failing_host_lookup_does_not_stop_the_sweep(
    woken: list[tuple[int, str]],
) -> None:
    class _FlakyHostStore(_FakeHostStore):
        def get_host(self, host_id: str) -> Host | None:
            if host_id == "host-broken":
                raise RuntimeError("database unavailable")
            return super().get_host(host_id)

    rows = [(0, _conv("broken", host_id="host-broken")), (0, _conv("ok"))]
    recoverer, _ = _recoverer(rows, [_host()])
    recoverer._host_store = _FlakyHostStore([_host()])  # type: ignore[assignment]

    assert await recoverer.sweep_once(now=1_000) == 1
    assert woken == [(0, "ok")]


async def test_start_and_shutdown_run_the_loop(woken: list[tuple[int, str]]) -> None:
    recoverer, _ = _recoverer([(0, _conv("c1"))], [_host()])
    await recoverer.start()
    await recoverer.start()
    for _ in range(50):
        if woken:
            break
        await asyncio.sleep(0.01)
    await recoverer.shutdown()
    await recoverer.shutdown()
    assert woken == [(0, "c1")]


def test_constructor_rejects_non_positive_limits() -> None:
    kwargs: dict[str, Any] = {
        "conversation_store": _FakeConversationStore([]),
        "host_store": _FakeHostStore([]),
        "sandbox_config": _deployment(),
        "app_state": object(),
        "runner_router": None,
    }
    with pytest.raises(ValueError, match="scan_batch_size"):
        InterruptedSessionRecoverer(**kwargs, scan_batch_size=0)
    with pytest.raises(ValueError, match="max_concurrent_hosts"):
        InterruptedSessionRecoverer(**kwargs, max_concurrent_hosts=0)


async def test_preserves_interruption_only_for_top_level_managed_sessions() -> None:
    hosts = _FakeHostStore([_host(), _host("host-local", provider=None)])

    async def check(conv: Conversation, config: ManagedSandboxDeployment | None) -> bool:
        return await preserves_interruption(conv, config, hosts)  # type: ignore[arg-type]

    assert await check(_conv("top"), _deployment())
    assert not await check(_conv("top"), _deployment(enabled=False))
    assert not await check(_conv("top"), None)
    assert not await check(_conv("child", parent="top"), _deployment())
    assert not await check(_conv("local", host_id=None), _deployment())
    assert not await check(_conv("self-hosted", host_id="host-local"), _deployment())
    assert not await preserves_interruption(
        _conv("old", updated_at=0),
        _deployment(max_age_s=60),
        hosts,
        now=1_000,  # type: ignore[arg-type]
    )
    _intentional_stop_sessions["stopped"] = "runner-old"
    assert not await check(_conv("stopped"), _deployment())
    assert not await preserves_interruption(_conv("top"), _deployment(), None)


def test_managed_host_ids_looks_up_each_distinct_host_once() -> None:
    lookups: list[str] = []

    class _CountingHostStore(_FakeHostStore):
        def get_host(self, host_id: str) -> Host | None:
            lookups.append(host_id)
            return super().get_host(host_id)

    store = _CountingHostStore([_host(), _host("host-local", provider=None)])
    ids = ["host-1", "host-1", "host-local", "missing", "host-1"]

    assert managed_host_ids(store, ids) == {"host-1"}  # type: ignore[arg-type]
    assert sorted(lookups) == ["host-1", "host-local", "missing"]


async def test_preserves_interruption_uses_precomputed_managed_hosts() -> None:
    class _NoLookupHostStore(_FakeHostStore):
        def get_host(self, host_id: str) -> Host | None:
            raise AssertionError("precomputed managed hosts should skip the lookup")

    store = _NoLookupHostStore([])
    assert await preserves_interruption(
        _conv("top"),
        _deployment(),
        store,  # type: ignore[arg-type]
        managed_hosts={"host-1"},
    )
    assert not await preserves_interruption(
        _conv("top"),
        _deployment(),
        store,  # type: ignore[arg-type]
        managed_hosts=set(),
    )


@pytest.mark.parametrize(
    ("conv", "hosts", "config", "expected"),
    [
        (_conv("c1"), [_host()], _deployment(), True),
        (_conv("c1", live_status="running", labels={}), [_host()], _deployment(), True),
        (_conv("c1"), [_host()], _deployment(enabled=False), False),
        (_conv("c1", live_status="idle", labels={}), [_host()], _deployment(), False),
        (
            _conv("c1", labels={"omnigent.last_task_error_code": "x"}),
            [_host()],
            _deployment(),
            False,
        ),
        (_conv("c1"), [_host(provider=None)], _deployment(), False),
        (_conv("c1"), [], _deployment(), False),
        (_conv("c1", parent="p"), [_host()], _deployment(), False),
        (
            _conv("c1", labels={"omnigent.last_task_error_code": "runner_failed_to_start"}),
            [_host()],
            _deployment(),
            False,
        ),
        (_conv("c1", updated_at=0), [_host()], _deployment(max_age_s=60), False),
        (_conv("c1", updated_at=950), [_host()], _deployment(max_age_s=60), True),
    ],
)
async def test_auto_resume_requested(
    conv: Conversation,
    hosts: list[Host],
    config: ManagedSandboxDeployment,
    expected: bool,
) -> None:
    host_store = _FakeHostStore(hosts)
    assert await auto_resume_requested(conv, config, host_store, now=1_000) is expected  # type: ignore[arg-type]
    assert await auto_resume_requested(conv, config, None, now=1_000) is False


async def test_auto_resume_requested_skips_sessions_stopped_on_purpose() -> None:
    _intentional_stop_sessions["c1"] = "runner-old"
    assert not await auto_resume_requested(
        _conv("c1"),
        _deployment(),
        _FakeHostStore([_host()]),  # type: ignore[arg-type]
        now=1_000,
    )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(201, json={"status": "running"}), True),
        (httpx.Response(201, json={"status": "idle"}), False),
        (httpx.Response(201, content=b"not json"), False),
        (httpx.Response(201, json=["running"]), False),
    ],
)
def test_init_started_turn(response: httpx.Response, expected: bool) -> None:
    assert init_started_turn(response) is expected
