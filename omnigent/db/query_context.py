"""Context-local names for database query observability."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_query_name: ContextVar[str | None] = ContextVar("omnigent_query_name", default=None)


def current_query_name() -> str | None:
    """Return the semantic name of the query currently being executed, if any.

    Database instrumentation can read this value without coupling application
    stores to a particular query-comment format, tracer, or logger.
    """
    return _current_query_name.get()


@contextmanager
def query_name_scope(query_name: str) -> Iterator[None]:
    """Bind ``query_name`` while one logical database query executes.

    Nested scopes restore their parent's name, including when query execution
    raises. The scope itself does not modify SQL or emit telemetry.
    """
    if not query_name.strip():
        raise ValueError("query_name must not be empty")
    token = _current_query_name.set(query_name)
    try:
        yield
    finally:
        _current_query_name.reset(token)
