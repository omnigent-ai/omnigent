"""The persistent-dispatch executor: claimed runs through the shared fire path, and recovery.

Leases, claims, and run rows use the real store. Hosts, sessions, and sandboxes
are fakes, as in ``test_fire.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.entities import ScheduledTask, ScheduledTaskRun
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.managed_hosts import ManagedLaunchTracker
from omnigent.server.scheduled import fire as fire_mod
from omnigent.server.scheduled.dispatch_scheduler import (
    FireIdentityRevoked,
    PersistentTaskScheduler,
    StartupLease,
    StartupLeaseLost,
)
from omnigent.server.scheduled.fire import ClaimedRunExecutor, FireDeps
from omnigent.server.scheduled.schedule import occurrence_host_id, scheduled_trigger_key
from omnigent.server.startup_inputs import STARTUP_LABEL
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.scheduled.test_fire import (
    FakeAgentStore,
    FakeConversationStore,
    FakeHostStore,
    FakePermissionStore,
    FakeProjectStore,
    FakeStartupInputs,
    _drain_managed_launches,
    _FakeAgent,
    _FakeConversation,
    _FakeHost,
    _managed_sandbox_config,
)
from tests.server.scheduled.test_scheduler import FakeClock

_LEASE_SECONDS = 120
# The real store keeps these as 16-byte UUIDs.
_AGENT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "agent").hex
_HOST_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "host").hex
_CONV_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "conversation").hex


class _ConversationStore(FakeConversationStore):
    """Mints UUID session ids and remembers host bindings a managed launch writes."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.hosts: dict[str, str] = {}

    def create_conversation(self, **kwargs: Any) -> _FakeConversation:
        conv = super().create_conversation(**kwargs)
        conv.id = uuid.uuid4().hex
        self.ids.append(conv.id)
        return conv

    def get_conversation(self, conversation_id: str) -> _FakeConversation | None:
        return _FakeConversation(
            id=conversation_id, agent_id=_AGENT_ID, host_id=self.hosts.get(conversation_id)
        )


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyScheduledTaskStore:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    return store


@dataclass
class _Claimed:
    task: ScheduledTask
    lease: StartupLease
    clock: FakeClock


def _claim(store: SqlAlchemyScheduledTaskStore, **kwargs: Any) -> _Claimed:
    """Create a due task and claim its first occurrence, as the scheduler's poll would."""
    kwargs.setdefault("workspace", "/repo")
    kwargs.setdefault("host_id", _HOST_ID)
    created = store.create(
        uuid.uuid4().hex,
        "nightly",
        "do the thing",
        "FREQ=HOURLY",
        None,
        _AGENT_ID,
        "UTC",
        **kwargs,
    )
    task = store.get(created.id)
    assert task is not None and task.next_run_at is not None
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=occurrence_host_id(task, scheduled_trigger_key(task.next_run_at)),
        lease_seconds=_LEASE_SECONDS,
    )
    assert run is not None
    lease = StartupLease(store, run, now=clock.now, lease_seconds=_LEASE_SECONDS)
    return _Claimed(task=task, lease=lease, clock=clock)


def _deps(store: SqlAlchemyScheduledTaskStore, **overrides: Any) -> FireDeps:
    return FireDeps(
        scheduled_task_store=store,
        agent_store=FakeAgentStore({_AGENT_ID: _FakeAgent(_AGENT_ID)}),
        conversation_store=overrides.get("conversation_store", _ConversationStore()),
        permission_store=FakePermissionStore(),
        host_store=overrides.get("host_store", FakeHostStore()),
        host_registry=None,
        sandbox_config=overrides.get("sandbox_config"),
        managed_launches=overrides.get("managed_launches"),
        startup_inputs=overrides.get("startup_inputs"),
        project_store=overrides.get("project_store"),
    )


def _runs(store: SqlAlchemyScheduledTaskStore, task: ScheduledTask) -> list[ScheduledTaskRun]:
    return store.list_runs(task.id)[0]


# ── Starting a claimed run ───────────────────────────────────────────────────


async def test_connected_fire_runs_on_the_claimed_run(store) -> None:
    claimed = _claim(store)
    conversations = _ConversationStore()
    seen: list[ScheduledTaskRun] = []

    async def dispatch(conv: Any, task: ScheduledTask) -> None:
        seen.append(store.list_runs(task.id)[0][0])

    executor = ClaimedRunExecutor(
        _deps(store, conversation_store=conversations), launch_dispatch=dispatch
    )
    await executor.fire(claimed.task, claimed.lease)

    [session_id] = conversations.ids
    # The session is on the run before its prompt is sent, so recovery can find it.
    assert [(run.status, run.conversation_id) for run in seen] == [("starting", session_id)]
    [run] = _runs(store, claimed.task)
    assert run.id == claimed.lease.run.id
    assert (run.status, run.conversation_id, run.lease_expires_at) == ("running", session_id, None)
    task = store.get(claimed.task.id)
    assert task.last_run_conversation_id == session_id
    assert task.last_run_at is not None


async def test_launch_failure_fails_the_claimed_run(store) -> None:
    claimed = _claim(store)
    executor = ClaimedRunExecutor(
        _deps(store), launch_dispatch=AsyncMock(side_effect=RuntimeError("runner gone"))
    )

    await executor.fire(claimed.task, claimed.lease)

    [run] = _runs(store, claimed.task)
    assert (run.status, run.error_code) == ("failed", "launch_failed")
    assert run.conversation_id is not None
    assert run.finished_at is not None


async def test_unsupported_target_skips_the_claimed_run(store) -> None:
    claimed = _claim(store)
    claimed.task.execution_target = "elsewhere"
    dispatch = AsyncMock()

    await ClaimedRunExecutor(_deps(store), launch_dispatch=dispatch).fire(
        claimed.task, claimed.lease
    )

    [run] = _runs(store, claimed.task)
    assert (run.status, run.error_code) == ("skipped", "unsupported_target")
    dispatch.assert_not_awaited()


async def test_lost_lease_stops_before_the_prompt_is_sent(store) -> None:
    claimed = _claim(store)
    conversations = _ConversationStore()
    create = conversations.create_conversation

    def create_while_lease_expires(**kwargs: Any) -> Any:
        # Startup stalls past its lease, e.g. a slow database during a rollout.
        claimed.clock.advance(_LEASE_SECONDS + 1)
        return create(**kwargs)

    conversations.create_conversation = create_while_lease_expires
    dispatch = AsyncMock()
    executor = ClaimedRunExecutor(
        _deps(store, conversation_store=conversations), launch_dispatch=dispatch
    )

    with pytest.raises(StartupLeaseLost):
        await executor.fire(claimed.task, claimed.lease)

    dispatch.assert_not_awaited()
    # Left for recovery to decide.
    assert _runs(store, claimed.task)[0].status == "starting"


async def test_fire_runs_inside_the_owner_identity(store) -> None:
    claimed = _claim(store)
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def identity(task: ScheduledTask) -> AsyncIterator[None]:
        assert task.id == claimed.task.id
        events.append("enter")
        yield
        events.append("exit")

    async def dispatch(conv: Any, task: ScheduledTask) -> None:
        events.append("dispatch")

    executor = ClaimedRunExecutor(_deps(store), fire_identity=identity, launch_dispatch=dispatch)
    await executor.fire(claimed.task, claimed.lease)

    assert events == ["enter", "dispatch", "exit"]
    assert _runs(store, claimed.task)[0].status == "running"


async def test_claimed_fire_files_the_session_into_the_task_project(store) -> None:
    project_id = uuid.uuid5(uuid.NAMESPACE_DNS, "project").hex
    claimed = _claim(store, project_id=project_id)
    conversations = _ConversationStore()
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            project_store=FakeProjectStore({(project_id, RESERVED_USER_LOCAL)}),
        ),
        launch_dispatch=AsyncMock(),
    )

    await executor.fire(claimed.task, claimed.lease)

    [created] = conversations.created
    assert created["project_id"] == project_id
    assert _runs(store, claimed.task)[0].status == "running"


def _failing_identity(error: Exception) -> Any:
    @contextlib.asynccontextmanager
    async def identity(task: ScheduledTask) -> AsyncIterator[None]:
        raise error
        yield

    return identity


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (FireIdentityRevoked("delegation revoked"), "reauthorization_required"),
        (ConnectionError("token service unavailable"), "owner_identity_unavailable"),
    ],
)
async def test_owner_identity_failure_fails_before_creating_a_session(
    store, error, error_code
) -> None:
    claimed = _claim(store)
    conversations = _ConversationStore()
    executor = ClaimedRunExecutor(
        _deps(store, conversation_store=conversations),
        fire_identity=_failing_identity(error),
        launch_dispatch=AsyncMock(),
    )
    await executor.fire(claimed.task, claimed.lease)

    [run] = _runs(store, claimed.task)
    assert (run.status, run.error_code) == ("failed", error_code)
    assert str(error) not in (run.error or "")
    assert conversations.created == []
    assert store.get(claimed.task.id).state == "active"


async def test_revoked_identity_of_a_deleted_owner_disables_the_task(store, monkeypatch) -> None:
    claimed = _claim(store)
    monkeypatch.setattr(fire_mod, "_owner_is_deleted", AsyncMock(return_value=True))
    finishes: list[tuple[str, str | None]] = []
    finish = claimed.lease.finish

    async def record_finish(status: str, **kwargs: Any) -> None:
        finishes.append((status, kwargs.get("error_code")))
        await finish(status, **kwargs)

    claimed.lease.finish = record_finish
    executor = ClaimedRunExecutor(
        _deps(store),
        fire_identity=_failing_identity(FireIdentityRevoked("owner removed")),
        launch_dispatch=AsyncMock(),
    )
    await executor.fire(claimed.task, claimed.lease)

    assert finishes == [("failed", "owner_deleted")]
    # The disabled task's finished history is purged with it.
    assert store.get(claimed.task.id) is None


@pytest.mark.parametrize("outcome", ["bound", "failed_after_binding", "tracker_failed", "raised"])
async def test_managed_fire_hands_off_once_the_sandbox_host_is_bound(
    store, monkeypatch, outcome: str
) -> None:
    from omnigent.server.routes import sessions

    claimed = _claim(store, execution_target="managed_sandbox", host_id=None, workspace=None)
    calls: list[str] = []
    startup = FakeStartupInputs(calls)
    conversations = _ConversationStore()
    runner_started = asyncio.Event()
    launched_host_ids: list[str | None] = []

    async def launch(**kwargs: Any) -> None:
        calls.append("launch")
        session_id, tracker = kwargs["session_id"], kwargs["tracker"]
        launched_host_ids.append(kwargs["host_id"])
        if outcome == "raised":
            raise RuntimeError("allocation failed")
        if outcome == "tracker_failed":
            tracker.fail(session_id, "provider refused")
            return
        conversations.hosts[session_id] = kwargs["host_id"]
        tracker.get(session_id).bound.set()
        if outcome == "failed_after_binding":
            # The runner launch fails before the executor records the handoff.
            tracker.fail(session_id, "harness not configured on the sandbox host")
            return
        # Startup is handed off before the runner connects.
        await runner_started.wait()
        tracker.finish(session_id)

    monkeypatch.setattr(sessions, "_run_managed_launch", AsyncMock(side_effect=launch))
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            sandbox_config=_managed_sandbox_config(),
            managed_launches=ManagedLaunchTracker(),
            startup_inputs=startup,
        )
    )
    try:
        await asyncio.wait_for(executor.fire(claimed.task, claimed.lease), 10)
        [run] = _runs(store, claimed.task)
    finally:
        runner_started.set()
        await _drain_managed_launches()

    assert calls == ["enqueue", "launch"]
    # The sandbox registers under the run's host ID, so its connection routes back here.
    assert launched_host_ids == [run.host_id]
    assert run.host_id == occurrence_host_id(claimed.task, scheduled_trigger_key(run.scheduled_at))
    if outcome == "bound":
        assert (run.status, run.conversation_id) == ("running", conversations.ids[0])
        assert startup.launch_failures == []
    else:
        # The failure stage: creating the sandbox, or starting its runner once bound.
        error_code, reason = {
            "failed_after_binding": ("runner_start_failed", "harness not configured"),
            "tracker_failed": ("sandbox_create_failed", "provider refused"),
            "raised": ("sandbox_create_failed", "allocation failed"),
        }[outcome]
        assert (run.status, run.error_code) == ("failed", error_code)
        assert reason in (run.error or "")


async def test_sandbox_that_never_registers_is_abandoned_and_torn_down(store, monkeypatch) -> None:
    from omnigent.server import managed_hosts
    from omnigent.server.routes import sessions

    claimed = _claim(store, execution_target="managed_sandbox", host_id=None, workspace=None)
    host_id = claimed.lease.run.host_id
    terminate = AsyncMock()
    monkeypatch.setattr(managed_hosts, "terminate_managed_host", terminate)
    startup = FakeStartupInputs()
    cancelled = asyncio.Event()

    async def launch(**kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()  # Provisioning never finishes.
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(sessions, "_run_managed_launch", AsyncMock(side_effect=launch))
    hosts = FakeHostStore({host_id: _FakeHost(host_id, "local", sandbox_provider="agent_sandbox")})
    executor = ClaimedRunExecutor(
        _deps(
            store,
            host_store=hosts,
            sandbox_config=_managed_sandbox_config(),
            managed_launches=ManagedLaunchTracker(),
            startup_inputs=startup,
        ),
        registration_timeout_s=0.2,
    )
    try:
        await asyncio.wait_for(executor.fire(claimed.task, claimed.lease), 10)
    finally:
        await _drain_managed_launches()

    [run] = _runs(store, claimed.task)
    assert (run.status, run.error_code) == ("failed", "sandbox_registration_timeout")
    assert cancelled.is_set()
    assert terminate.await_args.args[0] is hosts.hosts[host_id]
    # The saved prompt fails with the timeout first. The cancelled launch's later
    # attempt finds nothing pending, so a generic shutdown reason never shows.
    assert "did not register within 0.2 seconds" in startup.launch_failures[0][1]


async def test_lost_lease_during_the_binding_wait_leaves_the_launch_running(
    store, monkeypatch
) -> None:
    from omnigent.server.routes import sessions

    claimed = _claim(store, execution_target="managed_sandbox", host_id=None, workspace=None)
    conversations = _ConversationStore()
    provisioning = asyncio.Event()
    provisioned = asyncio.Event()
    bound: list[str] = []

    async def launch(**kwargs: Any) -> None:
        provisioning.set()
        await provisioned.wait()
        conversations.hosts[kwargs["session_id"]] = kwargs["host_id"]
        bound.append(kwargs["session_id"])
        kwargs["tracker"].finish(kwargs["session_id"])

    monkeypatch.setattr(sessions, "_run_managed_launch", AsyncMock(side_effect=launch))
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            sandbox_config=_managed_sandbox_config(),
            managed_launches=ManagedLaunchTracker(),
            startup_inputs=FakeStartupInputs(),
        )
    )
    startup = asyncio.create_task(executor.fire(claimed.task, claimed.lease))
    try:
        await asyncio.wait_for(provisioning.wait(), 10)
        # The scheduler cancels local startup when its lease is lost.
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        provisioned.set()
    finally:
        provisioned.set()
        await _drain_managed_launches()

    # The launch finished binding on its own; recovery decides the run.
    assert bound == conversations.ids
    assert _runs(store, claimed.task)[0].status == "starting"


# ── Recovering an interrupted startup ────────────────────────────────────────


@dataclass
class _Item:
    status: str


def _item(type: str, role: str | None = None) -> Any:
    return SimpleNamespace(type=type, data=SimpleNamespace(role=role))


# Transcript items, newest first, as recovery reads them.
_RUNNER_STARTED = _item("resource_event")
_PROMPT = _item("message", "user")
_REPLY = _item("message", "assistant")
_TOOL_CALL = _item("function_call")


@dataclass
class _RecoveryConversationStore:
    """Serves one saved session's state for recovery to inspect."""

    conversation: _FakeConversation | None
    live_status: str | None = None
    items: list[object] = field(default_factory=list)
    pending: list[_Item] = field(default_factory=list)

    def get_conversation(self, conversation_id: str) -> Any:
        if self.conversation is None:
            return None
        return SimpleNamespace(
            id=self.conversation.id,
            host_id=self.conversation.host_id,
            labels=self.conversation.labels,
            live_status=self.live_status,
        )

    def list_items(
        self, conversation_id: str, limit: int = 100, order: str = "asc", **_: Any
    ) -> Any:
        assert order == "desc"
        return SimpleNamespace(data=self.items[:limit])

    def list_pending_inputs(self, conversation_id: str) -> list[_Item]:
        return self.pending


def _expired_run(
    task: ScheduledTask, *, conversation_id: str | None = _CONV_ID
) -> ScheduledTaskRun:
    expired_at = int(time.time()) - 1
    trigger_key = scheduled_trigger_key(expired_at - 120)
    return ScheduledTaskRun(
        id=uuid.uuid4().hex,
        scheduled_task_id=task.id,
        status="starting",
        scheduled_at=expired_at - 120,
        conversation_id=conversation_id,
        trigger_key=trigger_key,
        host_id=occurrence_host_id(task, trigger_key),
        lease_expires_at=expired_at,
    )


def _saved_task(store: SqlAlchemyScheduledTaskStore, **kwargs: Any) -> ScheduledTask:
    if kwargs.get("execution_target") != "managed_sandbox":
        kwargs.setdefault("workspace", "/repo")
        kwargs.setdefault("host_id", _HOST_ID)
    task = store.create(
        uuid.uuid4().hex,
        "nightly",
        "do the thing",
        "FREQ=HOURLY",
        None,
        _AGENT_ID,
        "UTC",
        **kwargs,
    )
    return store.get(task.id)


async def test_recovery_fails_a_run_that_never_created_its_session(store) -> None:
    task = _saved_task(store)
    executor = ClaimedRunExecutor(_deps(store))

    assert await executor.recover(_expired_run(task, conversation_id=None)) == "failed"


@pytest.mark.parametrize(
    ("items", "live_status", "expected"),
    [
        ([], None, "failed"),
        # The runner connected but never got the prompt.
        ([_RUNNER_STARTED], "idle", "failed"),
        ([_PROMPT, _RUNNER_STARTED], None, "running"),
        ([_PROMPT, _RUNNER_STARTED], "running", "running"),
        ([], "waiting", "running"),
        # Saved but never run: the replica died as it sent the prompt.
        ([_PROMPT, _RUNNER_STARTED], "idle", "running"),
        ([_REPLY, _PROMPT, _RUNNER_STARTED], "idle", "succeeded"),
        ([_RUNNER_STARTED, _REPLY, _PROMPT], "idle", "succeeded"),
        ([_TOOL_CALL, _REPLY, _PROMPT], "idle", "running"),
        ([_PROMPT], "failed", "failed"),
    ],
)
async def test_recovery_decides_a_connected_host_run_from_its_session(
    store, items, live_status, expected
) -> None:
    task = _saved_task(store)
    conversations = _RecoveryConversationStore(
        _FakeConversation(id=_CONV_ID, agent_id=_AGENT_ID, host_id=_HOST_ID),
        live_status=live_status,
        items=items,
    )
    executor = ClaimedRunExecutor(_deps(store, conversation_store=conversations))

    assert await executor.recover(_expired_run(task)) == expected


def _sandbox_hosts(task: ScheduledTask, run: ScheduledTaskRun, provider: str | None) -> Any:
    return FakeHostStore({run.host_id: _FakeHost(run.host_id, "local", sandbox_provider=provider)})


@pytest.mark.parametrize(
    ("pending", "items", "live_status", "expected"),
    [
        ([_Item("in_progress")], [], None, "running"),
        ([_Item("incomplete")], [], "running", "running"),
        ([], [_PROMPT], "running", "running"),
        ([], [_PROMPT], "idle", "running"),
        ([], [_REPLY, _PROMPT], "idle", "succeeded"),
        ([], [_PROMPT], "failed", "failed"),
    ],
)
async def test_recovery_keeps_a_bound_managed_run_and_reads_its_turn(
    store, monkeypatch, pending, items, live_status, expected
) -> None:
    from omnigent.server import managed_hosts

    terminate = AsyncMock()
    monkeypatch.setattr(managed_hosts, "terminate_managed_host", terminate)
    task = _saved_task(store, execution_target="managed_sandbox")
    run = _expired_run(task)
    conversations = _RecoveryConversationStore(
        _FakeConversation(id=_CONV_ID, agent_id=_AGENT_ID, host_id=run.host_id),
        live_status=live_status,
        items=items,
        pending=pending,
    )
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            host_store=_sandbox_hosts(task, run, "agent_sandbox"),
        )
    )

    assert await executor.recover(run) == expected
    terminate.assert_not_awaited()


async def test_recovery_waits_while_this_server_still_provisions_the_sandbox(store) -> None:
    task = _saved_task(store, execution_target="managed_sandbox")
    tracker = ManagedLaunchTracker()
    tracker.begin(_CONV_ID)
    conversations = _RecoveryConversationStore(_FakeConversation(id=_CONV_ID, agent_id=_AGENT_ID))
    executor = ClaimedRunExecutor(
        _deps(store, conversation_store=conversations, managed_launches=tracker)
    )

    assert await executor.recover(_expired_run(task)) is None


@pytest.mark.parametrize("provider", ["agent_sandbox", None])
async def test_recovery_cleans_up_an_unbound_managed_run(store, monkeypatch, provider) -> None:
    from omnigent.server import managed_hosts

    terminate = AsyncMock()
    monkeypatch.setattr(managed_hosts, "terminate_managed_host", terminate)
    task = _saved_task(store, execution_target="managed_sandbox")
    run = _expired_run(task)
    startup = FakeStartupInputs()
    hosts = _sandbox_hosts(task, run, provider)
    conversations = _RecoveryConversationStore(
        _FakeConversation(id=_CONV_ID, agent_id=_AGENT_ID, labels={STARTUP_LABEL: "pending"})
    )
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            host_store=hosts,
            managed_launches=ManagedLaunchTracker(),
            startup_inputs=startup,
        )
    )

    assert await executor.recover(run) == "failed"
    assert [session for session, _ in startup.launch_failures] == [_CONV_ID]
    # Only a server-managed sandbox is torn down, never a user's own host.
    if provider is None:
        terminate.assert_not_awaited()
    else:
        assert terminate.await_args.args[0] is hosts.hosts[run.host_id]


async def test_recovery_cleans_up_the_sandbox_of_a_deleted_session(store, monkeypatch) -> None:
    from omnigent.server import managed_hosts

    terminate = AsyncMock()
    monkeypatch.setattr(managed_hosts, "terminate_managed_host", terminate)
    task = _saved_task(store, execution_target="managed_sandbox")
    run = _expired_run(task)
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=_RecoveryConversationStore(None),
            host_store=_sandbox_hosts(task, run, "agent_sandbox"),
        )
    )

    assert await executor.recover(run) == "failed"
    terminate.assert_awaited_once()


def _draining_sandbox_config(shutdown_drain_s: float) -> Any:
    from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment

    return ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.com",
            launcher_factory=lambda: pytest.fail("recovery never launches a sandbox"),
            token_ttl_s=90000,
        ),
        shutdown_drain_s=shutdown_drain_s,
    )


async def test_recovery_fails_a_failed_managed_launch_and_tears_down_its_sandbox(
    store, monkeypatch
) -> None:
    from omnigent.server import managed_hosts

    terminate = AsyncMock()
    monkeypatch.setattr(managed_hosts, "terminate_managed_host", terminate)
    task = _saved_task(store, execution_target="managed_sandbox")
    run = _expired_run(task)
    # Still inside another replica's shutdown drain: a known failure doesn't wait it out.
    clock = FakeClock(run.lease_expires_at + 1)
    # The sandbox bound after its prompt had already failed, so it can never deliver it.
    conversations = _RecoveryConversationStore(
        _FakeConversation(
            id=_CONV_ID, agent_id=_AGENT_ID, host_id=run.host_id, labels={STARTUP_LABEL: "failed"}
        )
    )
    startup = FakeStartupInputs()
    hosts = _sandbox_hosts(task, run, "agent_sandbox")
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            host_store=hosts,
            sandbox_config=_draining_sandbox_config(30),
            startup_inputs=startup,
        ),
        now=clock.now,
    )

    assert await executor.recover(run) == "failed"
    assert startup.launch_failures == []
    assert terminate.await_args.args[0] is hosts.hosts[run.host_id]


async def test_recovery_waits_out_another_replicas_shutdown_drain(store, monkeypatch) -> None:
    from omnigent.server import managed_hosts

    terminate = AsyncMock()
    monkeypatch.setattr(managed_hosts, "terminate_managed_host", terminate)
    task = _saved_task(store, execution_target="managed_sandbox")
    run = _expired_run(task)
    config = _draining_sandbox_config(30)
    clock = FakeClock(run.lease_expires_at + 10)
    startup = FakeStartupInputs()
    conversations = _RecoveryConversationStore(
        _FakeConversation(id=_CONV_ID, agent_id=_AGENT_ID, labels={STARTUP_LABEL: "pending"})
    )
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            host_store=_sandbox_hosts(task, run, "agent_sandbox"),
            sandbox_config=config,
            managed_launches=ManagedLaunchTracker(),
            startup_inputs=startup,
        ),
        now=clock.now,
    )

    # The replica that lost the lease may still bind the sandbox while it shuts down.
    assert await executor.recover(run) is None
    terminate.assert_not_awaited()
    assert startup.launch_failures == []
    clock.advance(30)
    assert await executor.recover(run) == "failed"
    terminate.assert_awaited_once()
    assert [session for session, _ in startup.launch_failures] == [_CONV_ID]


async def test_recovery_with_a_revoked_identity_fails_without_inspecting(store) -> None:
    task = _saved_task(store)
    conversations = AsyncMock()
    executor = ClaimedRunExecutor(
        _deps(store, conversation_store=conversations),
        fire_identity=_failing_identity(FireIdentityRevoked("delegation revoked")),
    )

    assert await executor.recover(_expired_run(task)) == "failed"
    conversations.get_conversation.assert_not_called()


async def test_recovery_retries_after_a_temporary_identity_error(store) -> None:
    task = _saved_task(store)
    conversations = AsyncMock()
    executor = ClaimedRunExecutor(
        _deps(store, conversation_store=conversations),
        fire_identity=_failing_identity(ConnectionError("token service unavailable")),
    )

    # The scheduler treats an error as uncertain and checks the run on a later sweep.
    with pytest.raises(ConnectionError):
        await executor.recover(_expired_run(task))
    conversations.get_conversation.assert_not_called()


@pytest.mark.parametrize("claimed_target", ["connected_host", "managed_sandbox"])
async def test_recovery_follows_the_claimed_target_not_a_later_edit(
    store, monkeypatch, claimed_target
) -> None:
    from omnigent.server import managed_hosts

    terminate = AsyncMock()
    monkeypatch.setattr(managed_hosts, "terminate_managed_host", terminate)
    task = _saved_task(store, execution_target=claimed_target)
    run = _expired_run(task)
    if claimed_target == "connected_host":
        store.update(task.id, execution_target="managed_sandbox", host_id=None, workspace=None)
    else:
        store.update(task.id, execution_target="connected_host", host_id=_HOST_ID, workspace="/r")
    startup = FakeStartupInputs()
    # The replica died before sending the prompt, and before any sandbox was bound.
    conversations = _RecoveryConversationStore(
        _FakeConversation(
            id=_CONV_ID,
            agent_id=_AGENT_ID,
            host_id=run.host_id if claimed_target == "connected_host" else None,
            labels={} if claimed_target == "connected_host" else {STARTUP_LABEL: "pending"},
        ),
        items=[_RUNNER_STARTED],
        live_status="idle",
    )
    executor = ClaimedRunExecutor(
        _deps(
            store,
            conversation_store=conversations,
            host_store=_sandbox_hosts(task, run, "agent_sandbox"),
            managed_launches=ManagedLaunchTracker(),
            startup_inputs=startup,
        )
    )

    assert await executor.recover(run) == "failed"
    if claimed_target == "managed_sandbox":
        # Its sandbox is torn down and its saved prompt failed, as for any sandbox run.
        terminate.assert_awaited_once()
        assert [session for session, _ in startup.launch_failures] == [_CONV_ID]
    else:
        terminate.assert_not_awaited()
        assert startup.launch_failures == []


# ── Through the persistent scheduler ─────────────────────────────────────────


async def test_scheduler_records_a_session_that_finished_during_startup(store) -> None:
    """A run still starting when its session already finished is recorded as succeeded."""
    task = _saved_task(store)
    clock = FakeClock(task.next_run_at)
    conversations = _RecoveryConversationStore(
        _FakeConversation(id=_CONV_ID, agent_id=_AGENT_ID, host_id=_HOST_ID),
        live_status="idle",
        items=[_REPLY, _PROMPT, _RUNNER_STARTED],
    )
    executor = ClaimedRunExecutor(_deps(store, conversation_store=conversations))

    async def fire_then_die(snapshot: ScheduledTask, lease: StartupLease) -> None:
        # The replica sent the prompt but died before recording it.
        await lease.attach_session(_CONV_ID)

    scheduler = PersistentTaskScheduler(store, fire_then_die, executor.recover, now=clock.now)
    await scheduler.poll_once()
    await asyncio.gather(*list(scheduler._startups.values()))
    # Let the first poll's sweep finish, so the next poll starts a fresh one.
    assert scheduler._recovery is not None
    await scheduler._recovery
    clock.advance(_LEASE_SECONDS)
    await scheduler.poll_once()
    assert scheduler._recovery is not None
    await scheduler._recovery

    [run] = _runs(store, task)
    assert (run.status, run.conversation_id, run.lease_expires_at) == ("succeeded", _CONV_ID, None)
    assert run.finished_at is not None
