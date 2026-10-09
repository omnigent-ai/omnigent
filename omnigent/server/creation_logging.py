"""Debug-log contract for server-received session creation requests."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from typing import Literal

from omnigent.debug_logging import (
    add_audit_attrs,
    current_request_audit_attrs,
    debug_event,
    set_current_runner_id,
    set_current_session_id,
)
from omnigent.server.session_metadata_logging import harness_attributes

_logger = logging.getLogger("omnigent.server.creation")

CreationStageAttribute = Literal[
    "create_persistence_ms",
    "create_identity_ms",
    "create_acl_ms",
]


@contextlib.contextmanager
def creation_stage(attribute: CreationStageAttribute) -> Iterator[None]:
    """Accumulate one create-stage duration on the request audit row.

    A stage may occur more than once in one request (for example, the
    conversation insert and a post-initial-items refresh are both persistence
    work), so repeated measurements add to the existing value.

    :param attribute: Bounded audit attribute that receives elapsed
        milliseconds.
    """
    started_at = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        existing = current_request_audit_attrs().get(attribute)
        total_ms = elapsed_ms + (float(existing) if existing is not None else 0.0)
        add_audit_attrs(**{attribute: total_ms})


def creation_metadata(*, parent_session_id: str | None, host_type: str) -> None:
    """Classify validated creates before persistence or launch can fail."""
    add_audit_attrs(
        creation_kind="child" if parent_session_id else "top_level",
        host_type=host_type,
    )


def session_created(
    session_id: str,
    runner_id: str | None = None,
    parent_session_id: str | None = None,
    *,
    agent_id: str | None = None,
    harness: str | None = None,
) -> None:
    """Publish the request-to-session link immediately after persistence.

    ``parent_session_id`` is the spawning parent for a sub-agent child (``None``
    for a top-level session); logging it on the creation row is what lets a
    debug-log query join a child back to its parent session.
    """
    set_current_session_id(session_id)
    set_current_runner_id(runner_id)
    add_audit_attrs(session_id=session_id, runner_id=runner_id)
    _logger.info(
        "Session persisted",
        extra=debug_event(
            "session_created",
            session_id=session_id,
            runner_id=runner_id,
            agent_id=agent_id,
            session_kind="sub_agent" if parent_session_id else "default",
            parent_session_id=parent_session_id,
            creation_kind=current_request_audit_attrs().get("creation_kind", "unknown"),
            **harness_attributes(harness, source="create_selection"),
        ),
    )
