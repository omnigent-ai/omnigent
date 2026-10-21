"""Tests for :mod:`omnigent.server.interrupted_session_recovery`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.db.utils import generate_agent_id, now_epoch
from omnigent.entities import Conversation, MessageData, NewConversationItem
from omnigent.onboarding.sandboxes.types import RestoredState, ResumeOutcome
from omnigent.server.interrupted_session_recovery import (
    InterruptedSessionRecoverer,
    auto_resume_requested,
    init_started_turn,
    managed_host_ids,
    managed_recovery_context,
    preserves_interruption,
)
from omnigent.server.managed_hosts import (
    ManagedLaunchTracker,
    ManagedSandboxAutoResumeConfig,
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
)
from omnigent.server.routes._sessions.common import (
    _intentional_stop_sessions,
    _session_status_cache,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import Host, HostStore

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
            provider="remote",
            launcher_factory=lambda: SimpleNamespace(
                provider="remote", background_recovery_context=lambda target: nullcontext()
            ),  # type: ignore[return-value]
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

    def get_session_recovery(self, session_id: str):
        return None

    def get_host(self, host_id: str) -> Host | None:
        return self.hosts.get(host_id)


@pytest.fixture(autouse=True)
def _isolate_caches():
    _session_status_cache.clear()
    _intentional_stop_sessions.clear()
    yield
    _session_status_cache.clear()
    _intentional_stop_sessions.clear()


@pytest.fixture
def recovery_case(db_uri: str, monkeypatch: pytest.MonkeyPatch):
    store = SqlAlchemyConversationStore(db_uri)
    hosts = HostStore(db_uri)
    now = now_epoch()
    initializer = SimpleNamespace(
        initialize=AsyncMock(
            return_value=httpx.Response(
                201, json={"status": "running"}, request=httpx.Request("POST", "/v1/sessions")
            )
        )
    )
    state = SimpleNamespace(
        runner_session_initializer=initializer, managed_launches=ManagedLaunchTracker()
    )
    config = _deployment(max_attempts=2)
    wakes = []

    def seed(*, workspace_id=0, host_id=None, status="failed", labels=None):
        with workspace_scope(workspace_id):
            host_id = host_id or uuid4().hex
            if hosts.get_host(host_id) is None:
                hosts.register_managed_host(
                    host_id=host_id,
                    name=host_id,
                    user_id="owner@example.test",
                    token="test",
                    provider="remote",
                    sandbox_id=f"sandbox-{host_id}",
                    token_expires_at=now + 3600,
                )
            conv = store.create_conversation(
                agent_id=generate_agent_id(),
                runner_id=uuid4().hex,
                host_id=host_id,
                workspace="/workspace",
            )
            store.set_session_live_status(conv.id, status)
            store.set_labels(conv.id, _DISCONNECTED if labels is None else labels)
            return store.get_conversation(conv.id)

    async def wake(*, conv, **kwargs):
        wakes.append((current_workspace_id(), conv.id))
        return Mock(spec=httpx.AsyncClient), conv

    def recoverer(**overrides):
        args = {
            "conversation_store": store,
            "host_store": hosts,
            "sandbox_config": config,
            "app_state": state,
            "runner_router": None,
            "clock": lambda: now,
            "scan_batch_size": 2,
        }
        args.update(overrides)
        return InterruptedSessionRecoverer(**args)

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", wake
    )
    return SimpleNamespace(
        store=store,
        hosts=hosts,
        now=now,
        initializer=initializer,
        state=state,
        config=config,
        wakes=wakes,
        seed=seed,
        recoverer=recoverer,
    )


async def test_scan_enters_authority_before_reading_session_content(recovery_case, monkeypatch):
    case = recovery_case
    rows = [case.seed(workspace_id=workspace) for workspace in (101, 202)]
    entered = ContextVar("test_recovery_identity", default=None)

    @asynccontextmanager
    async def authority(target):
        token = entered.set((target.workspace_id, target.session_id))
        try:
            yield
        finally:
            entered.reset(token)

    case.config.default.launcher_factory = lambda: SimpleNamespace(
        provider="remote", background_recovery_context=authority
    )
    original = case.store.get_conversation

    def authorized_read(session_id):
        assert entered.get() == (current_workspace_id(), session_id)
        return original(session_id)

    monkeypatch.setattr(case.store, "get_conversation", authorized_read)
    assert await case.recoverer().sweep_once(now=case.now) == 2
    assert set(case.wakes) == {(101, rows[0].id), (202, rows[1].id)}
    assert entered.get() is None


async def test_failed_replacement_keeps_budget_across_replicas_and_restart(
    recovery_case, monkeypatch
):
    case = recovery_case
    conv = case.seed()

    async def fail(*, conv, **kwargs):
        case.wakes.append(conv.id)
        case.store.set_labels(conv.id, {"omnigent.last_task_error_code": "runner_failed_to_start"})
        raise ConnectionError("replacement exited before connecting")

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", fail
    )
    assert await case.recoverer().sweep_once(now=case.now) == 1
    assert await case.recoverer().sweep_once(now=case.now + 59) == 0
    assert await case.recoverer().sweep_once(now=case.now + 60) == 1
    assert await case.recoverer().sweep_once(now=case.now + 600) == 0
    fresh = case.store.get_conversation(conv.id)
    assert await case.recoverer().resume_connected(fresh, Mock(spec=httpx.AsyncClient)) is None
    assert case.wakes == [conv.id, conv.id]
    record = case.hosts.get_session_recovery(conv.id)
    assert record.attempts == 2 and record.terminal_reason == "exhausted"
    assert "stopped after 2" in fresh.labels["omnigent.last_task_error_message"]


async def test_sessions_on_one_host_keep_separate_budgets(recovery_case, monkeypatch):
    case = recovery_case
    first = case.seed()
    second = case.seed(host_id=first.host_id)

    async def fail(**kwargs):
        raise ConnectionError("host down")

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", fail
    )
    assert await case.recoverer().sweep_once(now=case.now) == 2
    assert await case.recoverer().sweep_once(now=case.now + 60) == 2
    assert await case.recoverer().sweep_once(now=case.now + 600) == 0
    assert [case.hosts.get_session_recovery(row.id).attempts for row in (first, second)] == [2, 2]


@pytest.mark.parametrize("change", ["idle", "archive", "rebind", "stop", "delete", "generation"])
async def test_wake_rereads_eligibility_and_binding_before_continuing(
    recovery_case, monkeypatch, change
):
    case = recovery_case
    case.seed()

    async def race(*, conv, **kwargs):
        if change == "idle":
            case.store.set_session_live_status(conv.id, "idle")
        elif change == "archive":
            case.store.update_conversation(conv.id, archived=True)
        elif change == "rebind":
            case.store.replace_runner_id(conv.id, uuid4().hex)
        elif change == "delete":
            case.hosts.delete_host(conv.host_id)
        elif change == "generation":
            case.hosts.replace_managed_host_sandbox(
                host_id=conv.host_id,
                user_id="owner@example.test",
                token="replacement",
                provider="remote",
                sandbox_id="replacement-sandbox",
                token_expires_at=case.now + 3600,
            )
        else:
            _intentional_stop_sessions[conv.id] = conv.runner_id
        return Mock(spec=httpx.AsyncClient), conv

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", race
    )
    assert await case.recoverer().sweep_once(now=case.now) == 1
    case.initializer.initialize.assert_not_awaited()


async def test_reconnect_during_wake_joins_existing_attempt(recovery_case, monkeypatch):
    case = recovery_case
    conv = case.seed()
    worker = case.recoverer()
    claims = []

    async def wake(*, conv, **kwargs):
        assert await case.recoverer().resume_connected(conv, Mock(spec=httpx.AsyncClient)) is None
        claims.append(case.hosts.get_session_recovery(conv.id).claim_id)
        context = managed_recovery_context.get()
        await context.record_outcome(
            ResumeOutcome(workspace=RestoredState("partial", "repository"))
        )
        return Mock(spec=httpx.AsyncClient), conv

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", wake
    )
    assert await worker.sweep_once(now=case.now) == 1
    case.initializer.initialize.assert_awaited_once()
    kwargs = case.initializer.initialize.await_args.kwargs
    record = case.hosts.get_session_recovery(conv.id)
    assert kwargs["recovery_id"] == claims[0]
    assert record.claim_id is None
    assert '"status": "partial"' in kwargs["recovery_prompt"]
    assert '"scope": "repository"' in record.outcome_json


async def test_unknown_provider_does_not_read_content_or_wake(recovery_case, monkeypatch):
    case = recovery_case
    case.seed()
    case.config.default.launcher_factory = lambda: SimpleNamespace(
        provider="remote", background_recovery_context=lambda target: None
    )
    monkeypatch.setattr(
        case.store, "get_conversation", Mock(side_effect=AssertionError("not authorized"))
    )
    assert await case.recoverer().sweep_once(now=case.now) == 0
    assert case.wakes == []


async def test_duplicate_scan_row_cannot_exceed_attempt_limit(recovery_case, monkeypatch):
    case = recovery_case
    conv = case.seed()
    case.config.auto_resume_interrupted = ManagedSandboxAutoResumeConfig(
        enabled=True, max_attempts=1
    )
    monkeypatch.setattr(
        case.store,
        "list_interrupted_host_session_candidates",
        Mock(return_value=([(0, conv.id, conv.host_id), (0, conv.id, conv.host_id)], None)),
    )
    assert await case.recoverer().sweep_once(now=case.now) == 1
    assert len(case.wakes) == 1


async def test_completed_and_never_started_sessions_stay_stopped(recovery_case):
    case = recovery_case
    case.seed(status="idle", labels={})
    case.seed(labels={"omnigent.last_task_error_code": "runner_failed_to_start"})
    assert await case.recoverer().sweep_once(now=case.now) == 0
    case.initializer.initialize.assert_not_awaited()


async def test_server_restart_preserves_cap_but_new_user_work_gets_budget(
    recovery_case, monkeypatch
):
    case = recovery_case
    conv = case.seed()
    case.config.auto_resume_interrupted = ManagedSandboxAutoResumeConfig(
        enabled=True, max_attempts=1
    )

    async def fail(**kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", fail
    )
    assert await case.recoverer().sweep_once(now=case.now) == 1
    case.store.append(
        conv.id,
        [
            NewConversationItem(
                response_id=uuid4().hex,
                type="message",
                data=MessageData(role="assistant", agent="test-agent", content=[]),
            )
        ],
    )
    assert await case.recoverer().sweep_once(now=case.now + 600) == 0
    case.store.append(
        conv.id,
        [
            NewConversationItem(
                response_id=uuid4().hex,
                type="message",
                data=MessageData(role="user", content=[]),
            )
        ],
    )
    assert await case.recoverer().sweep_once(now=case.now + 600) == 0
    case.store.append(
        conv.id,
        [
            NewConversationItem(
                response_id=uuid4().hex,
                type="message",
                data=MessageData(role="user", content=[], user_authored=True),
            )
        ],
    )
    assert await case.recoverer().sweep_once(now=case.now + 601) == 1


async def test_start_shutdown_and_invalid_limits(recovery_case):
    case = recovery_case
    worker = case.recoverer()
    await worker.start()
    await worker.start()
    await worker.shutdown()
    await worker.shutdown()
    assert worker._task is None
    for key in ("scan_batch_size", "max_concurrent_hosts"):
        with pytest.raises(ValueError, match=key):
            case.recoverer(**{key: 0})


async def test_queued_claim_uses_current_time(recovery_case, monkeypatch):
    case = recovery_case
    conv = case.seed()
    later = case.now + 600

    async def wake(**kwargs):
        record = case.hosts.get_session_recovery(conv.id)
        assert record.lease_until > later
        return Mock(spec=httpx.AsyncClient), conv

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", wake
    )
    assert await case.recoverer(clock=lambda: later).sweep_once(now=case.now) == 1


async def test_reconnect_retains_outcome_from_failed_runner_start(recovery_case, monkeypatch):
    case = recovery_case
    conv = case.seed()

    async def wake(**kwargs):
        await managed_recovery_context.get().record_outcome(
            ResumeOutcome(workspace=RestoredState("partial", "repository"))
        )
        raise ConnectionError("runner failed after workspace restoration")

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", wake
    )
    assert await case.recoverer().sweep_once(now=case.now) == 1
    response = await case.recoverer(clock=lambda: case.now + 60).resume_connected(
        conv, Mock(spec=httpx.AsyncClient)
    )
    assert response.status_code == 201
    assert (
        '"status": "partial"' in case.initializer.initialize.await_args.kwargs["recovery_prompt"]
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_ambiguous_attempt_stops_and_drains_launch(recovery_case, monkeypatch, cancel):
    case = recovery_case
    conv = case.seed()
    started = asyncio.Event()
    launch = case.state.managed_launches.begin(conv.id)

    async def wake(**kwargs):
        launch.task = asyncio.create_task(asyncio.Event().wait())
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", wake
    )
    monkeypatch.setattr(
        "omnigent.server.interrupted_session_recovery._ATTEMPT_TIMEOUT_S", 1 if cancel else 0.05
    )
    task = asyncio.create_task(case.recoverer().sweep_once(now=case.now))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == 1
    assert launch.task.done() and launch.settled.is_set()
    record = case.hosts.get_session_recovery(conv.id)
    assert record.terminal_reason == "outcome_unknown"
    assert await case.recoverer().sweep_once(now=case.now + 600) == 0
    case.initializer.initialize.assert_not_awaited()


async def test_long_provider_refusal_is_terminal(recovery_case, monkeypatch):
    from omnigent.onboarding.sandboxes.base import SandboxRecoveryRefusedError

    case = recovery_case
    conv = case.seed()

    async def refuse(**kwargs):
        raise SandboxRecoveryRefusedError("No authority. " * 100)

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", refuse
    )
    assert await case.recoverer().sweep_once(now=case.now) == 1
    record = case.hosts.get_session_recovery(conv.id)
    assert record.terminal_reason == "refused" and record.claim_id is None
    assert await case.recoverer().sweep_once(now=case.now + 600) == 0


async def test_lost_claim_cannot_publish_failure(recovery_case, monkeypatch):
    case = recovery_case
    conv = case.seed()
    case.config.auto_resume_interrupted = ManagedSandboxAutoResumeConfig(
        enabled=True, max_attempts=1
    )

    async def fail(**kwargs):
        raise ConnectionError("old attempt")

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", fail
    )
    monkeypatch.setattr(case.hosts, "finish_session_recovery", Mock(return_value=False))
    assert await case.recoverer().sweep_once(now=case.now) == 1
    assert case.store.get_conversation(conv.id).labels == conv.labels


async def test_exhaustion_becomes_visible_after_liveness_grace(recovery_case):
    case = recovery_case
    conv = case.seed(status="running")
    _session_status_cache[conv.id] = "running"
    worker = case.recoverer()
    claim = case.hosts.claim_session_recovery(
        conv.host_id,
        conv.id,
        await worker._episode(conv.id),
        now=case.now,
        lease_s=180,
        max_attempts=1,
        retry_backoff_s=1,
    )
    case.hosts.finish_session_recovery(conv.id, claim, terminal_reason="exhausted")
    case.store.touch_runner_liveness([conv.runner_id], now=case.now)
    assert await worker.resume_connected(conv, Mock(spec=httpx.AsyncClient)) is None
    assert case.store.get_conversation(conv.id).live_status == "running"
    assert await case.recoverer(clock=lambda: case.now + 120).sweep_once() == 0
    fresh = case.store.get_conversation(conv.id)
    assert fresh.live_status == "failed"
    assert _session_status_cache[conv.id] == "failed"
    assert "stopped after 1 attempts" in fresh.labels["omnigent.last_task_error_message"]
    assert case.hosts.get_session_recovery(conv.id).attempts == 1
    case.initializer.initialize.assert_not_awaited()


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
