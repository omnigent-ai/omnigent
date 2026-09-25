"""Shared error factories for route handlers.

Centralizes the construction of common ``OmnigentError`` instances so
that the wire message and error code have a single source of truth
across all route modules.  Import the factory and raise it directly::

    from omnigent.server.routes._errors import session_not_found

    raise session_not_found()
    # or, to preserve exception chaining:
    raise session_not_found() from exc
"""

from __future__ import annotations

from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.schemas import ErrorResponse

_SESSION_NOT_FOUND: str = "Session not found"


def session_not_found() -> OmnigentError:
    """Build the canonical ``NOT_FOUND`` error for a vanished session.

    Every "the conversation row is gone" branch across the route modules
    raises the same message and :class:`ErrorCode.NOT_FOUND` code;
    centralizing the construction keeps the wire response identical
    across handlers.  Raise it directly with ``raise session_not_found()``,
    or ``raise session_not_found() from exc`` to preserve cause chaining.

    :returns: A fresh :class:`OmnigentError` with message
        ``"Session not found"`` and code :attr:`ErrorCode.NOT_FOUND`.
    """
    return OmnigentError(_SESSION_NOT_FOUND, code=ErrorCode.NOT_FOUND)


STALE_CURSOR_RESPONSE: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorResponse,
        "description": (
            "The `after`/`before` cursor names an item that no longer exists "
            "(it was deleted, or archived out of the filtered set) so the "
            "keyset bound cannot be resolved. The `error.code` is "
            "`stale_cursor`. Enumeration cannot continue from this cursor: "
            "restart from the first page, without a cursor."
        ),
    }
}
"""Documented ``stale_cursor`` 400 for the cursor-paginated list routes.

Shared by every route that resolves a caller-supplied cursor, so the
contract clients code against is described once.
"""
