"""Correlation and delivery events for persistent scheduled runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator
from contextvars import ContextVar

from omnigent.debug_logging import current_session_id_scope, debug_event, log_attributes_scope
from omnigent.entities.scheduled_task import (
    ScheduledRunLogContext,
    ScheduledTask,
    ScheduledTaskRun,
)
from omnigent.scheduled_context import (
    current_scheduled_run as current_scheduled_run,
)
from omnigent.scheduled_context import (
    scheduled_run_scope as scheduled_run_scope,
)

_logger = logging.getLogger(__name__)
_current_stage: ContextVar[tuple[ScheduledRunLogContext, str] | None] = ContextVar(
    "scheduled_fire_stage", default=None
)


def run_log_context(task: ScheduledTask, run: ScheduledTaskRun) -> ScheduledRunLogContext:
    """Build the occurrence identity shared by scheduling and direct execution."""
    return ScheduledRunLogContext(
        task_id=task.id,
        run_id=run.id,
        task_workspace_id=run.workspace_id,
        scheduled_at=run.scheduled_at,
        trigger_key=run.trigger_key,
        target=task.execution_target,
        session_mode=task.session_mode,
    )


def current_fire_stage() -> str:
    """Return the most recent delivery boundary reached in this task."""
    progress = _current_stage.get()
    return (
        progress[1]
        if progress is not None and progress[0] == current_scheduled_run()
        else "input_validation"
    )


def start_fire_stage(stage: str, *, session_id: str | None = None) -> None:
    """Record progress before work starts, including work that never returns."""
    run = current_scheduled_run()
    if run is None or _current_stage.get() == (run, stage):
        return
    _current_stage.set((run, stage))
    log_delivery("scheduled_run_stage_started", session_id=session_id, fire_stage=stage)


def log_delivery(event: str, *, session_id: str | None, **attributes: object) -> None:
    """Emit without letting telemetry failures change prompt delivery."""
    run = current_scheduled_run()
    if run is None:
        return
    with contextlib.suppress(Exception):
        _logger.info(
            "%s for scheduled run %s",
            event,
            run.run_id,
            extra=debug_event(
                event,
                session_id=session_id,
                turn_id=None,
                user_id=None,
                **run.model_dump(),
                **attributes,
            ),
        )


def log_session_created(session_id: str) -> None:
    """Count newly created sessions before grants, host launch, or prompt dispatch."""
    run = current_scheduled_run()
    if run is not None:
        log_delivery(
            "scheduled_run_session_created",
            session_id=session_id,
            session_delay_seconds=round(max(0.0, time.time() - run.scheduled_at), 3),
        )


def log_prompt_sent(session_id: str, *, dispatch_id: str, input_id: str | None = None) -> None:
    """Record runner acceptance, independently of sandbox binding and turn completion."""
    run = current_scheduled_run()
    if run is not None:
        log_delivery(
            "scheduled_run_prompt_sent",
            session_id=session_id,
            input_id=input_id,
            dispatch_id=dispatch_id,
            prompt_delay_seconds=round(max(0.0, time.time() - run.scheduled_at), 3),
        )


@contextlib.contextmanager
def fire_stage(stage: str, *, session_id: str | None = None) -> Iterator[None]:
    """Locate failures without changing the existing run-state/error-code contract."""
    start_fire_stage(stage, session_id=session_id)
    with current_session_id_scope(session_id), log_attributes_scope(fire_stage=stage):
        try:
            yield
        except asyncio.CancelledError:
            log_delivery(
                "scheduled_run_stage_interrupted",
                session_id=session_id,
                failure_stage=stage,
                outcome="uncertain",
            )
            raise
        except Exception as exc:
            log_delivery(
                "scheduled_run_stage_failed",
                session_id=session_id,
                failure_stage=stage,
                exception_type=type(exc).__name__,
            )
            raise


def startup_failure_stage(error_code: str | None) -> str | None:
    """Classify known startup results; retain ambiguity when no precise stage survived."""
    return {
        "host_offline": "host_validation",
        "host_not_found": "host_validation",
        "host_not_owned": "host_validation",
        "host_authority_revoked": "host_validation",
        "host_registry_unavailable": "host_validation",
        "no_online_host": "host_validation",
        "existing_sandbox_not_allowed": "host_validation",
        "default_workspace_unresolved": "input_validation",
        "managed_sandbox_unavailable": "sandbox_create",
        "continued_session_budget_failed": "budget_validation",
        "continued_session_budget_conflict": "budget_validation",
        "reauthorization_required": "owner_identity",
        "owner_identity_unavailable": "owner_identity",
        "owner_deleted": "owner_identity",
        "owner_grant_failed": "owner_grant",
        "session_create_failed": "session_create",
        "session_record_failed": "session_record",
        "sandbox_create_failed": "sandbox_create",
        "sandbox_registration_timeout": "sandbox_registration",
        "runner_start_failed": "runner_start",
        "launch_failed": "launch_or_dispatch",
        "startup_expired": "recovery_unknown",
    }.get(error_code or "")


def log_task_created(
    task: ScheduledTask, *, owner: str, source: str | None, template: str | None
) -> None:
    """Count persisted creations across UI, API, and tools without recording user content."""
    source = source if source in {"ui", "agent"} else "api"
    # Keep accepted IDs in sync with AUTOMATION_TEMPLATE_IDS in web/src/lib/scheduledTasksApi.ts.
    template = (
        template
        if template
        in {
            "custom",
            "jira",
            "slack",
            "github",
            "linear",
            "pagerduty",
            "follow-up-monitor",
            "pr-sweep",
            "news-digest",
        }
        else "unknown"
    )
    with contextlib.suppress(Exception):
        _logger.info(
            "Created scheduled task %s",
            task.id,
            extra=debug_event(
                "scheduled_task_created",
                user_id=owner,
                task_id=task.id,
                task_workspace_id=task.workspace_id,
                creation_source=source,
                template_id=template,
                target=task.execution_target,
                session_mode=task.session_mode,
            ),
        )
