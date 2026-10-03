"""Stable failure codes for local-session import, shared by host, server, and clients.

Every way an import can fail maps to one of these strings so the UI and CLI can
show a specific, actionable message (and decide whether "retry" can help)
instead of a generic "stopped unexpectedly". They ride on the wire next to the
existing fields — ``code``/``retryable`` on stream ``failed``/``error`` events,
``import_code``/``retryable`` inside HTTP error bodies (whose ``code`` stays the
global :class:`~omnigent.errors.ErrorCode` for older clients) — so a client that
predates them keeps working off ``reason``/``message`` alone.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnigent.errors import ErrorCode, OmnigentError


class ImportErrorCode:
    """Machine-readable import failure codes (stable wire strings)."""

    SESSION_TOO_LARGE = "session_too_large"
    ALREADY_IMPORTED = "already_imported"
    HOST_PYTHON_MISSING_SQLITE = "host_python_missing_sqlite"
    HOST_OFFLINE = "host_offline"
    HOST_UNREACHABLE = "host_unreachable"
    HOST_DISCONNECTED = "host_disconnected"
    HOST_UNRESPONSIVE = "host_unresponsive"
    SESSION_UNREADABLE = "session_unreadable"
    SESSION_SAVE_TIMEOUT = "session_save_timeout"
    ENCRYPTION_UNAVAILABLE = "encryption_unavailable"
    TIME_LIMIT_REACHED = "time_limit_reached"
    # Client-side only: the response stream broke before its terminal event.
    STREAM_INTERRUPTED = "stream_interrupted"
    INVALID_REQUEST = "invalid_request"
    INTERNAL = "internal"


# Whether re-running the same import can succeed without the user changing
# anything first. Drives the UI's "Retry failed" affordance.
_RETRYABLE: Mapping[str, bool] = {
    ImportErrorCode.SESSION_TOO_LARGE: False,
    ImportErrorCode.ALREADY_IMPORTED: False,
    ImportErrorCode.HOST_PYTHON_MISSING_SQLITE: False,
    ImportErrorCode.HOST_OFFLINE: True,
    ImportErrorCode.HOST_UNREACHABLE: True,
    ImportErrorCode.HOST_DISCONNECTED: True,
    ImportErrorCode.HOST_UNRESPONSIVE: True,
    ImportErrorCode.SESSION_UNREADABLE: False,
    ImportErrorCode.SESSION_SAVE_TIMEOUT: True,
    # Key-service denials are usually transient.
    ImportErrorCode.ENCRYPTION_UNAVAILABLE: True,
    ImportErrorCode.TIME_LIMIT_REACHED: True,
    ImportErrorCode.STREAM_INTERRUPTED: True,
    ImportErrorCode.INVALID_REQUEST: False,
    ImportErrorCode.INTERNAL: True,
}


def import_code_is_retryable(code: str) -> bool:
    """Whether retrying an import that failed with *code* can succeed as-is.

    Unknown codes (a newer peer's) count as retryable: offering a retry that
    fails again is cheaper than hiding one that would have worked.
    """
    return _RETRYABLE.get(code, True)


MISSING_SQLITE_MESSAGE = (
    "Your machine's Python was built without SQLite (`_sqlite3` is missing), so "
    "`omnigent host` can't read local sessions. Fix: update Omnigent on that machine "
    "(newer hosts no longer need SQLite to import), or reinstall Python with SQLite "
    "support and restart `omnigent host`."
)

# ``fix_commands`` entries: ``label`` says when it applies, ``command`` is
# exactly what to paste into a shell (the UI's copy button copies only that; the
# CLI prints "label: command").
MISSING_SQLITE_FIX_COMMANDS: tuple[Mapping[str, str], ...] = (
    {
        "label": "macOS (or reinstall Python from python.org)",
        "command": "brew install sqlite && pyenv install --force 3.12",
    },
    {
        "label": "Linux",
        "command": "sudo apt-get install libsqlite3-dev && pyenv install --force 3.12",
    },
)


def mentions_missing_sqlite(text: object) -> bool:
    """Whether a host-reported error says Python's SQLite module is missing.

    Older hosts import ``sqlite3`` eagerly, so on a Python built without it every
    import fails with ``No module named '_sqlite3'`` (or ``'sqlite3'``) as text.
    """
    if not isinstance(text, str):
        return False
    return "No module named '_sqlite3'" in text or "No module named 'sqlite3'" in text


def missing_sqlite_error() -> LocalImportError:
    """The whole-import failure for a host whose Python lacks SQLite."""
    return LocalImportError(
        MISSING_SQLITE_MESSAGE,
        import_code=ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
        # 409 like host_offline: the host's state, not the request, is at fault.
        code=ErrorCode.CONFLICT,
        details={"fix_commands": [dict(fix) for fix in MISSING_SQLITE_FIX_COMMANDS]},
    )


class LocalImportError(OmnigentError):
    """An import failure with a stable import code and a user-facing message.

    ``code`` stays a global :class:`~omnigent.errors.ErrorCode` (it picks the
    HTTP status and fault attribution); ``import_code`` is the finer-grained
    import vocabulary clients branch on. ``http_status`` overrides the code's
    default for classifications that have no global equivalent (413 for an
    oversized session, 503 for a storage timeout). ``details`` carries extra
    machine-readable fields (e.g. ``host_name``) and is merged into the error
    body next to ``import_code``/``retryable``.
    """

    def __init__(
        self,
        message: str,
        *,
        import_code: str,
        code: str = ErrorCode.CONFLICT,
        http_status: int | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details={
                "import_code": import_code,
                "retryable": import_code_is_retryable(import_code),
                **(details or {}),
            },
        )
        self.import_code = import_code
        self._http_status_override = http_status

    @property
    def http_status(self) -> int:
        """The override when set, else the global code's status."""
        if self._http_status_override is not None:
            return self._http_status_override
        return super().http_status

    @property
    def retryable(self) -> bool:
        """Whether retrying this import as-is can succeed."""
        return import_code_is_retryable(self.import_code)


__all__ = [
    "MISSING_SQLITE_FIX_COMMANDS",
    "MISSING_SQLITE_MESSAGE",
    "ImportErrorCode",
    "LocalImportError",
    "import_code_is_retryable",
    "mentions_missing_sqlite",
    "missing_sqlite_error",
]
