"""Best-effort Codex stderr export, isolated from subprocess pipe draining."""

from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import cast

from omnigent.debug_logging import debug_event
from omnigent.harnesses.codex_native.bridge import read_bridge_state
from omnigent.harnesses.diagnostics import DIAGNOSTIC_TAIL_BYTES, bounded_diagnostic_tail

MAX_STDERR_RECORD_BYTES = 1024 * 1024  # Includes the newline when present.
_QUEUE_BYTES = MAX_STDERR_RECORD_BYTES
_QUEUE_RECORDS = 256
_EXPORT_INTERVAL_S = 0.25
_CLOSE_TIMEOUT_S = 1.0
_logger = logging.getLogger(__name__)


def report_capture_start_failure(*, session_id: str | None, pid: int, error_type: str) -> None:
    """Best-effort, payload-free warning without logging I/O on the pipe reader."""

    def emit() -> None:
        with contextlib.suppress(Exception):
            _logger.warning(
                "Codex stderr diagnostic capture unavailable; session=%s pid=%d error_type=%s",
                session_id,
                pid,
                error_type,
                extra=debug_event(
                    "harness_diagnostic_capture_failed",
                    session_id=session_id,
                    harness="codex-native",
                    source_kind="codex_app_server_stderr",
                    app_server_pid=pid,
                    error_type=error_type,
                ),
            )

    # If thread resources are exhausted, the startup snapshot retains the error type.
    with contextlib.suppress(Exception):
        threading.Thread(target=emit, name="codex-stderr-capture-failure", daemon=True).start()


class CodexStderrDiagnostics:
    """Buffer complete records without making the reader wait for logging I/O."""

    def __init__(self, *, session_id: str | None, bridge_dir: Path, pid: int) -> None:
        self._session_id = session_id
        self._bridge_dir = bridge_dir
        self._pid = pid
        self._launch_id = uuid.uuid4().hex
        self._records: deque[bytes] = deque()
        self._queued_bytes = 0
        self._omitted_lines = 0
        self._omitted_bytes = 0
        self._offset = 0
        self._lock = threading.Lock()
        self._finished = threading.Event()
        # App-server teardown must not join a stuck logging handler indefinitely.
        self._thread = threading.Thread(
            target=self._run, name=f"codex-stderr-diagnostics-{pid}", daemon=True
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
            if bytes_omitted or size > _QUEUE_BYTES:
                self._omitted_lines += 1
                self._omitted_bytes += size + bytes_omitted
                return
            while self._records and (
                self._queued_bytes + size > _QUEUE_BYTES or len(self._records) >= _QUEUE_RECORDS
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
        session_id = self._session_id
        with contextlib.suppress(Exception):
            state = read_bridge_state(self._bridge_dir)
            if state is not None:
                session_id = state.session_id
        snapshot = bounded_diagnostic_tail(
            [record.decode("utf-8", errors="replace") for record in records]
        )
        text = snapshot["tail"]
        omitted_lines += cast("int", snapshot["lines_omitted"])
        omitted_bytes += cast("int", snapshot["bytes_omitted"])
        _logger.info(
            "Codex diagnostic output; session=%s launch=%s offset=%d "
            "lines_omitted=%d bytes_omitted=%d\n%s",
            session_id,
            self._launch_id,
            offset,
            omitted_lines,
            omitted_bytes,
            text,
            extra=debug_event(
                "harness_diagnostic_output",
                session_id=session_id,
                harness="codex-native",
                source_kind="codex_app_server_stderr",
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
