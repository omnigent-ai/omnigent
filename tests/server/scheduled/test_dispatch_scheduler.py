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
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.scheduled.test_scheduler import FakeClock


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyScheduledTaskStore:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    return store


def _task(store: SqlAlchemyScheduledTaskStore, seed: str = "task", **kwargs):
    task = store.create(
        uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex,
        seed,
        "prompt",
        "FREQ=HOURLY",
        None,
        uuid.uuid4().hex,
        "UTC",
        **kwargs,
    )
    return store.get(task.id)


async def _drain(scheduler: PersistentTaskScheduler) -> None:
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
    scheduler = PersistentTaskScheduler(store, fire, now=clock.now)
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
    scheduler = PersistentTaskScheduler(store, AsyncMock())
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
    scheduler = PersistentTaskScheduler(store, AsyncMock(), now=clock.now)
    await scheduler.poll_once()
    await _drain(scheduler)
    run = store.list_runs(task.id)[0][0]
    assert run.status == "starting"
    assert "no final startup outcome was recorded" in caplog.text


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

    schedulers = [PersistentTaskScheduler(store, fire, now=clock.now) for _ in range(2)]
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

    scheduler = PersistentTaskScheduler(store, fire, owns_host=owns, now=clock.now, batch_size=1)
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

    scheduler = PersistentTaskScheduler(store, fire, now=clock.now, max_startups=1)
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

    scheduler = PersistentTaskScheduler(store, fire, now=clock.now, renewal_seconds=0.01)
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
    "interruption", ["lease_loss", "renewal_timeout", "stop", "executor_error"]
)
async def test_interrupted_executor_stops_and_leaves_run_recoverable(
    store, monkeypatch, caplog, interruption
):
    task = _task(store)
    clock = FakeClock(task.next_run_at)
    entered = asyncio.Event()
    exited = asyncio.Event()
    renewal_blocked = asyncio.Event()
    release = threading.Event()
    leases = []
    loop = asyncio.get_running_loop()
    original_renew = store.renew_starting_run

    def hanging_renewal(*args, **kwargs):
        loop.call_soon_threadsafe(renewal_blocked.set)
        assert release.wait(10)
        return original_renew(*args, **kwargs)

    async def fire(task, lease):
        leases.append(lease)
        entered.set()
        try:
            if interruption == "executor_error":
                raise RuntimeError("executor could not complete startup")
            await asyncio.Event().wait()
        finally:
            exited.set()

    scheduler = PersistentTaskScheduler(store, fire, now=clock.now, renewal_seconds=0.01)
    try:
        await scheduler.poll_once()
        startups = list(scheduler._startups.values())
        await asyncio.wait_for(entered.wait(), 10)
        if interruption == "lease_loss":
            monkeypatch.setattr(store, "renew_starting_run", lambda *args, **kwargs: None)
        elif interruption == "renewal_timeout":
            monkeypatch.setattr(store, "renew_starting_run", hanging_renewal)
            clock.advance(119)
            await asyncio.wait_for(renewal_blocked.wait(), 10)
            clock.advance(1)
        elif interruption == "stop":
            await asyncio.wait_for(scheduler.stop(), 10)
        await asyncio.wait_for(asyncio.gather(*startups, return_exceptions=True), 10)
        assert exited.is_set()
        assert all(startup.cancelled() or startup.exception() is None for startup in startups)
        assert scheduler._startups == {}
        run = store.list_runs(task.id)[0][0]
        assert run.status == "starting"
        assert run.lease_expires_at is not None
        records = [
            record for record in caplog.records if record.name.endswith("dispatch_scheduler")
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
        if interruption == "executor_error":
            assert "executor could not complete startup" in caplog.text
            assert records[0].levelno == logging.ERROR
            assert records[0].exc_info is not None
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
    scheduler = PersistentTaskScheduler(store, fire, now=clock.now, owns_host=owns)
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
    scheduler = PersistentTaskScheduler(store, AsyncMock(), poll_seconds=0.01)
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


def test_d1_dispatch_is_rejected_before_polling(store, monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        store, "_engine", SimpleNamespace(dialect=SimpleNamespace(name="cloudflare_d1"))
    )
    with pytest.raises(NotImplementedError, match="multi-statement transactions"):
        PersistentTaskScheduler(store, AsyncMock())


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
    scheduler = PersistentTaskScheduler(store, fire, now=clock.now)
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

    scheduler = PersistentTaskScheduler(store, fire, now=clock.now)
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
    clock = FakeClock(due.next_run_at + 120)
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

    scheduler = PersistentTaskScheduler(store, fire, now=clock.now, batch_size=2)
    try:
        for expected in (2, 4, 5):
            await scheduler.poll_once()
            await _drain(scheduler)
            assert len(attempted) == expected
        assert attempted == [task.id for task in legacy]
        assert store.get(legacy[0].id).next_run_at is None
        assert all(store.get(task.id).next_run_at is not None for task in legacy[1:])
        assert store.list_runs(due.id)[0][0].status == "running"
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
    scheduler = PersistentTaskScheduler(store, AsyncMock(), now=clock.now, renewal_seconds=0.01)
    with pytest.raises(StartupLeaseLost, match="expired"):
        await scheduler._heartbeat(lease)
    assert attempts == 2
    assert store.list_runs(task.id)[0] == [run]
