"""Bounded, in-memory diagnostics for native Codex startup failures."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.harnesses.diagnostics import bounded_diagnostic_tail
from omnigent.process_logging import harness_stderr_capture_enabled

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer


def collect_codex_startup_diagnostics(
    app_server: CodexNativeAppServer | None,
) -> dict[str, object]:
    """Snapshot one launch without probing or changing its process or reader.

    Only completed stderr entries already captured in memory are considered;
    an empty buffer says nothing about pending unterminated stderr bytes.
    Text capture requires explicit opt-in. Known credential patterns are
    redacted before a 64 KiB limit, retaining complete entries where possible.
    """
    capture_enabled = harness_stderr_capture_enabled()
    snapshot: dict[str, object] = {
        "app_server_state": "unavailable" if app_server is None else "not_started",
        "stderr_reader_state": "unavailable" if app_server is None else "not_started",
        "stderr_capture_enabled": capture_enabled,
    }
    if capture_enabled:
        entries = None if app_server is None else app_server.recent_stderr
        tail = bounded_diagnostic_tail(entries or [])
        snapshot.update(
            stderr_tail_available=entries is not None,
            stderr_tail=tail["tail"],
            stderr_tail_truncated=tail["truncated"],
            stderr_lines_omitted=tail["lines_omitted"],
            stderr_bytes_omitted=tail["bytes_omitted"],
        )
    if app_server is None:
        return snapshot

    process = app_server.proc
    if process is not None:
        snapshot["app_server_state"] = "running" if process.returncode is None else "exited"
        snapshot["app_server_pid"] = process.pid
        if process.returncode is not None:
            snapshot["app_server_returncode"] = process.returncode
    if app_server.codex_cli_version is not None:
        snapshot["codex_version"] = ".".join(map(str, app_server.codex_cli_version))[:64]

    reader = app_server.stderr_task
    if reader is not None:
        if reader.cancelled():
            snapshot["stderr_reader_state"] = "cancelled"
        elif not reader.done():
            snapshot["stderr_reader_state"] = "running"
        else:
            error = reader.exception()
            snapshot["stderr_reader_state"] = "completed" if error is None else "failed"
            if error is not None:
                snapshot["stderr_reader_error_type"] = type(error).__name__[:128]
                cause = error.__cause__ or error.__context__
                if cause is not None:
                    snapshot["stderr_reader_cause_type"] = type(cause).__name__[:128]
    return snapshot
