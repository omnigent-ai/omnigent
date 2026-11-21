"""Best-effort Codex stderr export, isolated from subprocess pipe draining."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from omnigent.harnesses.codex_native.bridge import read_bridge_state
from omnigent.harnesses.diagnostics import (
    MAX_STDERR_RECORD_BYTES,
    STDERR_QUEUE_BYTES,
    STDERR_QUEUE_RECORDS,
    HarnessStderrExporter,
    report_stderr_capture_start_failure,
)
from omnigent.process_logging import harness_stderr_capture_enabled

CODEX_DIAGNOSTIC_RUST_LOG = (
    "warn,codex_core::client=info,codex_core::tools::parallel=debug,"
    "codex_core::mcp=info,codex_http_client=debug,codex_client::default_client=debug,"
    "codex_mcp_client=info,codex_code_mode::timing=debug"
)
__all__ = ["MAX_STDERR_RECORD_BYTES", "CodexStderrDiagnostics", "report_capture_start_failure"]
_QUEUE_BYTES = STDERR_QUEUE_BYTES
_QUEUE_RECORDS = STDERR_QUEUE_RECORDS
_HARNESS = "codex-native"
_SOURCE_KIND = "codex_app_server_stderr"
_THREAD_PREFIX = "codex-stderr-diagnostics"
_logger = logging.getLogger(__name__)


def codex_app_server_diagnostic_env(env: Mapping[str, str]) -> dict[str, str]:
    """Enable native runtime stderr diagnostics only for opted-in app-servers.

    Keep explicit launch/host filters, including an empty value or ``off``.
    Broad core/protocol debug filters can include prompts and tool payloads;
    the default selects request metadata, runtime timing, and warnings instead.
    """
    configured = dict(env)
    if harness_stderr_capture_enabled():
        configured.setdefault("RUST_LOG", os.environ.get("RUST_LOG", CODEX_DIAGNOSTIC_RUST_LOG))
    return configured


def report_capture_start_failure(*, session_id: str | None, pid: int, error_type: str) -> None:
    """Best-effort, payload-free warning without logging I/O on the pipe reader."""
    report_stderr_capture_start_failure(
        logger=_logger,
        label="Codex",
        harness=_HARNESS,
        source_kind=_SOURCE_KIND,
        thread_prefix="codex-stderr",
        session_id=session_id,
        pid=pid,
        error_type=error_type,
    )


class CodexStderrDiagnostics(HarnessStderrExporter):
    """Codex app-server stderr export; the session id is re-read from bridge state."""

    def __init__(self, *, session_id: str | None, bridge_dir: Path, pid: int) -> None:
        def resolve_session_id() -> str | None:
            state = read_bridge_state(bridge_dir)
            return state.session_id if state is not None else session_id

        super().__init__(
            logger=_logger,
            label="Codex",
            harness=_HARNESS,
            source_kind=_SOURCE_KIND,
            thread_prefix=_THREAD_PREFIX,
            pid=pid,
            session_id=resolve_session_id,
        )
