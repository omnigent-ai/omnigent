"""Shared text formatting, size limits and stderr export for opt-in harness diagnostics."""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable
from typing import cast

from omnigent.debug_logging import debug_event
from omnigent.process_logging import redact_log_text

DIAGNOSTIC_TAIL_BYTES = 64 * 1024
MAX_STDERR_RECORD_BYTES = 1024 * 1024  # Includes the newline when present.
STDERR_QUEUE_BYTES = MAX_STDERR_RECORD_BYTES
STDERR_QUEUE_RECORDS = 256
_EXPORT_INTERVAL_S = 0.25
_CLOSE_TIMEOUT_S = 1.0
_TERMINAL_ESCAPE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|\x1b[P^_].*?(?:\x1b\\|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]",
    re.DOTALL,
)
_URL_USERINFO = re.compile(r"(?i)((?<![\w+.-])[a-z][a-z0-9+.-]*://)[^/\s?#\"'<>]*@")
_HTTP_COOKIE = re.compile(
    r"(?i)((?<![\w-])(?:set-cookie|cookie)\b[\"']?[ \t]*[:=][ \t]*)"
    # Rust HeaderValue debug leaves existing backslashes before its escaped quotes.
    r'''("[^"\r\n]*(?:(?<=\\)"[^"\r\n]*)*(?<!\\)"'''
    r"|'[^'\r\n]*(?:(?<=\\)'[^'\r\n]*)*(?<!\\)'"
    r"|[^\r\n]*)"
)


def _redact_http_cookie(match: re.Match[str]) -> str:
    """Keep the field's quoting without retaining any of its cookie value."""
    quote = match[2][:1]
    replacement = f"{quote}[REDACTED]{quote}" if quote in {"'", '"'} else "[REDACTED]"
    return match[1] + replacement


def sanitize_diagnostic_text(text: str) -> str:
    """Strip terminal controls and redact known credential patterns."""
    text = _TERMINAL_ESCAPE.sub("", text).translate(str.maketrans("\t\v\f", "   "))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        char for char in text if char == "\n" or unicodedata.category(char) not in {"Cc", "Cf"}
    ).rstrip()
    cleaned = _URL_USERINFO.sub(r"\1[REDACTED]@", cleaned)
    cleaned = _HTTP_COOKIE.sub(_redact_http_cookie, cleaned)
    return redact_log_text(cleaned, include_whitespace_credentials=True)


def bounded_diagnostic_tail(entries: list[str]) -> dict[str, object]:
    """Retain recent redacted entries within a 64 KiB UTF-8 export budget."""
    lines = [sanitize_diagnostic_text(line) for line in entries]
    retained: list[str] = []
    remaining = DIAGNOSTIC_TAIL_BYTES
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
        "tail": tail,
        "truncated": omitted_bytes > 0,
        "lines_omitted": len(lines) - len(retained),
        "bytes_omitted": omitted_bytes,
    }


def report_stderr_capture_start_failure(
    *,
    logger: logging.Logger,
    label: str,
    harness: str,
    source_kind: str,
    thread_prefix: str,
    session_id: str | None,
    pid: int,
    error_type: str,
) -> None:
    """Best-effort, payload-free warning without logging I/O on the pipe reader."""

    def emit() -> None:
        with contextlib.suppress(Exception):
            logger.warning(
                "%s stderr diagnostic capture unavailable; session=%s pid=%d error_type=%s",
                label,
                session_id,
                pid,
                error_type,
                extra=debug_event(
                    "harness_diagnostic_capture_failed",
                    session_id=session_id,
                    harness=harness,
                    source_kind=source_kind,
                    app_server_pid=pid,
                    error_type=error_type,
                ),
            )

    # If thread resources are exhausted, the startup snapshot retains the error type.
    with contextlib.suppress(Exception):
        threading.Thread(target=emit, name=f"{thread_prefix}-capture-failure", daemon=True).start()


class HarnessStderrExporter:
    """Buffer complete stderr records and export them without blocking the pipe reader.

    Records are redacted and batched into ``harness_diagnostic_output`` INFO
    logs on a daemon thread, so a slow logging handler never stalls the reader.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger,
        label: str,
        harness: str,
        source_kind: str,
        thread_prefix: str,
        pid: int,
        session_id: Callable[[], str | None],
    ) -> None:
        self._logger = logger
        self._label = label
        self._harness = harness
        self._source_kind = source_kind
        self._session_id = session_id
        self._pid = pid
        self._launch_id = uuid.uuid4().hex
        self._records: deque[bytes] = deque()
        self._queued_bytes = 0
        self._omitted_lines = 0
        self._omitted_bytes = 0
        self._offset = 0
        self._lock = threading.Lock()
        self._finished = threading.Event()
        # Process teardown must not join a stuck logging handler indefinitely.
        self._thread = threading.Thread(
            target=self._run, name=f"{thread_prefix}-{pid}", daemon=True
        )
        self._thread.start()

    def submit(self, record: bytes, *, bytes_omitted: int = 0) -> None:
        """Enqueue a whole record, shedding old records under sustained overload.

        An oversized source record is omitted entirely: exporting a clipped
        credential assignment could defeat redaction at the clipping boundary.
        """
        size = len(record)
        with self._lock:
            if self._finished.is_set():
                return
            self._offset += size + bytes_omitted
            if bytes_omitted or size > STDERR_QUEUE_BYTES:
                self._omitted_lines += 1
                self._omitted_bytes += size + bytes_omitted
                return
            while self._records and (
                self._queued_bytes + size > STDERR_QUEUE_BYTES
                or len(self._records) >= STDERR_QUEUE_RECORDS
            ):
                removed = len(self._records.popleft())
                self._queued_bytes -= removed
                self._omitted_lines += 1
                self._omitted_bytes += removed
            self._records.append(record)
            self._queued_bytes += size

    def finish(self) -> None:
        """Signal EOF/cancellation without waiting for the exporter."""
        with self._lock:
            self._finished.set()

    def close(self) -> None:
        """Allow a final batch, but bound teardown if a logging handler stalls."""
        self.finish()
        self._thread.join(timeout=_CLOSE_TIMEOUT_S)

    def _run(self) -> None:
        while True:
            self._finished.wait(_EXPORT_INTERVAL_S)
            with self._lock:
                records = self._records
                omitted_lines, omitted_bytes = self._omitted_lines, self._omitted_bytes
                offset = self._offset
                finished = self._finished.is_set()
                self._records = deque()
                self._queued_bytes = self._omitted_lines = self._omitted_bytes = 0
            if records or omitted_lines or omitted_bytes:
                with contextlib.suppress(Exception):
                    self._emit(records, omitted_lines, omitted_bytes, offset)
            if finished:
                return

    def _emit(
        self, records: deque[bytes], omitted_lines: int, omitted_bytes: int, offset: int
    ) -> None:
        session_id = None
        with contextlib.suppress(Exception):
            session_id = self._session_id()
        snapshot = bounded_diagnostic_tail(
            [record.decode("utf-8", errors="replace") for record in records]
        )
        text = snapshot["tail"]
        omitted_lines += cast("int", snapshot["lines_omitted"])
        omitted_bytes += cast("int", snapshot["bytes_omitted"])
        self._logger.info(
            "%s diagnostic output; session=%s launch=%s offset=%d "
            "lines_omitted=%d bytes_omitted=%d\n%s",
            self._label,
            session_id,
            self._launch_id,
            offset,
            omitted_lines,
            omitted_bytes,
            text,
            extra=debug_event(
                "harness_diagnostic_output",
                session_id=session_id,
                harness=self._harness,
                source_kind=self._source_kind,
                launch_id=self._launch_id,
                app_server_pid=self._pid,
                offset=offset,
                text=text,
                truncated=bool(snapshot["truncated"] or omitted_lines or omitted_bytes),
                lines_omitted=omitted_lines,
                bytes_omitted=omitted_bytes,
                tail_byte_limit=DIAGNOSTIC_TAIL_BYTES,
            ),
        )
