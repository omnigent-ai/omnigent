"""Automation creation telemetry with bounded attribution."""

from __future__ import annotations

import contextlib
import logging

from omnigent.debug_logging import debug_event
from omnigent.entities.scheduled_task import ScheduledTask

_logger = logging.getLogger(__name__)


def log_task_created(
    task: ScheduledTask, *, owner: str, source: str | None, template: str | None
) -> None:
    """Count persisted creations across UI, API, and tools without recording user content."""
    source = source if source in {"ui", "agent"} else "api"
    # Keep accepted IDs in sync with AUTOMATION_TEMPLATE_IDS in web/src/lib/scheduledTasksApi.ts.
    template = (
        template
        if template
        in {"custom", "jira", "slack", "github", "follow-up-monitor", "pr-sweep", "news-digest"}
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
