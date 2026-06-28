"""Tests for SqlAlchemyJobStore (jobs + runs)."""

from __future__ import annotations

from omnigent.stores.job_store.sqlalchemy_store import SqlAlchemyJobStore


def test_create_and_get_job(job_store: SqlAlchemyJobStore) -> None:
    """A created job is fetchable and timestamps are stamped equal."""
    job = job_store.create_job(name="Flow", graph="{}", narrative="Do X.", created_by="u1")
    assert job.id.startswith("job_")
    assert job.created_at == job.updated_at
    fetched = job_store.get_job(job.id)
    assert fetched is not None
    assert fetched.name == "Flow"
    assert fetched.narrative == "Do X."


def test_get_nonexistent_job(job_store: SqlAlchemyJobStore) -> None:
    """A missing job id returns None."""
    assert job_store.get_job("job_missing") is None


def test_list_jobs_scoped_by_owner(job_store: SqlAlchemyJobStore) -> None:
    """``created_by`` filters the listing; ``None`` returns all."""
    job_store.create_job(name="A", graph="{}", narrative="a", created_by="u1")
    job_store.create_job(name="B", graph="{}", narrative="b", created_by="u2")
    assert [j.name for j in job_store.list_jobs(created_by="u1")] == ["A"]
    assert len(job_store.list_jobs()) == 2


def test_update_job_bumps_updated_at(job_store: SqlAlchemyJobStore) -> None:
    """Patching a field updates it and leaves untouched fields intact."""
    job = job_store.create_job(name="Old", graph="{}", narrative="old")
    updated = job_store.update_job(job.id, name="New")
    assert updated is not None
    assert updated.name == "New"
    assert updated.narrative == "old"
    assert updated.updated_at >= job.updated_at


def test_update_missing_job_returns_none(job_store: SqlAlchemyJobStore) -> None:
    """Updating an unknown job is a no-op returning None."""
    assert job_store.update_job("job_missing", name="x") is None


def test_delete_job(job_store: SqlAlchemyJobStore) -> None:
    """Deleting a job returns True once, then the job is gone."""
    job = job_store.create_job(name="D", graph="{}", narrative="d")
    assert job_store.delete_job(job.id) is True
    assert job_store.get_job(job.id) is None
    assert job_store.delete_job(job.id) is False


def test_schedule_config_round_trips(job_store: SqlAlchemyJobStore) -> None:
    """``schedule_config`` is persisted on create and patchable via update."""
    job = job_store.create_job(
        name="S", graph="{}", narrative="s", schedule_config='{"enabled": true}'
    )
    assert job.schedule_config == '{"enabled": true}'
    updated = job_store.update_job(job.id, schedule_config='{"enabled": false}')
    assert updated is not None
    assert updated.schedule_config == '{"enabled": false}'


def test_list_scheduled_jobs_only_returns_jobs_with_config(
    job_store: SqlAlchemyJobStore,
) -> None:
    """``list_scheduled_jobs`` returns only jobs with a non-null schedule_config."""
    job_store.create_job(name="plain", graph="{}", narrative="n")
    scheduled = job_store.create_job(
        name="sched", graph="{}", narrative="n", schedule_config='{"enabled": true}'
    )
    ids = {j.id for j in job_store.list_scheduled_jobs()}
    assert ids == {scheduled.id}


def test_create_run_defaults_adhoc_and_persists_scheduled(
    job_store: SqlAlchemyJobStore,
) -> None:
    """A run defaults to the adhoc trigger; a scheduled trigger round-trips."""
    job = job_store.create_job(name="T", graph="{}", narrative="go")
    adhoc = job_store.create_run(job_id=job.id, session_id=None)
    assert adhoc.trigger == "adhoc"
    scheduled = job_store.create_run(job_id=job.id, session_id=None, trigger="scheduled")
    assert scheduled.trigger == "scheduled"
    assert job_store.get_run(scheduled.id).trigger == "scheduled"


def test_host_id_round_trips_and_clears(job_store: SqlAlchemyJobStore) -> None:
    """``host_id`` persists on create, patches on update, and clears on empty string."""
    job = job_store.create_job(name="H", graph="{}", narrative="n", host_id="host_1")
    assert job.host_id == "host_1"
    # Patch to a different host.
    updated = job_store.update_job(job.id, host_id="host_2")
    assert updated is not None and updated.host_id == "host_2"
    # None leaves it unchanged.
    unchanged = job_store.update_job(job.id, name="renamed")
    assert unchanged is not None and unchanged.host_id == "host_2"
    # Empty string clears the binding.
    cleared = job_store.update_job(job.id, host_id="")
    assert cleared is not None and cleared.host_id is None


def test_run_lifecycle_and_cascade(job_store: SqlAlchemyJobStore) -> None:
    """Runs are created, filtered by status, and cascade-deleted with the job."""
    job = job_store.create_job(name="R", graph="{}", narrative="go")
    run = job_store.create_run(job_id=job.id, session_id=None)
    assert run.id.startswith("run_")
    assert run.status == "running"

    finished = job_store.update_run_status(
        run.id, status="finished", completed_at=run.started_at + 5
    )
    assert finished is not None
    assert finished.status == "finished"
    assert finished.completed_at == run.started_at + 5

    assert len(job_store.list_runs(job_id=job.id, status="finished")) == 1
    assert len(job_store.list_runs(job_id=job.id, status="running")) == 0

    job_store.delete_job(job.id)
    assert job_store.get_run(run.id) is None
