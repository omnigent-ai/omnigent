"""Job store — manages saved AI workflows (jobs) and their runs.

A *job* is a node-graph workflow stored as opaque JSON plus a client-rendered
English narrative. A *run* records one execution of a job (an agent session).
This store owns both jobs and runs, since a run is subordinate to its job and a
single backend/session-maker keeps the wiring simple.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import Job, Run


class JobStore(ABC):
    """
    Abstract base for job + run persistence.

    Manages the lifecycle of saved jobs (CRUD, scoped per owner) and their
    runs (create, list, status updates).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the job store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///jobs.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    # --- Jobs -------------------------------------------------------------

    @abstractmethod
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
        """
        Create a new job. The store mints the id.

        :param name: Human-readable job name.
        :param graph: Opaque flow-graph JSON string.
        :param narrative: English narrative rendered from the graph.
        :param agent_id: Agent the job runs as, or ``None``.
        :param harness_override: Optional harness override for runs.
        :param model_override: Optional model override for runs.
        :param created_by: Owning user id, or ``None`` in single-user mode.
        :returns: The newly created :class:`Job`.
        """
        ...

    @abstractmethod
    def get_job(self, job_id: str) -> Job | None:
        """
        Return the job, or ``None`` if it does not exist.

        :param job_id: Unique job identifier, e.g. ``"job_abc123"``.
        :returns: The :class:`Job` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list_jobs(self, *, created_by: str | None = None) -> list[Job]:
        """
        List jobs, newest-updated first.

        :param created_by: When set, only return jobs owned by this user.
            When ``None`` (single-user mode), return all jobs.
        :returns: Jobs ordered by ``updated_at`` descending.
        """
        ...

    @abstractmethod
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
        """
        Patch the given fields and bump ``updated_at``. Only non-``None``
        arguments are applied. Returns the updated job, or ``None`` if the
        id is unknown.

        :param job_id: Unique job identifier, e.g. ``"job_abc123"``.
        :returns: The updated :class:`Job`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def delete_job(self, job_id: str) -> bool:
        """
        Delete a job (and, via cascade, its runs). Returns ``True`` if the
        job existed, ``False`` otherwise.

        :param job_id: Unique job identifier, e.g. ``"job_abc123"``.
        :returns: ``True`` if deleted, ``False`` if it did not exist.
        """
        ...

    # --- Runs -------------------------------------------------------------

    @abstractmethod
    def create_run(
        self,
        *,
        job_id: str,
        session_id: str | None,
        status: str = "running",
        created_by: str | None = None,
    ) -> Run:
        """
        Record a new run for a job. The store mints the id and stamps
        ``started_at``.

        :param job_id: The job this run belongs to.
        :param session_id: The session created for this run.
        :param status: Initial status, normally ``"running"``.
        :param created_by: Owning user id, or ``None``.
        :returns: The newly created :class:`Run`.
        """
        ...

    @abstractmethod
    def get_run(self, run_id: str) -> Run | None:
        """
        Return the run, or ``None`` if it does not exist.

        :param run_id: Unique run identifier, e.g. ``"run_abc123"``.
        :returns: The :class:`Run` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list_runs(self, *, job_id: str, status: str | None = None) -> list[Run]:
        """
        List runs for a job, newest-started first.

        :param job_id: The job whose runs to list.
        :param status: Optional status filter (``running`` / ``finished`` /
            ``failed``).
        :returns: Runs ordered by ``started_at`` descending.
        """
        ...

    @abstractmethod
    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        completed_at: int | None = None,
        error: str | None = None,
    ) -> Run | None:
        """
        Update a run's status (and optionally ``completed_at`` / ``error``).
        Returns the updated run, or ``None`` if the id is unknown.

        :param run_id: Unique run identifier, e.g. ``"run_abc123"``.
        :param status: New status.
        :param completed_at: Terminal timestamp, when transitioning to a
            terminal state.
        :param error: Failure detail, when ``status == "failed"``.
        :returns: The updated :class:`Run`, or ``None`` if not found.
        """
        ...
