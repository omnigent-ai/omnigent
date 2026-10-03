"""Shared relay cache and status projection for child sessions."""

from __future__ import annotations

from collections.abc import Mapping

from omnigent.db.enum_codecs import SESSION_LIVE_STATUS
from omnigent.db.workspace_cache import WorkspaceScopedCache

LAST_TASK_ERROR_CODE_LABEL_KEY = "omnigent.last_task_error_code"
LAST_TASK_ERROR_MESSAGE_LABEL_KEY = "omnigent.last_task_error_message"

session_status_cache: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


def resolve_child_session_status(
    session_id: str,
    durable_status: str | None,
    labels: Mapping[str, str],
    *,
    cached_status: str | None = None,
) -> str | None:
    """Prefer a durable failure, then the relay cache, then the stored row."""
    if labels.get(LAST_TASK_ERROR_CODE_LABEL_KEY) and labels.get(
        LAST_TASK_ERROR_MESSAGE_LABEL_KEY
    ):
        return "failed"
    if cached_status is None:
        cached_status = session_status_cache.get(session_id)
    status = cached_status if cached_status is not None else durable_status
    return status if status in SESSION_LIVE_STATUS else None
