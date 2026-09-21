"""Bounded, in-memory diagnostics for native Codex startup failures."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from omnigent.process_logging import redact_log_text

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer

_STDERR_TAIL_CHARS = 4096
_STDERR_ENTRIES = 20
_STDERR_ENTRY_CHARS = 16_384
_REDACTED = "[REDACTED]"
_TERMINAL_ESCAPE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|\x1b[P^_].*?(?:\x1b\\|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]",
    re.DOTALL,
)
_PAYLOAD = re.compile(
    r"\b(?:prompts?|inputs?|messages?|instructions?|content|body|payload|headers?)\b"
    r"|\b(?:request|response)[\"']?\s*(?:[:={\[]|dump\b)",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"\b(?:authorization|proxy-authorization|cookie|set-cookie|content-type|user-agent|"
    r"x-[\w-]+|[\w.-]*(?:token|api[_-]?key|password|secret|credential))[\"']?\s*[:=]",
    re.IGNORECASE,
)
_NEW_RECORD = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\S+\s+(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b"
)
_DIAGNOSTIC = re.compile(
    r"\b(?:error|warn|warning|failed|failure|invalid|denied|timeout|timed out|refused|unavailable|"
    r"unauthorized|forbidden|panic)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")
_URL_USERINFO = re.compile(r"(?<=://)\S+(?=@)")


def _without_terminal_controls(text: str) -> str:
    text = _TERMINAL_ESCAPE.sub("", text).translate(str.maketrans("\t\v\f", "   "))
    return "".join(
        char for char in text if char in "\n\r" or unicodedata.category(char) not in {"Cc", "Cf"}
    ).rstrip()


def _redact_url(match: re.Match[str]) -> str:
    url = _URL_USERINFO.sub(_REDACTED, match.group())
    return re.sub(r"[?#].*", "?" + _REDACTED, url)


def _stderr_snapshot(entries: list[str] | None) -> dict[str, object]:
    retained: list[str] = []
    omitted = max(0, len(entries) - _STDERR_ENTRIES) if entries is not None else 0
    # A bounded tail may begin in the middle of a dump. Resume only at a new record.
    in_payload = entries is not None and len(entries) >= _STDERR_ENTRIES
    for original in entries[-_STDERR_ENTRIES:] if entries is not None else ():
        if len(original) > _STDERR_ENTRY_CHARS:
            omitted += 1
            in_payload = True
            continue
        line = _without_terminal_controls(original)
        if _NEW_RECORD.match(line):
            in_payload = False
        if (
            "\n" in line
            or "\r" in line
            or _PAYLOAD.search(line)
            or _HEADER.search(line)
            or line.lstrip().startswith(("{", "[", "}", "]", '"', "'"))
        ):
            in_payload = True
        if in_payload or not _DIAGNOSTIC.search(line):
            omitted += 1
            continue
        # Sanitize the complete retained line before clipping any of its characters.
        safe = redact_log_text(line)
        safe = _URL.sub(_redact_url, safe)
        retained.append(safe)

    tail = "\n".join(retained)
    clipped = max(0, len(tail) - _STDERR_TAIL_CHARS)
    omitted += tail[:clipped].count("\n")
    return {
        "stderr_tail_available": entries is not None,
        "stderr_tail": tail[clipped:],
        "stderr_tail_truncated": bool(omitted or clipped),
        "stderr_lines_omitted": omitted,
    }


def collect_codex_startup_diagnostics(
    app_server: CodexNativeAppServer | None,
) -> dict[str, object]:
    """Snapshot one launch without probing or changing its process or reader.

    Only completed stderr entries already captured in memory are considered;
    an empty buffer says nothing about pending unterminated stderr bytes.
    Payloads and their continuations are omitted, and useful diagnostic lines
    are redacted before a final 4096-character tail limit. Omitted counts refer
    to captured entries (normally individual lines), including privacy filtering.
    """
    snapshot: dict[str, object] = {
        "app_server_state": "unavailable" if app_server is None else "not_started",
        "stderr_reader_state": "unavailable" if app_server is None else "not_started",
    }
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
