"""Bounded, in-memory diagnostics for native Codex startup failures."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from omnigent.process_logging import redact_log_text, startup_stderr_capture_enabled

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer

_STDERR_TAIL_BYTES = 64 * 1024
_TERMINAL_ESCAPE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|\x1b[P^_].*?(?:\x1b\\|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]",
    re.DOTALL,
)


def _without_terminal_controls(text: str) -> str:
    text = _TERMINAL_ESCAPE.sub("", text).translate(str.maketrans("\t\v\f", "   "))
    return "".join(
        char for char in text if char in "\n\r" or unicodedata.category(char) not in {"Cc", "Cf"}
    ).rstrip()


def _stderr_snapshot(entries: list[str] | None) -> dict[str, object]:
    lines = [redact_log_text(_without_terminal_controls(line)) for line in entries or ()]
    retained: list[str] = []
    remaining = _STDERR_TAIL_BYTES
    for line in reversed(lines):
        encoded = line.encode("utf-8")
        required = len(encoded) + bool(retained)
        if required > remaining:
            # Keep complete entries unless even the newest entry exceeds the budget.
            if not retained:
                retained.append(encoded[-remaining:].decode("utf-8", errors="ignore"))
            break
        retained.append(line)
        remaining -= required

    tail = "\n".join(reversed(retained))
    omitted_bytes = len("\n".join(lines).encode("utf-8")) - len(tail.encode("utf-8"))
    return {
        "stderr_tail_available": entries is not None,
        "stderr_tail": tail,
        "stderr_tail_truncated": omitted_bytes > 0,
        "stderr_lines_omitted": len(lines) - len(retained),
        "stderr_bytes_omitted": omitted_bytes,
    }


def collect_codex_startup_diagnostics(
    app_server: CodexNativeAppServer | None,
) -> dict[str, object]:
    """Snapshot one launch without probing or changing its process or reader.

    Only completed stderr entries already captured in memory are considered;
    an empty buffer says nothing about pending unterminated stderr bytes.
    Text capture requires explicit opt-in. Known credential patterns are
    redacted before a 64 KiB limit, retaining complete entries where possible.
    """
    capture_enabled = startup_stderr_capture_enabled()
    snapshot: dict[str, object] = {
        "app_server_state": "unavailable" if app_server is None else "not_started",
        "stderr_reader_state": "unavailable" if app_server is None else "not_started",
        "stderr_capture_enabled": capture_enabled,
    }
    if capture_enabled:
        snapshot.update(_stderr_snapshot(None if app_server is None else app_server.recent_stderr))
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
