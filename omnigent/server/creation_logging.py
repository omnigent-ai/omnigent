"""Debug-log contract for server-received session creation requests."""

from __future__ import annotations

import logging

from omnigent.debug_logging import (
    add_audit_attrs,
    current_request_audit_attrs,
    debug_event,
    set_current_runner_id,
    set_current_session_id,
)

_logger = logging.getLogger("omnigent.server.creation")


def creation_metadata(*, parent_session_id: str | None, host_type: str) -> None:
    """Classify validated creates before persistence or launch can fail."""
    add_audit_attrs(
        creation_kind="child" if parent_session_id else "top_level",
        host_type=host_type,
    )


def session_created(session_id: str, runner_id: str | None = None) -> None:
    """Publish the request-to-session link immediately after persistence."""
    set_current_session_id(session_id)
    set_current_runner_id(runner_id)
    add_audit_attrs(session_id=session_id, runner_id=runner_id)
    _logger.info(
        "Session persisted",
        extra=debug_event(
            "session_created",
            session_id=session_id,
            runner_id=runner_id,
            creation_kind=current_request_audit_attrs().get("creation_kind", "unknown"),
        ),
    )
