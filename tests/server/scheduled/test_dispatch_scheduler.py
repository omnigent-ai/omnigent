"""Database scheduler contracts against the real store, without hosts or sandboxes."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from unittest.mock import AsyncMock

import pytest

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.server.scheduled.dispatch_scheduler import (
    PersistentTaskScheduler,
    StartupLease,
    StartupLeaseLost,
)
from omnigent.server.scheduled.schedule import occurrence_host_id
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests._helpers.polling import poll_until_async
from tests.server.scheduled.test_scheduler import FakeClock

_SCHEDULER_LOGGER = "omnigent.server.scheduled.dispatch_scheduler"


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyScheduledTaskStore:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    return store


def _task(
    store: SqlAlchemyScheduledTaskStore, seed: str = "task", rrule: str = "FREQ=HOURLY", **kwargs
):
    task = store.create(
        uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex,
        seed,
        "prompt",
        rrule,
        None,
        uuid.uuid4().hex,
        "UTC",
        **kwargs,
    )
    return store.get(task.id)


def _events(caplog, event_name: str) -> list[dict]:
    return [
        {**record.attributes, "session_id": getattr(record, "session_id", None)}
        for record in caplog.records
        if getattr(record, "event_name", None) == event_name
    ]


async def _drain(scheduler: PersistentTaskScheduler) -> None:
    if scheduler._recovery is not None:
        await scheduler._recovery
    await asyncio.gather(*list(scheduler._startups.values()))


async def test_enabling_dispatch_does_not_replay_legacy_timer_occurrences(store, monkeypatch):
    from omnigent.stores.scheduled_task_store import sqlalchemy_store

    store.configure_dispatch(False)
    task = _task(store)
    assert task.next_run_at is None
    clock = FakeClock(task.created_at + 30 * 86400)
    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: int(clock.now()))
    store.update(task.id, rrule="FREQ=DAILY;BYHOUR=9")
    store.update(task.id, last_run_at=int(clock.now()))
    assert store.get(task.id).next_run_at is None
    fire = AsyncMock()
    scheduler = PersistentTaskScheduler(store, fire, AsyncMock(), now=clock.now)
    await scheduler.poll_once()
    await _drain(scheduler)
    fire.assert_not_awaited()
    assert store.get(task.id).next_run_at > clock.now()
    assert _task(store, "created_after_enabling").next_run_at is not None


async def test_bootstrap_retries_after_concurrent_metadata_edit(store, monkeypatch):
    store.configure_dispatch(False)
    task = _task(store)
    original = store.initialize_next_run_at
    attempts = 0

    def initialize(snapshot, next_run_at):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            store.update(task.id, prompt="edited during bootstrap")
        return original(snapshot, next_run_at)

    monkeypatch.setattr(store, "initialize_next_run_at", initialize)
    scheduler = PersistentTaskScheduler(store, AsyncMock(), AsyncMock())
    await scheduler.poll_once()
    await _drain(scheduler)
    assert store.get(task.id).next_run_at is None
    await scheduler.poll_once()
    await _drain(scheduler)
    assert store.get(task.id).next_run_at is not None
    assert store.get(task.id).prompt == "edited during bootstrap"
    assert attempts == 2


async def test_unfinished_fire_return_warns_and_leaves_run_for_recovery(store, caplog):
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    recover = AsyncMock(return_value="failed")
    scheduler = PersistentTaskScheduler(store, AsyncMock(), recover, now=clock.now)
    await scheduler.poll_once()
    await _drain(scheduler)
    run = store.list_runs(task.id)[0][0]
    assert run.status == "starting"
    assert "no final startup outcome was recorded" in caplog.text
    clock.advance(120)
    await scheduler.poll_once()
    await _drain(scheduler)
    recover.assert_awaited_once_with(run)
    assert store.list_runs(task.id)[0][0].status == "failed"


async def test_two_pollers_fire_once_and_do_not_catch_up_outage(store) -> None:
    with workspace_scope(42):
        task = _task(store)
    clock = FakeClock(task.next_run_at + 7200)
    fired = []

    async def fire(snapshot, lease):
        assert current_workspace_id() == 42
        assert snapshot == task
        fired.append(lease.run)
        session_id = uuid.uuid4().hex
        await lease.attach_session(session_id)
        assert store.list_runs(task.id)[0][0].conversation_id == session_id
        assert lease.run.status == "starting"
        await lease.finish("running")

    schedulers = [
        PersistentTaskScheduler(store, fire, AsyncMock(), now=clock.now) for _ in range(2)
    ]
    await asyncio.gather(*(scheduler.poll_once() for scheduler in schedulers))
    await asyncio.gather(*(_drain(scheduler) for scheduler in schedulers))
    assert len(fired) == 1
    assert fired[0].scheduled_at == task.next_run_at
    with workspace_scope(42):
        assert store.get(task.id).next_run_at > clock.now()
        assert len(store.list_runs(task.id)[0]) == 1
    await schedulers[0].poll_once()
    await _drain(schedulers[0])
    assert len(fired) == 1


async def test_scan_pages_past_unowned_tasks_and_ownership_errors(store) -> None:
    tasks = [_task(store, str(i), host_id=uuid.uuid4().hex) for i in range(3)]
    clock = FakeClock(max(task.next_run_at for task in tasks))
    ordered = store.list_due_tasks(int(clock.now()))
    own = ordered[-1]
    calls = []

    async def owns(workspace, host):
        calls.append(host)
        if host == ordered[0].host_id:
            raise RuntimeError("no assignment")
        return host == own.host_id

    async def fire(task, lease):
        assert lease.run.host_id == own.host_id
        await lease.finish("running")

    scheduler = PersistentTaskScheduler(
        store, fire, AsyncMock(), owns_host=owns, now=clock.now, batch_size=1
    )
    await scheduler.poll_once()
    await _drain(scheduler)
    assert calls == [task.host_id for task in ordered]
    assert store.list_runs(own.id)[0][0].status == "running"
    assert store.list_runs(ordered[0].id)[0] == []


async def test_startup_limit_leaves_unclaimed_work_for_next_poll(store) -> None:
    tasks = [_task(store, str(i)) for i in range(2)]
    clock = FakeClock(max(task.next_run_at for task in tasks))
    started = asyncio.Event()
    release = asyncio.Event()

    async def fire(task, lease):
        started.set()
        await release.wait()
        await lease.finish("running")

    scheduler = PersistentTaskScheduler(store, fire, AsyncMock(), now=clock.now, max_startups=1)
    await scheduler.poll_once()
    await asyncio.wait_for(started.wait(), 10)
    assert len(store.list_due_tasks(int(clock.now()))) == 1
    release.set()
    await _drain(scheduler)
    await scheduler.poll_once()
    await _drain(scheduler)
    assert store.list_due_tasks(int(clock.now())) == []


@pytest.mark.parametrize("transient_failure", [False, True])
async def test_heartbeat_renews_only_local_startup_and_stops_after_prompt(
    store, monkeypatch, transient_failure
) -> None:
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    started = asyncio.Event()
    renewed = asyncio.Event()
    release = asyncio.Event()
    leases = []
    original_renew = store.renew_starting_run
    loop = asyncio.get_running_loop()

    failed_once = False

    def renew(run_id, *, now, lease_seconds):
        nonlocal failed_once
        if transient_failure and not failed_once and now() == task.next_run_at + 30:
            failed_once = True
            raise RuntimeError("temporary database outage")
        result = original_renew(run_id, now=now, lease_seconds=lease_seconds)
        if now() == task.next_run_at + 30:
            loop.call_soon_threadsafe(renewed.set)
        return result

    monkeypatch.setattr(store, "renew_starting_run", renew)

    async def fire(task, lease):
        leases.append(lease)
        started.set()
        await release.wait()
        await lease.finish("running")

    scheduler = PersistentTaskScheduler(
        store, fire, AsyncMock(), now=clock.now, renewal_seconds=0.01
    )
    try:
        await scheduler.poll_once()
        await asyncio.wait_for(started.wait(), 10)
        clock.advance(30)
        await asyncio.wait_for(renewed.wait(), 10)
        assert store.list_runs(task.id)[0][0].lease_expires_at == task.next_run_at + 150
        release.set()
        await _drain(scheduler)
        assert leases[0].run.status == "running"
        assert failed_once is transient_failure
        assert store.list_runs(task.id)[0][0].lease_expires_at is None
        clock.advance(200)
        await scheduler.poll_once()
        assert store.list_runs(task.id)[0][0].status == "running"
    finally:
        await scheduler.stop()


async def test_cancelled_renewal_finishes_before_the_next_lease_update(store, monkeypatch):
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    clock.advance(30)
    committed = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store.renew_starting_run

    def slow_reply(*args, **kwargs):
        updated = original(*args, **kwargs)
        loop.call_soon_threadsafe(committed.set)
        assert release.wait(10)
        return updated

    monkeypatch.setattr(store, "renew_starting_run", slow_reply)
    renewal = asyncio.create_task(lease.check())
    try:
        await asyncio.wait_for(committed.wait(), 10)
        renewal.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(renewal, 10)
        # The database committed, but the cancelled caller has not received its result.
        assert store.list_runs(task.id)[0][0].lease_expires_at > lease.run.lease_expires_at
        finishing = asyncio.create_task(lease.finish("running"))
        release.set()
        await asyncio.wait_for(finishing, 10)
        assert lease.run == store.list_runs(task.id)[0][0]
        assert lease.run.status == "running"
    finally:
        release.set()
        await asyncio.gather(renewal, return_exceptions=True)


@pytest.mark.parametrize("cancel_when", ["never", "while_writing", "after_write"])
@pytest.mark.parametrize("failure", [RuntimeError, StartupLeaseLost])
async def test_lease_write_failure_after_cancellation_is_logged_once(
    store, monkeypatch, caplog, cancel_when, failure
):
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    error = failure("lease write failed")

    def failing_write(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        raise error

    monkeypatch.setattr(store, "renew_starting_run", failing_write)
    caller = asyncio.create_task(lease.check())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        writes = list(lease._writes)
        if cancel_when == "while_writing":
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        elif cancel_when == "after_write":
            # Done callbacks run before the shielded caller resumes.
            assert len(writes) == 1
            writes[0].add_done_callback(lambda _: caller.cancel())
        release.set()
        if cancel_when == "never":
            with pytest.raises(failure):
                await asyncio.wait_for(caller, 10)
        elif cancel_when == "after_write":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 10)
            assert writes[0].done()
        await asyncio.wait_for(asyncio.gather(*writes, return_exceptions=True), 10)
        records = [r for r in caplog.records if r.name.endswith("dispatch_scheduler")]
        if cancel_when != "never" and failure is RuntimeError:
            assert len(records) == 1
            assert records[0].levelno == logging.ERROR
            assert run.id in records[0].message
            assert records[0].exc_info[1] is error
        else:
            assert records == []
        assert store.list_runs(task.id)[0] == [run]
    finally:
        release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(*list(lease._writes), return_exceptions=True), 10)


@pytest.mark.parametrize("operation", ["renew", "attach", "finish"])
async def test_cancelled_lease_write_waiting_for_lock_never_reaches_store(
    store, monkeypatch, operation
):
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    committed = asyncio.Event()
    queued = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store.renew_starting_run
    calls = []

    def slow_reply(*args, **kwargs):
        calls.append("renew")
        updated = original(*args, **kwargs)
        loop.call_soon_threadsafe(committed.set)
        assert release.wait(10)
        return updated

    async def waiting_write():
        queued.set()
        if operation == "renew":
            await lease.check()
        elif operation == "attach":
            await lease.attach_session("cancelled-session")
        else:
            await lease.finish("running")

    monkeypatch.setattr(store, "renew_starting_run", slow_reply)
    renewal = asyncio.create_task(lease.check())
    waiter = None
    try:
        await asyncio.wait_for(committed.wait(), 10)
        expected = store.list_runs(task.id)[0][0]
        waiter = asyncio.create_task(waiting_write())
        await asyncio.wait_for(queued.wait(), 10)
        waiter.cancel()
        renewal.cancel()
        for caller in (waiter, renewal):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 10)
        clock.advance(100)
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(lease._writes)), 10)
        assert calls == ["renew"]
        assert lease.run == store.list_runs(task.id)[0][0] == expected
        # Completion of the cancelled in-flight write must also release the lock.
        await asyncio.wait_for(lease.finish("running"), 10)
        assert lease.run.status == "running"
    finally:
        release.set()
        for caller in (renewal, waiter):
            if caller is not None:
                caller.cancel()
                await asyncio.gather(caller, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(*list(lease._writes), return_exceptions=True), 10)


@pytest.mark.parametrize(
    "interruption",
    [
        "lease_loss",
        "renewal_timeout",
        "stop",
        "stop_during_finish",
        "executor_error",
        "running_executor_error",
    ],
)
async def test_interrupted_executor_stops_and_preserves_run_state(
    store, monkeypatch, caplog, interruption
):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    entered = asyncio.Event()
    exited = asyncio.Event()
    renewal_blocked = asyncio.Event()
    finish_blocked = asyncio.Event()
    release = threading.Event()
    leases = []
    loop = asyncio.get_running_loop()
    original_renew = store.renew_starting_run
    original_transition = store.transition_starting_run

    def hanging_renewal(*args, **kwargs):
        loop.call_soon_threadsafe(renewal_blocked.set)
        assert release.wait(10)
        return original_renew(*args, **kwargs)

    def blocked_transition(*args, **kwargs):
        loop.call_soon_threadsafe(finish_blocked.set)
        assert release.wait(10)
        return original_transition(*args, **kwargs)

    if interruption == "stop_during_finish":
        monkeypatch.setattr(store, "transition_starting_run", blocked_transition)

    async def fire(task, lease):
        leases.append(lease)
        entered.set()
        try:
            if interruption in {"running_executor_error", "stop_during_finish"}:
                await lease.finish("running")
            if interruption in {"executor_error", "running_executor_error"}:
                raise RuntimeError("executor could not complete startup")
            await asyncio.Event().wait()
        finally:
            exited.set()

    scheduler = PersistentTaskScheduler(
        store, fire, AsyncMock(return_value=None), now=clock.now, renewal_seconds=0.01
    )
    if interruption == "renewal_timeout":
        heartbeat_ready = asyncio.Event()
        original_heartbeat = scheduler._heartbeat

        async def heartbeat(lease):
            await heartbeat_ready.wait()
            await original_heartbeat(lease)

        monkeypatch.setattr(scheduler, "_heartbeat", heartbeat)
    try:
        await scheduler.poll_once()
        startups = list(scheduler._startups.values())
        await asyncio.wait_for(entered.wait(), 10)
        if interruption == "lease_loss":
            monkeypatch.setattr(store, "renew_starting_run", lambda *args, **kwargs: None)
        elif interruption == "renewal_timeout":
            monkeypatch.setattr(store, "renew_starting_run", hanging_renewal)
            clock.advance(119)
            # Capture the heartbeat's real timeout after advancing the fake clock.
            heartbeat_ready.set()
            await asyncio.wait_for(renewal_blocked.wait(), 10)
            clock.advance(1)
        elif interruption == "stop":
            await asyncio.wait_for(scheduler.stop(), 10)
        elif interruption == "stop_during_finish":
            await asyncio.wait_for(finish_blocked.wait(), 10)
            await asyncio.wait_for(scheduler.stop(), 10)
            # The shielded write commits after its caller was cancelled.
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*list(leases[0]._writes), return_exceptions=True), 10
            )
        await asyncio.wait_for(asyncio.gather(*startups, return_exceptions=True), 10)
        assert exited.is_set()
        assert all(startup.cancelled() or startup.exception() is None for startup in startups)
        assert scheduler._startups == {}
        run = store.list_runs(task.id)[0][0]
        expected_status = (
            "running"
            if interruption in {"running_executor_error", "stop_during_finish"}
            else "starting"
        )
        assert run.status == expected_status
        assert (run.lease_expires_at is None) == (expected_status == "running")
        records = [
            record
            for record in caplog.records
            if record.name.endswith("dispatch_scheduler") and record.levelno >= logging.WARNING
        ]
        if interruption in {"lease_loss", "renewal_timeout"}:
            assert len(records) == 1
            assert records[0].levelno == logging.WARNING
            assert records[0].exc_info is None
            assert "Startup lease lost" in records[0].message
        if interruption == "renewal_timeout":
            # The executor stops while the database thread is still blocked.
            assert not release.is_set()
            assert leases[0]._writes
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*list(leases[0]._writes), return_exceptions=True), 10
            )
            assert store.list_runs(task.id)[0][0] == run
        if interruption in {"executor_error", "running_executor_error"}:
            assert f"status={expected_status}" in records[0].message
            assert "recovery" not in records[0].message
            assert "executor could not complete startup" in caplog.text
            assert records[0].levelno == logging.ERROR
            assert records[0].exc_info is not None
        expected_outcome = {
            "lease_loss": "lease_lost",
            "renewal_timeout": "lease_lost",
            "stop": "cancelled",
            "stop_during_finish": "running",
            "executor_error": "interrupted",
            "running_executor_error": "running",
        }[interruption]
        # A startup that ends with a write in flight is logged once that write settles.
        await poll_until_async(lambda: _events(caplog, "scheduled_run_startup_ended"))
        assert [event["outcome"] for event in _events(caplog, "scheduled_run_startup_ended")] == [
            expected_outcome
        ]
    finally:
        release.set()
        await scheduler.stop()
        for lease in leases:
            await asyncio.wait_for(
                asyncio.gather(*list(lease._writes), return_exceptions=True), 10
            )


@pytest.mark.parametrize("blocked_at", ["ownership", "claim"])
async def test_stop_prevents_a_poll_from_starting_more_work(store, monkeypatch, blocked_at):
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store.claim_scheduled_run

    def block():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    def claim(*args, **kwargs):
        run = original(*args, **kwargs)
        block()
        return run

    async def owns(workspace, host):
        if blocked_at == "ownership":
            await asyncio.to_thread(block)
        return True

    if blocked_at == "claim":
        monkeypatch.setattr(store, "claim_scheduled_run", claim)
    fire = AsyncMock()
    scheduler = PersistentTaskScheduler(
        store, fire, AsyncMock(return_value=None), now=clock.now, owns_host=owns
    )
    poll = asyncio.create_task(scheduler.poll_once())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await asyncio.wait_for(scheduler.stop(), 10)
        release.set()
        await asyncio.wait_for(poll, 10)
        await _drain(scheduler)
        fire.assert_not_awaited()
        runs = store.list_runs(task.id)[0]
        assert len(runs) == (1 if blocked_at == "claim" else 0)
        assert all(run.status == "starting" for run in runs)
        clock.advance(3600)
        await scheduler.poll_once()
        assert store.list_runs(task.id)[0] == runs
    finally:
        release.set()
        await asyncio.gather(poll, return_exceptions=True)
        await scheduler.stop()


async def test_expiry_cancels_old_executor_and_reconciles_without_refiring(store, caplog) -> None:
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    fire_calls = 0

    async def fire(task, lease):
        nonlocal fire_calls
        fire_calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    reconciled = []

    async def recover(run):
        # This fake executor has not sent a prompt or created a sandbox.
        assert cancelled.is_set()
        reconciled.append(run)
        return "failed"

    scheduler = PersistentTaskScheduler(store, fire, recover, now=clock.now)
    try:
        await scheduler.poll_once()
        await asyncio.wait_for(started.wait(), 10)
        clock.advance(120)
        await scheduler.poll_once()
        await _drain(scheduler)
        assert cancelled.is_set()
        assert reconciled[0].status == "starting"
        assert store.list_runs(task.id)[0][0].status == "failed"
        assert fire_calls == 1
        assert [e["outcome"] for e in _events(caplog, "scheduled_run_startup_ended")] == [
            "cancelled"
        ]
        assert [e["outcome"] for e in _events(caplog, "scheduled_run_recovered")] == ["failed"]
    finally:
        await scheduler.stop()


@pytest.mark.parametrize("outcome", ["running", "failed"])
async def test_new_owner_does_not_renew_other_process_claim(store, outcome) -> None:
    task = _task(store, execution_target="managed_sandbox")
    clock = FakeClock(task.next_run_at)
    host = occurrence_host_id(task, f"schedule:{task.next_run_at}")
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=host,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    session_id = uuid.uuid4().hex
    await lease.attach_session(session_id)
    run = lease.run
    recovered = AsyncMock(return_value=outcome)
    fire = AsyncMock()
    scheduler = PersistentTaskScheduler(store, fire, recovered, now=clock.now)
    clock.advance(30)
    await scheduler.poll_once()
    await _drain(scheduler)
    assert store.list_runs(task.id)[0][0].lease_expires_at == run.lease_expires_at
    recovered.assert_not_awaited()
    clock.advance(90)
    await scheduler.poll_once()
    await _drain(scheduler)
    recovered.assert_awaited_once_with(run)
    fire.assert_not_awaited()
    recovered_run = store.list_runs(task.id)[0][0]
    assert recovered_run.status == outcome
    assert recovered_run.conversation_id == session_id
    assert recovered_run.lease_expires_at is None
    assert (recovered_run.finished_at is None) == (outcome == "running")


async def test_recovery_skips_unowned_hosts_and_ownership_errors(store) -> None:
    with workspace_scope(42):
        tasks = [_task(store, str(i), host_id=uuid.uuid4().hex) for i in range(3)]
        clock = FakeClock(max(task.next_run_at for task in tasks))
        for task in tasks:
            store.claim_scheduled_run(
                task,
                now=int(clock.now()),
                next_run_at=int(clock.now()) + 3600,
                host_id=task.host_id,
                lease_seconds=120,
            )
    clock.advance(120)
    runs = store.list_expired_starting_runs(int(clock.now()))
    checked = []

    async def owns(workspace, host):
        assert workspace == 42
        checked.append(host)
        if host == runs[0].host_id:
            raise RuntimeError("ownership lookup unavailable")
        return host == runs[-1].host_id

    async def recover(run):
        assert current_workspace_id() == 42
        assert run == runs[-1]
        return "running"

    recovery = AsyncMock(side_effect=recover)
    fire = AsyncMock()
    scheduler = PersistentTaskScheduler(
        store, fire, recovery, owns_host=owns, now=clock.now, batch_size=1
    )
    try:
        for index in range(len(runs)):
            await scheduler.poll_once()
            await _drain(scheduler)
            assert checked == [run.host_id for run in runs[: index + 1]]
        recovery.assert_awaited_once_with(runs[-1])
        fire.assert_not_awaited()
        assert store.list_expired_starting_runs(int(clock.now())) == runs[:2]
        with workspace_scope(42):
            assert store.list_runs(runs[-1].scheduled_task_id)[0][0].status == "running"
    finally:
        await scheduler.stop()


async def test_late_recovery_result_cannot_overwrite_another_replica(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    clock.advance(120)
    second_entered = asyncio.Event()
    first_committed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_transition = store.transition_starting_run
    results = []

    def transition(snapshot, **kwargs):
        result = original_transition(snapshot, **kwargs)
        results.append(result)
        if result is not None:
            loop.call_soon_threadsafe(first_committed.set)
        return result

    monkeypatch.setattr(store, "transition_starting_run", transition)

    async def preserve(snapshot):
        assert snapshot == run
        await asyncio.wait_for(second_entered.wait(), 10)
        return "running"

    async def stale_failure(snapshot):
        assert snapshot == run
        second_entered.set()
        await asyncio.wait_for(first_committed.wait(), 10)
        return "failed"

    fire = AsyncMock()
    schedulers = [
        PersistentTaskScheduler(store, fire, recovery, now=clock.now)
        for recovery in (preserve, stale_failure)
    ]
    try:
        await asyncio.gather(*(scheduler.poll_once() for scheduler in schedulers))
        await asyncio.wait_for(
            asyncio.gather(*(_drain(scheduler) for scheduler in schedulers)), 10
        )
        assert len(results) == 2
        assert results[0].status == "running"
        assert results[1] is None
        recorded = store.list_runs(task.id)[0][0]
        assert recorded == results[0]
        assert recorded.error is None
        fire.assert_not_awaited()
        # Each replica logs when its own write finishes, in either order.
        outcomes = [event["outcome"] for event in _events(caplog, "scheduled_run_recovered")]
        assert sorted(outcomes) == ["running", "superseded"]
    finally:
        await asyncio.gather(*(scheduler.stop() for scheduler in schedulers))


async def test_lease_checkpoint_cannot_revive_expired_claim(store) -> None:
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    clock.advance(120)
    with pytest.raises(StartupLeaseLost):
        await lease.check()
    with pytest.raises(StartupLeaseLost):
        await lease.finish("running")
    assert store.list_runs(task.id)[0][0] == run


async def test_loop_retries_after_boot_database_outage_and_stops(store, monkeypatch) -> None:
    original = store.list_uninitialized_tasks
    attempts = 0
    recovered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def read(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary outage")
        result = original(**kwargs)
        loop.call_soon_threadsafe(recovered.set)
        return result

    monkeypatch.setattr(store, "list_uninitialized_tasks", read)
    scheduler = PersistentTaskScheduler(store, AsyncMock(), AsyncMock(), poll_seconds=0.01)
    await scheduler.start()
    await scheduler.start()
    try:
        await asyncio.wait_for(recovered.wait(), 10)
        assert attempts == 2
        assert scheduler.is_started
    finally:
        await scheduler.stop()
    assert not scheduler.is_started
    recovered.clear()
    await scheduler.start()
    try:
        await asyncio.wait_for(recovered.wait(), 10)
        assert scheduler.is_started
    finally:
        await scheduler.stop()


@pytest.mark.parametrize("failure", ["exception", "timeout", "unknown", "skipped", "invalid"])
@pytest.mark.parametrize("deleted", [False, True])
async def test_expired_cleanup_failure_is_retried_without_another_fire(
    store, failure, deleted, caplog
) -> None:
    task = _task(store, execution_target="managed_sandbox")
    healthy_task = _task(store, "healthy-expired", execution_target="managed_sandbox")
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=occurrence_host_id(task, f"schedule:{task.next_run_at}"),
        lease_seconds=120,
    )
    # One second later, so the failing run is first in the scan order.
    healthy_run = store.claim_scheduled_run(
        healthy_task,
        now=int(clock.now()) + 1,
        next_run_at=healthy_task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    if deleted:
        assert store.delete(task.id)
    clock.advance(121)

    async def recovery(recovered):
        if recovered.id == healthy_run.id:
            return "failed"
        assert store.get(task.id) is not None
        assert recovered.host_id == occurrence_host_id(task, f"schedule:{task.next_run_at}")
        if recover.await_count == 1:
            if failure == "unknown":
                return None
            if failure in {"skipped", "invalid"}:
                return failure
            if failure == "timeout":
                await asyncio.Event().wait()
            raise RuntimeError("cleanup unavailable")
        return "failed"

    recover = AsyncMock(side_effect=recovery)
    fire = AsyncMock()
    scheduler = PersistentTaskScheduler(
        store, fire, recover, now=clock.now, recovery_seconds=0.05, batch_size=1
    )
    await scheduler.poll_once()
    await _drain(scheduler)
    assert store.list_runs(task.id)[0] == [run]
    if failure in {"skipped", "invalid"}:
        assert "Invalid recovery result" in caplog.text
    elif failure == "timeout":
        assert "Recovery timed out" in caplog.text
    # The next page is the healthy run; the failed run waits for the scan to wrap.
    await scheduler.poll_once()
    await _drain(scheduler)
    assert recover.await_args.args == (healthy_run,)
    await scheduler.poll_once()
    await _drain(scheduler)
    assert recover.await_count == 2
    await scheduler.poll_once()
    await _drain(scheduler)
    if deleted:
        assert store.get(task.id) is None
        assert store.list_runs(task.id)[0] == []
    else:
        assert store.list_runs(task.id)[0][0].status == "failed"
    assert recover.await_count == 3
    assert recover.await_args.args == (run,)
    fire.assert_not_awaited()


async def test_recovery_retries_discovery_and_resumes_deleted_task_purge(
    store, monkeypatch, caplog
):
    monkeypatch.setattr(store, "_cleanup_batch_size", 1)
    deleted = [_task(store, f"deleted-{index}") for index in range(2)]
    for task in deleted:
        for index in range(2):
            store.create_run(uuid.uuid4().hex, task.id, "succeeded", index)
        # Simulate interruption after the deletion marker commits.
        store.update(task.id, state="deleted")
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    clock.advance(120)
    # Expires after the first scan's cutoff, so it must wait for the scan to wrap.
    late_task = _task(store, "expires-after-cutoff")
    late_run = store.claim_scheduled_run(
        late_task,
        now=int(clock.now()),
        next_run_at=late_task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    original_list = store.list_expired_starting_runs
    original_purge = store.purge_deleted_tasks
    discovery_calls = 0
    purge_cursors = []

    def discover(*args, **kwargs):
        nonlocal discovery_calls
        discovery_calls += 1
        if discovery_calls == 1:
            raise RuntimeError("recovery discovery unavailable")
        return original_list(*args, **kwargs)

    def purge(*, after=None, limit=100):
        purge_cursors.append(after)
        if len(purge_cursors) == 2:
            raise RuntimeError("purge temporarily unavailable")
        return original_purge(after=after, limit=limit)

    monkeypatch.setattr(store, "list_expired_starting_runs", discover)
    monkeypatch.setattr(store, "purge_deleted_tasks", purge)
    fire = AsyncMock()
    recover = AsyncMock(return_value="failed")
    scheduler = PersistentTaskScheduler(store, fire, recover, now=clock.now, batch_size=1)
    try:
        await scheduler.poll_once()
        await _drain(scheduler)
        assert sum(len(store.list_runs(item.id)[0]) for item in deleted) == 3
        recover.assert_not_awaited()
        cursor = scheduler._cleanup_cursor
        assert cursor is not None and cursor[2] is not None
        scan_cutoff = scheduler._recovery_cutoff
        assert scan_cutoff == int(clock.now())
        # The discovery failure leaves the cutoff in place while time passes.
        clock.advance(120)
        await scheduler.poll_once()
        await _drain(scheduler)
        assert scheduler._cleanup_cursor == cursor
        recover.assert_awaited_once_with(run)
        assert scheduler._recovery_cutoff == scan_cutoff
        # The empty page after the first run wraps the scan, so late_run waits.
        await scheduler.poll_once()
        await _drain(scheduler)
        recover.assert_awaited_once_with(run)
        assert scheduler._recovery_cutoff is None
        await scheduler.poll_once()
        await _drain(scheduler)
        assert recover.await_count == 2
        assert recover.await_args.args == (late_run,)
        for _ in range(10):
            await scheduler.poll_once()
            await _drain(scheduler)
            if scheduler._cleanup_cursor is None:
                break
        else:
            pytest.fail("Deleted-task cleanup did not finish")
        assert purge_cursors[:3] == [None, cursor, cursor]
        assert all(store.get(item.id) is None for item in deleted)
        assert store.list_runs(task.id)[0][0].status == "failed"
        fire.assert_not_awaited()
        failed = _events(caplog, "scheduled_task_operation_failed")
        assert [event["operation"] for event in failed] == ["recovery", "cleanup"]
    finally:
        await scheduler.stop()


async def test_recovery_timeout_allows_inflight_transition_to_finish(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    clock.advance(120)
    entered = asyncio.Event()
    release = threading.Event()
    committed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = store.transition_starting_run

    def delayed_transition(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        result = original(*args, **kwargs)
        loop.call_soon_threadsafe(committed.set)
        return result

    monkeypatch.setattr(store, "transition_starting_run", delayed_transition)
    fire = AsyncMock()
    recover = AsyncMock(return_value="running")
    scheduler = PersistentTaskScheduler(
        store,
        fire,
        recover,
        now=clock.now,
        recovery_seconds=0.1,
    )
    try:
        await scheduler.poll_once()
        await asyncio.wait_for(entered.wait(), 10)
        await asyncio.wait_for(_drain(scheduler), 10)
        assert "Recovery timed out" in caplog.text
        assert "an in-flight database update may still complete" in caplog.text
        assert store.list_runs(task.id)[0][0].status == "starting"
        release.set()
        await asyncio.wait_for(committed.wait(), 10)
        assert store.list_runs(task.id)[0][0].status == "running"
        # The late commit is still logged, after the timeout.
        await poll_until_async(lambda: len(_events(caplog, "scheduled_run_recovered")) == 2)
        assert [event["outcome"] for event in _events(caplog, "scheduled_run_recovered")] == [
            "timeout",
            "running",
        ]
        await scheduler.poll_once()
        await _drain(scheduler)
        recover.assert_awaited_once()
        fire.assert_not_awaited()
    finally:
        release.set()
        await scheduler.stop()


def test_d1_dispatch_is_rejected_before_polling(store, monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        store, "_engine", SimpleNamespace(dialect=SimpleNamespace(name="cloudflare_d1"))
    )
    with pytest.raises(NotImplementedError, match="multi-statement transactions"):
        PersistentTaskScheduler(store, AsyncMock(), AsyncMock())


@pytest.mark.parametrize("bootstrap", [False, True])
async def test_subminimum_legacy_schedule_is_paused(store, bootstrap, caplog) -> None:
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask

    task = _task(store)
    invalid = store.update(task.id, rrule="FREQ=MINUTELY")
    clock = FakeClock(invalid.next_run_at)
    if bootstrap:
        with store._session_immediate("test_legacy_schedule") as session:
            session.execute(
                update(SqlScheduledTask)
                .where(SqlScheduledTask.id == task.id)
                .values(next_run_at=None)
            )
    fire = AsyncMock()
    scheduler = PersistentTaskScheduler(store, fire, AsyncMock(return_value=None), now=clock.now)
    await scheduler.poll_once()
    await _drain(scheduler)
    await scheduler.poll_once()
    await _drain(scheduler)
    fire.assert_not_awaited()
    assert store.list_runs(task.id)[0] == []
    assert store.get(task.id).state == "paused"
    assert store.get(task.id).next_run_at is None
    assert caplog.text.count("invalid schedule") == 1


@pytest.mark.parametrize("action", ["check", "attach_session", "finish"])
async def test_lease_expiring_while_waiting_for_row_lock_is_not_revived(
    store, monkeypatch, action
) -> None:
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlScheduledTaskRun

    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=int(clock.now()),
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    clock.advance(119)
    original_get = Session.get

    def delayed_get(session, entity, *args, **kwargs):
        row = original_get(session, entity, *args, **kwargs)
        if entity is SqlScheduledTaskRun and kwargs.get("with_for_update"):
            clock.advance(2)
        return row

    monkeypatch.setattr(Session, "get", delayed_get)
    with pytest.raises(StartupLeaseLost):
        if action == "check":
            await lease.check()
        elif action == "attach_session":
            await lease.attach_session(uuid.uuid4().hex)
        else:
            await lease.finish("running")
    assert store.list_runs(task.id)[0] == [run]


async def test_slow_recovery_does_not_block_due_tasks_and_is_drained_on_stop(store) -> None:
    expired_task = _task(store, "expired")
    due_task = _task(store, "due")
    clock = FakeClock(max(expired_task.next_run_at, due_task.next_run_at))
    expired_run = store.claim_scheduled_run(
        expired_task,
        now=int(clock.now()),
        next_run_at=int(clock.now()) + 3600,
        host_id=None,
        lease_seconds=120,
    )
    clock.advance(120)
    recovering = asyncio.Event()
    cancelled = asyncio.Event()

    async def recover(run):
        recovering.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def fire(task, lease):
        assert task.id == due_task.id
        await lease.finish("running")

    recovery = AsyncMock(side_effect=recover)
    scheduler = PersistentTaskScheduler(
        store,
        fire,
        recovery,
        now=clock.now,
        recovery_seconds=60,
    )
    try:
        await scheduler.poll_once()
        await asyncio.wait_for(recovering.wait(), 10)
        await asyncio.gather(*list(scheduler._startups.values()))
        assert store.list_runs(due_task.id)[0][0].status == "running"
        await scheduler.poll_once()
        recovery.assert_awaited_once_with(expired_run)
        assert not cancelled.is_set()
    finally:
        await scheduler.stop()
    assert cancelled.is_set()
    assert store.list_runs(expired_task.id)[0] == [expired_run]


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("failure", ["validation", "calculation", "unexpected"])
async def test_schedule_search_failure_preserves_next_run_at_and_retries(
    store, monkeypatch, bootstrap, failure
) -> None:
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask
    from omnigent.server.scheduled import dispatch_scheduler, schedule
    from omnigent.server.scheduled.rrule import RRuleSearchError

    task = _task(store)
    healthy = _task(store, "healthy")
    clock = FakeClock(task.next_run_at)
    if bootstrap:
        with store._session_immediate("test_legacy_schedule") as session:
            session.execute(
                update(SqlScheduledTask)
                .where(SqlScheduledTask.id == task.id)
                .values(next_run_at=None)
            )
    snapshot = store.get(task.id)
    original = schedule.get_next_fire_time
    original_validate = dispatch_scheduler.validate_rrule
    failing = True

    def validate(rule):
        if failing and failure == "validation" and rule == "FREQ=HOURLY":
            raise RRuleSearchError("RRULE validation exceeds the supported time limit")
        return original_validate(rule)

    def calculate(rule, after, tz, *, anchor=None):
        # Distinguish the task without replacing schedule error handling.
        if failing and failure != "validation" and rule == "FREQ=HOURLY":
            if failure == "unexpected":
                raise OverflowError("invalid persisted timestamp")
            raise RRuleSearchError("RRULE search exceeds the supported time limit")
        return original(rule, after, tz, anchor=anchor)

    store.update(healthy.id, rrule="FREQ=HOURLY;INTERVAL=1")
    monkeypatch.setattr(schedule, "get_next_fire_time", calculate)
    monkeypatch.setattr(dispatch_scheduler, "validate_rrule", validate)

    async def fire(task, lease):
        await lease.finish("running")

    scheduler = PersistentTaskScheduler(store, fire, AsyncMock(return_value=None), now=clock.now)
    try:
        await scheduler.poll_once()
        await _drain(scheduler)
        assert store.get(task.id) == snapshot
        assert store.list_runs(task.id)[0] == []
        assert store.list_runs(healthy.id)[0][0].status == "running"
        failing = False
        await scheduler.poll_once()
        await _drain(scheduler)
        assert store.get(task.id).next_run_at > clock.now()
        if bootstrap:
            clock.advance(3600)
            await scheduler.poll_once()
            await _drain(scheduler)
        assert store.list_runs(task.id)[0][0].status == "running"
    finally:
        await scheduler.stop()


async def test_initialization_advances_pages_past_bad_rows_while_work_continues(
    store, monkeypatch
):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask

    legacy = sorted([_task(store, f"legacy-{i}") for i in range(5)], key=lambda task: task.id)
    due = _task(store, "due")
    expired = _task(store, "expired")
    clock = FakeClock(due.next_run_at + 120)
    store.claim_scheduled_run(
        expired,
        now=due.next_run_at,
        next_run_at=due.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    with store._session_immediate("test_uninitialized_tasks") as session:
        session.execute(
            update(SqlScheduledTask)
            .where(SqlScheduledTask.id.in_([t.id for t in legacy]))
            .values(next_run_at=None)
        )
    original = store.initialize_next_run_at
    attempted = []

    def initialize(task, next_run_at):
        attempted.append(task.id)
        if task.id == legacy[0].id:
            raise RuntimeError("bad persisted row")
        return original(task, next_run_at)

    monkeypatch.setattr(store, "initialize_next_run_at", initialize)

    async def fire(task, lease):
        await lease.finish("running")

    recovery = AsyncMock(return_value="failed")
    scheduler = PersistentTaskScheduler(store, fire, recovery, now=clock.now, batch_size=2)
    try:
        for expected in (2, 4, 5):
            await scheduler.poll_once()
            await _drain(scheduler)
            assert len(attempted) == expected
        assert attempted == [task.id for task in legacy]
        assert store.get(legacy[0].id).next_run_at is None
        assert all(store.get(task.id).next_run_at is not None for task in legacy[1:])
        assert store.list_runs(due.id)[0][0].status == "running"
        assert store.list_runs(expired.id)[0][0].status == "failed"
        recovery.assert_awaited_once()
    finally:
        await scheduler.stop()


async def test_heartbeat_stops_retrying_at_last_confirmed_expiry(store, monkeypatch):
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    attempts = 0

    def unavailable(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        clock.advance(60)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "renew_starting_run", unavailable)
    scheduler = PersistentTaskScheduler(
        store, AsyncMock(), AsyncMock(), now=clock.now, renewal_seconds=0.01
    )
    with pytest.raises(StartupLeaseLost, match="expired"):
        await scheduler._heartbeat(lease)
    assert attempts == 2
    assert store.list_runs(task.id)[0] == [run]


async def test_recovery_pages_leave_time_for_cleanup_and_revisit_uncertain_runs(store, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    tasks = [_task(store, f"expired-{index}") for index in range(3)]
    clock = FakeClock(max(task.next_run_at for task in tasks))
    for task in tasks:
        store.claim_scheduled_run(
            task,
            now=int(clock.now()),
            next_run_at=int(clock.now()) + 3600,
            host_id=None,
            lease_seconds=120,
        )
    clock.advance(120)
    runs = store.list_expired_starting_runs(int(clock.now()))
    deleted = _task(store, "deleted")
    store.update(deleted.id, state="deleted")
    recover = AsyncMock(return_value=None)
    scheduler = PersistentTaskScheduler(store, AsyncMock(), recover, now=clock.now, batch_size=1)
    try:
        await scheduler.poll_once()
        await _drain(scheduler)
        assert [call.args[0] for call in recover.await_args_list] == runs[:1]
        assert store.get(deleted.id) is None
        # Expires after the scan's cutoff, so it must wait for the next scan.
        later = _task(store, "expires-during-scan")
        later_run = store.claim_scheduled_run(
            later,
            now=int(clock.now()),
            next_run_at=int(clock.now()) + 3600,
            host_id=None,
            lease_seconds=120,
        )
        clock.advance(120)
        for index in range(1, len(runs)):
            await scheduler.poll_once()
            await _drain(scheduler)
            assert [call.args[0] for call in recover.await_args_list] == runs[: index + 1]
            assert store.get(deleted.id) is None
        # New expirations wait for the next scan, so older uncertain work gets a turn.
        # The empty page ends this scan; the following poll revisits uncertain work.
        await scheduler.poll_once()
        await _drain(scheduler)
        assert recover.await_count == len(runs)
        for run in [*runs, later_run]:
            await scheduler.poll_once()
            await _drain(scheduler)
            assert recover.await_args.args == (run,)
        assert recover.await_count == 2 * len(runs) + 1
        # Each inspection that cannot decide logs another uncertain event.
        uncertain = _events(caplog, "scheduled_run_recovered")
        assert len(uncertain) == recover.await_count
        assert {event["outcome"] for event in uncertain} == {"uncertain"}
    finally:
        await scheduler.stop()


async def test_restart_after_stop_starts_recovery_scan_from_the_beginning(store):
    tasks = [_task(store, f"restart-{index}") for index in range(2)]
    clock = FakeClock(max(task.next_run_at for task in tasks))
    for task in tasks:
        store.claim_scheduled_run(
            task,
            now=int(clock.now()),
            next_run_at=int(clock.now()) + 3600,
            host_id=None,
            lease_seconds=120,
        )
    clock.advance(120)
    runs = store.list_expired_starting_runs(int(clock.now()))
    recovered = []
    recovery_started = asyncio.Event()

    async def recover(run):
        recovered.append(run)
        recovery_started.set()
        return

    # The loop polls once on start; the long interval keeps the test to that poll.
    scheduler = PersistentTaskScheduler(
        store,
        AsyncMock(),
        AsyncMock(side_effect=recover),
        now=clock.now,
        batch_size=1,
        poll_seconds=3600,
    )
    try:
        await scheduler.start()
        await asyncio.wait_for(recovery_started.wait(), 10)
        await _drain(scheduler)
        assert recovered == runs[:1]
        await scheduler.stop()
        assert scheduler._recovery_cursor is None
        assert scheduler._recovery_cutoff is None
        clock.advance(10)
        recovery_started.clear()
        await scheduler.start()
        await asyncio.wait_for(recovery_started.wait(), 10)
        await _drain(scheduler)
        # The restarted scan begins at the first run, with a cutoff taken after the restart.
        assert recovered == [runs[0], runs[0]]
        assert scheduler._recovery_cutoff == int(clock.now())
    finally:
        await scheduler.stop()


async def test_debug_events_cover_fire_success_delays_and_recovery(store, caplog):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask

    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    pinned = {"execution_target": "connected_host", "host_id": uuid.uuid4().hex}
    tasks = {
        seed: _task(store, seed, **pinned)
        for seed in ("delivered", "failed", "skipped", "unfinished", "interrupted", "duplicate")
    }
    clock = FakeClock(max(task.next_run_at for task in tasks.values()) + 7)
    # Another replica already started this occurrence; a stale cursor revisits it.
    duplicate = tasks["duplicate"]
    started = store.claim_scheduled_run(
        duplicate,
        now=duplicate.next_run_at,
        next_run_at=duplicate.next_run_at + 3600,
        host_id=duplicate.host_id,
        lease_seconds=120,
    )
    store.transition_starting_run(started, now=clock.now, status="running")
    with store._session_immediate("test_revisited_occurrence") as session:
        session.execute(
            update(SqlScheduledTask)
            .where(SqlScheduledTask.id == duplicate.id)
            .values(next_run_at=duplicate.next_run_at)
        )
    session_id = uuid.uuid4().hex
    claims_done = asyncio.Event()

    async def fire(task, lease):
        if task.name == "delivered":
            await lease.attach_session(session_id)
            # Re-recording the same session is not a second session event.
            await lease.attach_session(session_id)
            # Advance only after every claim has read the clock.
            await claims_done.wait()
            clock.advance(3)
            await lease.finish("running")
        elif task.name == "failed":
            await lease.finish("failed", error_code="host_offline")
        elif task.name == "skipped":
            await lease.finish("skipped")
        elif task.name == "interrupted":
            raise RuntimeError("runner start failed")

    recover = AsyncMock(return_value="running")
    scheduler = PersistentTaskScheduler(store, fire, recover, now=clock.now)
    try:
        await scheduler.poll_once()
        claims_done.set()
        await _drain(scheduler)
        clock.advance(120)
        await scheduler.poll_once()
        await _drain(scheduler)
    finally:
        await scheduler.stop()

    names = {task.id: name for name, task in tasks.items()}
    claimed = _events(caplog, "scheduled_run_claimed")
    assert sorted(names[event["task_id"]] for event in claimed) == [
        "delivered",
        "failed",
        "interrupted",
        "skipped",
        "unfinished",
    ]
    assert {(event["target"], event["claim_delay_seconds"]) for event in claimed} == {
        ("connected_host", 7)
    }
    assert [event["task_id"] for event in _events(caplog, "scheduled_run_claim_skipped")] == [
        duplicate.id
    ]
    [attached] = _events(caplog, "scheduled_run_session_attached")
    assert (attached["session_id"], attached["session_delay_seconds"]) == (session_id, 7)
    ended = {
        names[event["task_id"]]: event for event in _events(caplog, "scheduled_run_startup_ended")
    }
    assert {name: event["outcome"] for name, event in ended.items()} == {
        "delivered": "running",
        "failed": "failed",
        "skipped": "skipped",
        "unfinished": "unfinished",
        "interrupted": "interrupted",
    }
    assert ended["delivered"]["prompt_delay_seconds"] == 10
    assert ended["delivered"]["session_id"] == session_id
    assert ended["failed"]["error_code"] == "host_offline"
    # Both unresolved startups are handed to recovery.
    recovered = _events(caplog, "scheduled_run_recovered")
    assert sorted(names[event["task_id"]] for event in recovered) == ["interrupted", "unfinished"]
    assert {event["outcome"] for event in recovered} == {"running"}
    claimed_runs = {event["run_id"] for event in claimed}
    assert {event["run_id"] for event in recovered} <= claimed_runs


@pytest.mark.parametrize(
    "failure",
    [
        "poll",
        "initialize_scan",
        "initialize_task",
        "claim",
        "lease_renewal",
        "recovery_inspection",
    ],
)
async def test_failed_operations_log_debug_events(store, monkeypatch, caplog, failure):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask

    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    release = asyncio.Event()

    async def fire(task, lease):
        await release.wait()
        await lease.finish("running")

    def fail(*args, **kwargs):
        raise RuntimeError(f"{failure} unavailable")

    if failure == "poll":
        monkeypatch.setattr(store, "list_due_tasks", fail)
    elif failure == "initialize_scan":
        monkeypatch.setattr(store, "list_uninitialized_tasks", fail)
    elif failure == "initialize_task":
        with store._session_immediate("test_uninitialized_task") as session:
            session.execute(
                update(SqlScheduledTask)
                .where(SqlScheduledTask.id == task.id)
                .values(next_run_at=None)
            )
        monkeypatch.setattr(store, "initialize_next_run_at", fail)
    elif failure == "claim":
        monkeypatch.setattr(store, "claim_scheduled_run", fail)
    elif failure == "lease_renewal":
        original_renew = store.renew_starting_run
        renewals = 0

        def renew_failing_once(*args, **kwargs):
            nonlocal renewals
            renewals += 1
            # The executor's first check succeeds; a heartbeat renewal fails once.
            if renewals == 2:
                raise RuntimeError("renewal unavailable")
            return original_renew(*args, **kwargs)

        monkeypatch.setattr(store, "renew_starting_run", renew_failing_once)
    elif failure == "recovery_inspection":
        store.claim_scheduled_run(
            task,
            now=task.next_run_at,
            next_run_at=task.next_run_at + 3600,
            host_id=None,
            lease_seconds=120,
        )
        clock.advance(120)
    expected = {
        "poll": ("scheduled_task_operation_failed", {"operation": "poll"}),
        "initialize_scan": ("scheduled_task_operation_failed", {"operation": "initialize"}),
        "initialize_task": (
            "scheduled_task_operation_failed",
            {"operation": "initialize", "task_id": task.id},
        ),
        "claim": ("scheduled_task_operation_failed", {"operation": "claim", "task_id": task.id}),
        "lease_renewal": (
            "scheduled_task_operation_failed",
            {"operation": "lease_renewal", "task_id": task.id},
        ),
        "recovery_inspection": (
            "scheduled_run_recovered",
            {"outcome": "error", "task_id": task.id},
        ),
    }
    event_name, attributes = expected[failure]

    def matching():
        return [
            event for event in _events(caplog, event_name) if attributes.items() <= event.items()
        ]

    recover = AsyncMock(side_effect=RuntimeError("inspection failed"))
    scheduler = PersistentTaskScheduler(
        store, fire, recover, now=clock.now, renewal_seconds=0.01, poll_seconds=3600
    )
    try:
        await scheduler.start()
        await poll_until_async(matching)
        if failure == "initialize_scan":
            assert all("task_id" not in event for event in matching())
        if failure == "lease_renewal":
            assert matching()[0]["run_id"] == store.list_runs(task.id)[0][0].id
    finally:
        release.set()
        await scheduler.stop()


async def test_failed_outcome_log_does_not_fail_committed_finish(store, monkeypatch, caplog):
    from omnigent.server.scheduled import dispatch_scheduler

    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    original_info = dispatch_scheduler._logger.info

    def broken_info(message, *args, **kwargs):
        if kwargs.get("extra", {}).get("event_name") == "scheduled_run_startup_ended":
            raise RuntimeError("debug-log sink unavailable")
        return original_info(message, *args, **kwargs)

    monkeypatch.setattr(dispatch_scheduler._logger, "info", broken_info)
    await lease.finish("running")
    assert lease.run.status == store.list_runs(task.id)[0][0].status == "running"
    assert "Could not log a scheduler event" in caplog.text


async def test_recovery_failure_after_timeout_is_still_logged(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    clock.advance(120)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def failing_transition(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "transition_starting_run", failing_transition)
    recover = AsyncMock(return_value="running")
    scheduler = PersistentTaskScheduler(
        store, AsyncMock(), recover, now=clock.now, recovery_seconds=0.1
    )
    try:
        await scheduler.poll_once()
        await asyncio.wait_for(entered.wait(), 10)
        await asyncio.wait_for(_drain(scheduler), 10)
        release.set()
        await poll_until_async(lambda: len(_events(caplog, "scheduled_run_recovered")) == 2)
        assert [event["outcome"] for event in _events(caplog, "scheduled_run_recovered")] == [
            "timeout",
            "error",
        ]
        assert "failed after its sweep timed out" in caplog.text
        assert store.list_runs(task.id)[0][0].status == "starting"
    finally:
        release.set()
        await scheduler.stop()


async def test_logged_delays_exclude_database_write_time(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at + 4)
    run = store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    original = store.transition_starting_run

    def slow_transition(*args, **kwargs):
        clock.advance(30)  # A slow database write.
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "transition_starting_run", slow_transition)
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    await lease.attach_session(uuid.uuid4().hex)
    await lease.finish("running")
    [attached] = _events(caplog, "scheduled_run_session_attached")
    [ended] = _events(caplog, "scheduled_run_startup_ended")
    assert attached["session_delay_seconds"] == 4
    assert ended["prompt_delay_seconds"] == 34


async def test_concurrent_session_attachment_logs_one_session(store, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    session_id = uuid.uuid4().hex
    await asyncio.gather(lease.attach_session(session_id), lease.attach_session(session_id))
    assert [
        event["session_id"] for event in _events(caplog, "scheduled_run_session_attached")
    ] == [session_id]


async def test_failed_startup_error_text_is_not_logged_as_an_attribute(store, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    secret = "token=sk-live-123 for alice@example.com"
    await lease.finish("failed", error=secret, error_code="host_offline")
    [ended] = _events(caplog, "scheduled_run_startup_ended")
    assert ended["error_code"] == "host_offline"
    assert all(secret not in str(value) for value in ended.values())
    assert store.list_runs(task.id)[0][0].error == secret


async def test_recovery_failure_after_stop_is_still_logged(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    clock.advance(120)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def failing_transition(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "transition_starting_run", failing_transition)
    scheduler = PersistentTaskScheduler(
        store, AsyncMock(), AsyncMock(return_value="running"), now=clock.now
    )
    try:
        await scheduler.poll_once()
        await asyncio.wait_for(entered.wait(), 10)
        # Stopping cancels the sweep while its shielded update is still running.
        await asyncio.wait_for(scheduler.stop(), 10)
        release.set()
        await poll_until_async(lambda: _events(caplog, "scheduled_run_recovered"))
        assert [event["outcome"] for event in _events(caplog, "scheduled_run_recovered")] == [
            "error"
        ]
    finally:
        release.set()
        await scheduler.stop()


async def test_failing_claim_log_still_starts_the_claimed_run(store, monkeypatch, caplog):
    from omnigent.server.scheduled import dispatch_scheduler

    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    original_info = dispatch_scheduler._logger.info

    def broken_info(message, *args, **kwargs):
        if kwargs.get("extra", {}).get("event_name") == "scheduled_run_claimed":
            raise RuntimeError("debug-log sink unavailable")
        return original_info(message, *args, **kwargs)

    monkeypatch.setattr(dispatch_scheduler._logger, "info", broken_info)

    async def fire(task, lease):
        await lease.finish("running")

    scheduler = PersistentTaskScheduler(store, fire, AsyncMock(), now=clock.now)
    try:
        await scheduler.poll_once()
        await _drain(scheduler)
        assert store.list_runs(task.id)[0][0].status == "running"
        assert "Could not log a scheduler event" in caplog.text
        assert "Could not prepare a scheduled run" not in caplog.text
    finally:
        await scheduler.stop()


async def test_event_records_keep_their_call_site(store, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)

    async def fire(task, lease):
        await lease.finish("running")

    scheduler = PersistentTaskScheduler(store, fire, AsyncMock(), now=clock.now)
    try:
        await scheduler.poll_once()
        await _drain(scheduler)
    finally:
        await scheduler.stop()
    [claimed] = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "scheduled_run_claimed"
    ]
    # Dashboards group by function name, so the guard must not replace the caller.
    assert claimed.funcName == "_claim_due"


async def test_changed_session_is_logged_again(store, caplog):
    caplog.set_level(logging.INFO, logger=_SCHEDULER_LOGGER)
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    run = store.claim_scheduled_run(
        task,
        now=task.next_run_at,
        next_run_at=task.next_run_at + 3600,
        host_id=None,
        lease_seconds=120,
    )
    lease = StartupLease(store, run, now=clock.now, lease_seconds=120)
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    await lease.attach_session(first)
    await lease.attach_session(first)
    await lease.attach_session(second)
    sessions = [event["session_id"] for event in _events(caplog, "scheduled_run_session_attached")]
    assert sessions == [first, second]
    assert store.list_runs(task.id)[0][0].conversation_id == second


async def test_finished_schedules_are_completed_and_not_rescanned(store, monkeypatch):
    store.configure_dispatch(False)
    ended = _task(store, "ended", rrule="FREQ=HOURLY;UNTIL=20200101T000000Z")
    store.configure_dispatch(True)
    counted = _task(store, "counted", rrule="FREQ=HOURLY;COUNT=2")
    clock = FakeClock(counted.next_run_at)

    async def fire(task, lease):
        await lease.finish("running")

    scheduler = PersistentTaskScheduler(store, fire, AsyncMock(), now=clock.now)
    try:
        for _ in range(4):
            await scheduler.poll_once()
            await _drain(scheduler)
            clock.advance(3600)
        assert store.get(ended.id).state == "completed"
        assert store.list_runs(ended.id)[0] == []
        assert store.get(counted.id).state == "completed"
        assert len(store.list_runs(counted.id)[0]) == 2
        assert store.list_uninitialized_tasks() == []
    finally:
        await scheduler.stop()
