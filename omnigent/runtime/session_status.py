"""Shared relay cache and status projection for child sessions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from omnigent.db.enum_codecs import SESSION_LIVE_STATUS, SessionLiveStatus
from omnigent.db.workspace_cache import WorkspaceScopedCache

LAST_TASK_ERROR_CODE_LABEL_KEY = "omnigent.last_task_error_code"
LAST_TASK_ERROR_MESSAGE_LABEL_KEY = "omnigent.last_task_error_message"

session_status_cache: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


def has_durable_task_error(labels: Mapping[str, str]) -> bool:
    """Whether a failure is recorded as both a durable code and message label."""
    return bool(
        labels.get(LAST_TASK_ERROR_CODE_LABEL_KEY)
        and labels.get(LAST_TASK_ERROR_MESSAGE_LABEL_KEY)
    )


def resolve_child_session_status(
    session_id: str,
    durable_status: str | None,
    labels: Mapping[str, str],
    *,
    cached_status: str | None = None,
) -> SessionLiveStatus | None:
    """Prefer a durable failure, then a known relay-cache value, then the stored row."""
    if has_durable_task_error(labels):
        return "failed"
    if cached_status is None:
        cached_status = session_status_cache.get(session_id)
    # The runner status probe caches the raw payload, so an out-of-vocabulary
    # cache entry must not hide a valid persisted status.
    for status in (cached_status, durable_status):
        if status in SESSION_LIVE_STATUS:
            return cast(SessionLiveStatus, status)
    return None
