"""Background time-trigger scheduler for Jobs/Workflows.

A *job* can carry an opaque ``schedule_config`` (``{"enabled": bool,
"interval_minutes": int}``). This module runs a single asyncio task — started in
the server's ``_lifespan`` (see ``omnigent/server/app.py``), mirroring
``publish_server_metrics_periodically`` — that wakes every five minutes, finds
jobs whose schedule is due, and spawns a new run of each via the same execution
path as the HTTP "Run now" handler (:func:`_execute_job_run`).

Due-ness is derived from the job's most recent *scheduled* run rather than a
stored "last fired" timestamp, so the scheduler never has to write back to the
job: a job is due when ``now - last_scheduled_run.started_at >=
interval_minutes * 60`` (or when it has no prior scheduled run). To avoid
pile-ups, a job is skipped while its latest scheduled run is still ``running``
(reconciled from the underlying session). Manual "Run now" is unaffected.

Assumptions / limits:

- **Single process.** The task runs once per server process; running more than
  one worker/replica would double-fire scheduled runs. This matches the
  single-process assumption of the metrics task. A DB lease would be the fix for
  a multi-replica deploy.
- **Tick granularity.** The poller wakes every 60 seconds, so effective
  scheduling granularity is one minute — the smallest supported interval.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI
from starlette.requests import Request

from omnigent.db.utils import now_epoch
from omnigent.entities import RUN_STATUS_RUNNING, RUN_TRIGGER_SCHEDULED, Job
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.routes.jobs import _execute_job_run, _reconcile_run
from omnigent.server.routes.sessions import SessionLiveness
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.job_store import JobStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# Lower bound on a schedule's interval, in minutes. The poller wakes every 60s,
# so one minute is the smallest interval that can actually fire on time; clamp
# rather than reject so a stray smaller/zero value just means "every minute".
_MIN_INTERVAL_MINUTES = 1


@dataclass(frozen=True)
class _Schedule:
    """A parsed, enabled schedule. ``interval_minutes`` is clamped to the poll cadence."""

    interval_minutes: int


def _parse_schedule_config(raw: str | None) -> _Schedule | None:
    """Parse a job's ``schedule_config`` into an enabled :class:`_Schedule`.

    Returns ``None`` for anything that should not fire: missing config,
    malformed JSON, ``enabled`` falsey, or a non-positive interval.

    :param raw: The opaque ``schedule_config`` JSON string, or ``None``.
    :returns: A :class:`_Schedule` when scheduling is enabled, else ``None``.
    """
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    interval = cfg.get("interval_minutes")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        return None
    return _Schedule(interval_minutes=max(interval, _MIN_INTERVAL_MINUTES))


def _latest_scheduled_run_started_at(job_store: JobStore, job_id: str) -> int | None:
    """Return the ``started_at`` of the job's most recent *scheduled* run.

    Used only as a fast "is it due" check that ignores the run's terminal
    status. ``list_runs`` is newest-started first, so the first scheduled run is
    the most recent.

    :returns: The epoch seconds of the latest scheduled run, or ``None`` if the
        job has never had a scheduled run.
    """
    runs = job_store.list_runs(job_id=job_id)
    for run in runs:
        if run.trigger == RUN_TRIGGER_SCHEDULED:
            return run.started_at
    return None


def _latest_scheduled_run_is_running(
    job_store: JobStore, conversation_store: ConversationStore, job_id: str
) -> bool:
    """Whether the job's latest scheduled run is still ``running`` (reconciled).

    Reconciles the run's status from its session (mirroring the read path) so a
    finished-but-unreconciled run doesn't wedge the schedule.

    :returns: ``True`` if a scheduled run is still in flight (skip this tick).
    """
    runs = job_store.list_runs(job_id=job_id)
    for run in runs:
        if run.trigger == RUN_TRIGGER_SCHEDULED:
            reconciled = _reconcile_run(run, job_store, conversation_store)
            return reconciled.status == RUN_STATUS_RUNNING
    return False


async def _maybe_run_job(
    job: Job,
    request: Request,
    *,
    job_store: JobStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    agent_cache: AgentCache | None,
    permission_store: PermissionStore | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
    default_run_agent_id: str | None,
) -> bool:
    """Spawn a scheduled run for ``job`` if it is enabled, due, and not overlapping.

    :returns: ``True`` if a run was spawned, ``False`` if skipped.
    """
    schedule = _parse_schedule_config(job.schedule_config)
    if schedule is None:
        return False

    # Jobs run as their bound agent; there is no default-agent fallback wired
    # today, so a job with no agent can't be run unattended. Skip + log.
    if job.agent_id is None and default_run_agent_id is None:
        _logger.warning(
            "job scheduler: job %s is scheduled but has no agent bound; skipping",
            job.id,
        )
        return False

    last_started = await asyncio.to_thread(
        _latest_scheduled_run_started_at, job_store, job.id
    )
    if last_started is not None:
        # Overlap guard: don't stack scheduled runs.
        if await asyncio.to_thread(
            _latest_scheduled_run_is_running, job_store, conversation_store, job.id
        ):
            return False
        # Not yet due.
        if now_epoch() - last_started < schedule.interval_minutes * 60:
            return False

    await _execute_job_run(
        job,
        request,
        conversation_store=conversation_store,
        agent_store=agent_store,
        job_store=job_store,
        runner_router=runner_router,
        agent_cache=agent_cache,
        user_id=job.created_by,
        permission_store=permission_store,
        liveness_lookup=liveness_lookup,
        file_store=file_store,
        artifact_store=artifact_store,
        default_run_agent_id=default_run_agent_id,
        trigger=RUN_TRIGGER_SCHEDULED,
    )
    _logger.info("job scheduler: spawned scheduled run for job %s", job.id)
    return True


async def _run_scheduler_tick(
    *,
    app: FastAPI,
    job_store: JobStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    agent_cache: AgentCache | None,
    permission_store: PermissionStore | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
    default_run_agent_id: str | None,
) -> int:
    """Run one scheduler pass: spawn a run for each due, enabled, non-overlapping job.

    Jobs are processed sequentially, each wrapped in its own try/except so one
    slow or failing job neither aborts the pass nor blocks the others.

    :returns: The number of scheduled runs spawned this tick.
    """
    jobs = await asyncio.to_thread(job_store.list_scheduled_jobs)
    if not jobs:
        return 0

    # Build a synthetic request carrying the real app: the run path reads
    # ``request.app.state`` (host registry/store) to pick an online host to
    # launch the runner on, and never touches request headers/body for a job
    # run. Passing the live app means scheduled runs dispatch to a host exactly
    # like the HTTP "Run now" handler; with no host online the narrative is
    # seeded as history (same fallback as the handler).
    request = Request({"type": "http", "method": "POST", "headers": [], "app": app})

    spawned = 0
    for job in jobs:
        try:
            if await _maybe_run_job(
                job,
                request,
                job_store=job_store,
                conversation_store=conversation_store,
                agent_store=agent_store,
                runner_router=runner_router,
                agent_cache=agent_cache,
                permission_store=permission_store,
                file_store=file_store,
                artifact_store=artifact_store,
                liveness_lookup=liveness_lookup,
                default_run_agent_id=default_run_agent_id,
            ):
                spawned += 1
        except Exception:
            # One bad job must not abort the pass or block the others.
            _logger.exception("job scheduler: failed to evaluate/run job %s", job.id)
    return spawned


async def run_job_scheduler_periodically(
    *,
    app: FastAPI,
    job_store: JobStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    agent_cache: AgentCache | None,
    permission_store: PermissionStore | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
    default_run_agent_id: str | None,
    interval_seconds: float = 60.0,
) -> None:
    """Poll for due scheduled jobs every ``interval_seconds`` until cancelled.

    Mirrors :func:`publish_server_metrics_periodically`: an
    ``asyncio.create_task`` started in ``_lifespan`` and cancelled on shutdown.
    A tick-level failure is logged and the loop continues rather than dying.

    :param app: The FastAPI app (for the synthetic request's ``app.state``).
    :param interval_seconds: Delay between polls in seconds (default 60s, which
        sets the one-minute scheduling granularity).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _run_scheduler_tick(
                app=app,
                job_store=job_store,
                conversation_store=conversation_store,
                agent_store=agent_store,
                runner_router=runner_router,
                agent_cache=agent_cache,
                permission_store=permission_store,
                file_store=file_store,
                artifact_store=artifact_store,
                liveness_lookup=liveness_lookup,
                default_run_agent_id=default_run_agent_id,
            )
        except Exception:
            # Keep the scheduler alive across ticks; retry on the next poll.
            _logger.exception("job scheduler: tick failed")
