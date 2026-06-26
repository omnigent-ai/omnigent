"""SQLAlchemy-backed job + run store."""

from __future__ import annotations

from sqlalchemy import desc, select

from omnigent.db.converters import sql_job_to_entity, sql_run_to_entity
from omnigent.db.db_models import SqlJob, SqlRun
from omnigent.db.utils import (
    generate_job_id,
    generate_run_id,
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Job, Run
from omnigent.stores.job_store import JobStore


class SqlAlchemyJobStore(JobStore):
    """
    SQLAlchemy-backed implementation of :class:`JobStore`.

    Persists jobs and their runs in a relational database via SQLAlchemy ORM.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy job store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///jobs.db"`` or
            ``"postgresql://user:pass@host/db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    # --- Jobs -------------------------------------------------------------

    def create_job(
        self,
        *,
        name: str,
        graph: str,
        narrative: str,
        agent_id: str | None = None,
        harness_override: str | None = None,
        model_override: str | None = None,
        created_by: str | None = None,
    ) -> Job:
        """Create a new job. See :meth:`JobStore.create_job`."""
        ts = now_epoch()
        row = SqlJob(
            id=generate_job_id(),
            created_at=ts,
            updated_at=ts,
            name=name,
            graph=graph,
            narrative=narrative,
            agent_id=agent_id,
            harness_override=harness_override,
            model_override=model_override,
            created_by=created_by,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return sql_job_to_entity(row)

    def get_job(self, job_id: str) -> Job | None:
        """Fetch a job by id. See :meth:`JobStore.get_job`."""
        with self._session() as session:
            row = session.get(SqlJob, job_id)
            return sql_job_to_entity(row) if row else None

    def list_jobs(self, *, created_by: str | None = None) -> list[Job]:
        """List jobs newest-updated first. See :meth:`JobStore.list_jobs`."""
        with self._session() as session:
            stmt = select(SqlJob)
            if created_by is not None:
                stmt = stmt.where(SqlJob.created_by == created_by)
            stmt = stmt.order_by(desc(SqlJob.updated_at), desc(SqlJob.id))
            rows = session.execute(stmt).scalars().all()
            return [sql_job_to_entity(r) for r in rows]

    def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        graph: str | None = None,
        narrative: str | None = None,
        agent_id: str | None = None,
        harness_override: str | None = None,
        model_override: str | None = None,
    ) -> Job | None:
        """Patch a job and bump ``updated_at``. See :meth:`JobStore.update_job`."""
        with self._session() as session:
            row = session.get(SqlJob, job_id)
            if not row:
                return None
            if name is not None:
                row.name = name
            if graph is not None:
                row.graph = graph
            if narrative is not None:
                row.narrative = narrative
            if agent_id is not None:
                row.agent_id = agent_id
            if harness_override is not None:
                row.harness_override = harness_override
            if model_override is not None:
                row.model_override = model_override
            row.updated_at = now_epoch()
            return sql_job_to_entity(row)

    def delete_job(self, job_id: str) -> bool:
        """Delete a job (cascades to runs). See :meth:`JobStore.delete_job`."""
        with self._session() as session:
            row = session.get(SqlJob, job_id)
            if not row:
                return False
            session.delete(row)
            return True

    # --- Runs -------------------------------------------------------------

    def create_run(
        self,
        *,
        job_id: str,
        session_id: str | None,
        status: str = "running",
        created_by: str | None = None,
    ) -> Run:
        """Record a new run. See :meth:`JobStore.create_run`."""
        row = SqlRun(
            id=generate_run_id(),
            job_id=job_id,
            session_id=session_id,
            status=status,
            started_at=now_epoch(),
            created_by=created_by,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return sql_run_to_entity(row)

    def get_run(self, run_id: str) -> Run | None:
        """Fetch a run by id. See :meth:`JobStore.get_run`."""
        with self._session() as session:
            row = session.get(SqlRun, run_id)
            return sql_run_to_entity(row) if row else None

    def list_runs(self, *, job_id: str, status: str | None = None) -> list[Run]:
        """List runs for a job newest-first. See :meth:`JobStore.list_runs`."""
        with self._session() as session:
            stmt = select(SqlRun).where(SqlRun.job_id == job_id)
            if status is not None:
                stmt = stmt.where(SqlRun.status == status)
            stmt = stmt.order_by(desc(SqlRun.started_at), desc(SqlRun.id))
            rows = session.execute(stmt).scalars().all()
            return [sql_run_to_entity(r) for r in rows]

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        completed_at: int | None = None,
        error: str | None = None,
    ) -> Run | None:
        """Update a run's status. See :meth:`JobStore.update_run_status`."""
        with self._session() as session:
            row = session.get(SqlRun, run_id)
            if not row:
                return None
            row.status = status
            if completed_at is not None:
                row.completed_at = completed_at
            if error is not None:
                row.error = error
            return sql_run_to_entity(row)
