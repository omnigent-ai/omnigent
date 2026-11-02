"""Request-scoped automation correlation shared by server, host and runner."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from contextvars import ContextVar

import httpx
from pydantic import ValidationError

from omnigent.debug_logging import current_session_id_scope, log_attributes_scope
from omnigent.entities.scheduled_task import ScheduledRunLogContext

SCHEDULED_RUN_HEADER = "x-omnigent-scheduled-run"
_current_run: ContextVar[ScheduledRunLogContext | None] = ContextVar(
    "scheduled_run_log_context", default=None
)


def current_scheduled_run() -> ScheduledRunLogContext | None:
    """Return the occurrence responsible for the current request."""
    return _current_run.get()


def parse_scheduled_run(value: object) -> ScheduledRunLogContext | None:
    """Ignore missing or malformed diagnostic metadata without affecting execution."""
    try:
        if isinstance(value, (str, bytes)):
            return ScheduledRunLogContext.model_validate_json(value)
        return ScheduledRunLogContext.model_validate(value)
    except ValidationError:
        return None


async def add_scheduled_run_header(request: httpx.Request) -> None:
    """Stamp each runner request independently, including on shared HTTP clients."""
    run = current_scheduled_run()
    request.headers.pop(SCHEDULED_RUN_HEADER, None)
    if run is not None:
        request.headers[SCHEDULED_RUN_HEADER] = json.dumps(run.model_dump(), ensure_ascii=True)


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
