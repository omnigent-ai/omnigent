"""Shared persisted schedule calculations and occurrence identities."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.entities import ScheduledTask
from omnigent.server.scheduled.rrule import (
    RRuleSearchError,
    RRuleValidationError,
    get_next_fire_time,
)

_logger = logging.getLogger(__name__)


def next_scheduled_time(task: ScheduledTask, after: int) -> int | None:
    """Next occurrence; search-budget errors propagate so callers preserve the cursor."""
    if task.state != "active":
        return None
    try:
        timezone = ZoneInfo(task.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        _logger.warning(
            "Unknown timezone %r for task %s; defaulting to UTC", task.timezone, task.id
        )
        timezone = ZoneInfo("UTC")
    try:
        occurrence = get_next_fire_time(
            task.rrule,
            datetime.fromtimestamp(after, UTC),
            timezone,
            anchor=datetime.fromtimestamp(task.created_at, UTC),
        )
    except RRuleSearchError:
        raise
    except RRuleValidationError:
        _logger.warning("Invalid schedule for task %s", task.id, exc_info=True)
        return None
    return int(occurrence.timestamp()) if occurrence is not None else None


def scheduled_trigger_key(scheduled_at: int) -> str:
    """Canonical occurrence key, distinct from manual requests and event deliveries."""
    return f"schedule:{scheduled_at}"


def manual_trigger_key() -> str:
    """A fresh key for one Run now request; each request is its own occurrence."""
    return f"manual:{uuid.uuid4().hex}"


def sandbox_host_id(task_id: str, workspace_id: int, trigger_key: str) -> str:
    """The fresh sandbox identity for one occurrence, from fields no task edit changes."""
    # UUIDv5 lets every replica derive the same sandbox host ID for this task occurrence.
    return uuid.uuid5(uuid.UUID(task_id), f"{workspace_id}:{trigger_key}").hex


def continued_sandbox_host_id(task_id: str, workspace_id: int) -> str:
    """The one sandbox identity every run of a task that continues a session uses.

    Derived only from fields no task edit changes, so every run routes to the
    replica holding that sandbox's connection, and a session reset keeps it.
    """
    return uuid.uuid5(uuid.UUID(task_id), f"{workspace_id}:continue").hex


def occurrence_host_id(task: ScheduledTask, trigger_key: str) -> str | None:
    """Pin a host, or derive the occurrence's sandbox identity from tenant and task."""
    if task.execution_target != "managed_sandbox":
        return task.host_id
    if task.session_mode == "continue":
        return continued_sandbox_host_id(task.id, task.workspace_id)
    return sandbox_host_id(task.id, task.workspace_id, trigger_key)


def host_ownership_key(workspace_id: int, host_id: str | None) -> str:
    """Key a replica's ownership check by host, or by workspace for a task with no host."""
    return host_id if host_id is not None else str(workspace_id)
