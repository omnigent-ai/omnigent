"""Server task context for automation delivery telemetry."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

from omnigent.debug_logging import current_session_id_scope, log_attributes_scope
from omnigent.entities.scheduled_task import ScheduledRunLogContext

_current_run: ContextVar[ScheduledRunLogContext | None] = ContextVar(
    "scheduled_run_log_context", default=None
)


def current_scheduled_run() -> ScheduledRunLogContext | None:
    """Return the occurrence responsible for the current request."""
    return _current_run.get()


@contextlib.contextmanager
def scheduled_run_scope(
    run: ScheduledRunLogContext | None, *, session_id: str | None = None
) -> Iterator[None]:
    """Restore occurrence metadata for this request and tasks it creates."""
    token = _current_run.set(run)
    try:
        with (
            log_attributes_scope(
                **(
                    run.model_dump()
                    if run is not None
                    else dict.fromkeys(ScheduledRunLogContext.model_fields)
                )
            ),
            current_session_id_scope(session_id),
        ):
            yield
    finally:
        _current_run.reset(token)
