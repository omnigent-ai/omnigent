"""Routes for Jobs/Workflows CRUD and execution.

A *job* is a saved AI workflow authored as a node graph in the web UI. Its
execution model is *promptgen*: the graph is rendered to an English narrative
client-side, persisted on the job, and fed as the initial prompt to a single
agent session when the job runs. A *run* records one such execution — it *is*
an agent session.

Endpoints (all under ``/v1``):

- ``POST/GET/GET{id}/PATCH{id}/DELETE{id}`` ``/jobs`` — job CRUD, scoped per user.
- ``POST /jobs/{id}/run`` — "Run now": create a session from the job's narrative
  and record a run.
- ``GET /jobs/{id}/runs`` — list a job's runs (optional ``?status=`` filter).
- ``GET /runs/{id}`` — a single run, with status reconciled from its session.

The backend never interprets the graph; it stores it as opaque JSON.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from fastapi import APIRouter, Request

from omnigent.db.utils import now_epoch
from omnigent.entities import (
    RUN_STATUS_FAILED,
    RUN_STATUS_FINISHED,
    RUN_STATUS_RUNNING,
    Job,
    Run,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, require_user
from omnigent.server.routes.sessions import (
    SessionLiveness,
    _announce_session_added,
    _create_session_from_existing_agent,
    _session_status_from_cache,
)
from omnigent.server.schemas import (
    JobCreateRequest,
    JobResponse,
    JobUpdateRequest,
    RunResponse,
    SessionCreateRequest,
    SessionEventInput,
)
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.job_store import JobStore
from omnigent.stores.permission_store import PermissionStore


def _job_to_response(job: Job) -> JobResponse:
    """Convert a :class:`Job` entity to a :class:`JobResponse`.

    Parses the stored opaque ``graph`` JSON back into an object for the wire.

    :param job: The job entity to convert.
    :returns: The wire-shaped response.
    """
    try:
        graph = json.loads(job.graph) if job.graph else {}
    except json.JSONDecodeError:
        # The graph is opaque to us; if it somehow isn't valid JSON, surface
        # the raw string rather than 500ing a read.
        graph = {"raw": job.graph}
    return JobResponse(
        id=job.id,
        name=job.name,
        graph=graph,
        narrative=job.narrative,
        agent_id=job.agent_id,
        harness_override=job.harness_override,
        model_override=job.model_override,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _run_to_response(run: Run) -> RunResponse:
    """Convert a :class:`Run` entity to a :class:`RunResponse`.

    :param run: The run entity to convert.
    :returns: The wire-shaped response.
    """
    return RunResponse(
        id=run.id,
        job_id=run.job_id,
        session_id=run.session_id,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error=run.error,
    )


def _reconcile_run(run: Run, job_store: JobStore) -> Run:
    """Reconcile a still-``running`` run's status from its session.

    A run is recorded ``running`` at creation. Since a promptgen run is just an
    agent session (no DAG), the terminal state is derived from the underlying
    session's live status: a ``failed`` session fails the run; an ``idle``
    session (loop finished or never started) marks it ``finished``. The
    reconciled state is persisted so subsequent reads are cheap.

    :param run: The run to reconcile.
    :param job_store: Store used to persist a terminal transition.
    :returns: The (possibly updated) run.
    """
    if run.status != RUN_STATUS_RUNNING or run.session_id is None:
        return run
    session_status = _session_status_from_cache(run.session_id)
    if session_status == "failed":
        updated = job_store.update_run_status(
            run.id, status=RUN_STATUS_FAILED, completed_at=now_epoch()
        )
        return updated or run
    if session_status == "idle":
        updated = job_store.update_run_status(
            run.id, status=RUN_STATUS_FINISHED, completed_at=now_epoch()
        )
        return updated or run
    return run


async def _execute_job_run(
    job: Job,
    request: Request,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    job_store: JobStore,
    runner_router: RunnerRouter | None,
    agent_cache: AgentCache | None,
    user_id: str | None,
    permission_store: PermissionStore | None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    default_run_agent_id: str | None,
) -> Run:
    """Run a job: create a session from its narrative and record a run.

    This is the single execution path shared by the HTTP "Run now" handler and
    any future scheduler — both build a session from the job's stored narrative
    and bound agent, then persist a run row. A scheduler would load jobs with a
    non-null ``schedule_config`` and call this with ``user_id`` set to the job
    owner.

    :param job: The job to run.
    :param request: The originating request (forwarded to session creation).
    :param default_run_agent_id: Fallback agent when the job has none bound.
    :returns: The created :class:`Run`.
    :raises OmnigentError: 400 if no agent is bound and no default exists.
    """
    agent_id = job.agent_id or default_run_agent_id
    if agent_id is None:
        raise OmnigentError(
            "Job has no agent bound and no default agent is configured; "
            "set an agent on the job before running it.",
            code=ErrorCode.INVALID_INPUT,
        )

    body = SessionCreateRequest(
        agent_id=agent_id,
        title=f"Run: {job.name}",
        harness_override=job.harness_override,
        model_override=job.model_override,
        # No host is required: with host_type="external" and no bound runner,
        # the narrative is persisted as a seed item and the session is created
        # idle, ready to run when opened (or by a bound runner).
        host_type="external",
        initial_items=[
            SessionEventInput(
                type="message",
                data={
                    "role": "user",
                    "content": [{"type": "input_text", "text": job.narrative}],
                },
            )
        ],
    )
    session = await _create_session_from_existing_agent(
        conversation_store,
        agent_store,
        runner_router,
        body,
        request,
        agent_cache=agent_cache,
        user_id=user_id,
        permission_store=permission_store,
        liveness_lookup=liveness_lookup,
        file_store=file_store,
        artifact_store=artifact_store,
    )
    # Grant the creator ownership and surface the session in their open tabs,
    # mirroring the POST /v1/sessions route wrapper.
    if permission_store is not None and user_id is not None:
        await asyncio.to_thread(permission_store.ensure_user, user_id)
        await asyncio.to_thread(permission_store.grant, user_id, session.id, LEVEL_OWNER)
    _announce_session_added(user_id, session.id)

    return await asyncio.to_thread(
        job_store.create_run,
        job_id=job.id,
        session_id=session.id,
        status=RUN_STATUS_RUNNING,
        created_by=attribution_user(user_id),
    )


def create_jobs_router(
    job_store: JobStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    default_run_agent_id: str | None = None,
) -> APIRouter:
    """Build the jobs router.

    :param job_store: Store for job + run persistence.
    :param conversation_store: Store used when creating run sessions.
    :param agent_store: Store used to resolve a job's bound agent.
    :param runner_router: Runner router, forwarded to session creation.
    :param auth_provider: Auth provider used to identify the caller.
    :param permission_store: Permission store for ownership grants/scoping.
    :param agent_cache: Optional agent-spec cache, forwarded to session create.
    :param file_store: Optional file store, forwarded to session create.
    :param artifact_store: Optional artifact store, forwarded to session create.
    :param liveness_lookup: Optional liveness lookup, forwarded to session create.
    :param default_run_agent_id: Fallback agent id for jobs with none bound.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner_scope(user_id: str | None) -> str | None:
        """The ``created_by`` filter for list/ownership checks.

        ``None`` in single-user mode (no scoping); the attribution actor
        otherwise.
        """
        return attribution_user(user_id) if permission_store is not None else None

    async def _load_owned_job(job_id: str, user_id: str | None) -> Job:
        """Fetch a job and enforce ownership, or raise 404.

        Returns 404 (not 403) for another user's job so existence isn't
        leaked across tenants.
        """
        job = await asyncio.to_thread(job_store.get_job, job_id)
        scope = _owner_scope(user_id)
        if job is None or (scope is not None and job.created_by != scope):
            raise OmnigentError(f"Job not found: {job_id!r}", code=ErrorCode.NOT_FOUND)
        return job

    @router.post("/jobs", status_code=201, response_model=JobResponse)
    async def create_job(request: Request, body: JobCreateRequest) -> JobResponse:
        """Create a job."""
        user_id = require_user(request, auth_provider)
        job = await asyncio.to_thread(
            job_store.create_job,
            name=body.name,
            graph=json.dumps(body.graph),
            narrative=body.narrative,
            agent_id=body.agent_id,
            harness_override=body.harness_override,
            model_override=body.model_override,
            created_by=attribution_user(user_id),
        )
        return _job_to_response(job)

    @router.get("/jobs", response_model=list[JobResponse])
    async def list_jobs(request: Request) -> list[JobResponse]:
        """List the caller's jobs, newest-updated first."""
        user_id = require_user(request, auth_provider)
        jobs = await asyncio.to_thread(job_store.list_jobs, created_by=_owner_scope(user_id))
        return [_job_to_response(j) for j in jobs]

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(request: Request, job_id: str) -> JobResponse:
        """Fetch one job."""
        user_id = require_user(request, auth_provider)
        return _job_to_response(await _load_owned_job(job_id, user_id))

    @router.patch("/jobs/{job_id}", response_model=JobResponse)
    async def update_job(request: Request, job_id: str, body: JobUpdateRequest) -> JobResponse:
        """Patch a job's fields."""
        user_id = require_user(request, auth_provider)
        await _load_owned_job(job_id, user_id)
        graph = json.dumps(body.graph) if body.graph is not None else None
        updated = await asyncio.to_thread(
            job_store.update_job,
            job_id,
            name=body.name,
            graph=graph,
            narrative=body.narrative,
            agent_id=body.agent_id,
            harness_override=body.harness_override,
            model_override=body.model_override,
        )
        if updated is None:
            raise OmnigentError(f"Job not found: {job_id!r}", code=ErrorCode.NOT_FOUND)
        return _job_to_response(updated)

    @router.delete("/jobs/{job_id}", status_code=204)
    async def delete_job(request: Request, job_id: str) -> None:
        """Delete a job (and, via cascade, its runs)."""
        user_id = require_user(request, auth_provider)
        await _load_owned_job(job_id, user_id)
        await asyncio.to_thread(job_store.delete_job, job_id)

    @router.post("/jobs/{job_id}/run", status_code=201, response_model=RunResponse)
    async def run_job(request: Request, job_id: str) -> RunResponse:
        """Run a job now: create a session from its narrative + record a run."""
        user_id = require_user(request, auth_provider)
        job = await _load_owned_job(job_id, user_id)
        run = await _execute_job_run(
            job,
            request,
            conversation_store=conversation_store,
            agent_store=agent_store,
            job_store=job_store,
            runner_router=runner_router,
            agent_cache=agent_cache,
            user_id=user_id,
            permission_store=permission_store,
            liveness_lookup=liveness_lookup,
            file_store=file_store,
            artifact_store=artifact_store,
            default_run_agent_id=default_run_agent_id,
        )
        return _run_to_response(run)

    @router.get("/jobs/{job_id}/runs", response_model=list[RunResponse])
    async def list_runs(
        request: Request, job_id: str, status: str | None = None
    ) -> list[RunResponse]:
        """List a job's runs, newest-started first, with status reconciled."""
        user_id = require_user(request, auth_provider)
        await _load_owned_job(job_id, user_id)
        runs = await asyncio.to_thread(job_store.list_runs, job_id=job_id, status=status)
        reconciled = [await asyncio.to_thread(_reconcile_run, r, job_store) for r in runs]
        # Re-apply the status filter post-reconcile so a now-finished run
        # doesn't linger in a ``?status=running`` view.
        if status is not None:
            reconciled = [r for r in reconciled if r.status == status]
        return [_run_to_response(r) for r in reconciled]

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(request: Request, run_id: str) -> RunResponse:
        """Fetch one run, with status reconciled from its session."""
        user_id = require_user(request, auth_provider)
        run = await asyncio.to_thread(job_store.get_run, run_id)
        if run is not None:
            # Enforce ownership via the parent job (404 on miss/cross-tenant).
            await _load_owned_job(run.job_id, user_id)
            run = await asyncio.to_thread(_reconcile_run, run, job_store)
        if run is None:
            raise OmnigentError(f"Run not found: {run_id!r}", code=ErrorCode.NOT_FOUND)
        return _run_to_response(run)

    return router
