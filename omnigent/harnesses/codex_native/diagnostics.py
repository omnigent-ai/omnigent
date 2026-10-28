"""Bounded diagnostics for native Codex startup and RPC failures."""

from __future__ import annotations

import itertools
import os
import sys
from typing import TYPE_CHECKING

from websockets.exceptions import ConnectionClosed, InvalidStatus

from omnigent.harnesses.diagnostics import bounded_diagnostic_tail
from omnigent.process_logging import harness_stderr_capture_enabled

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import (
        CodexAppServerClient,
        CodexNativeAppServer,
    )


def collect_codex_rpc_failure_diagnostics(
    client: CodexAppServerClient, error: BaseException
) -> dict[str, object]:
    """Capture transport facts and bounded local descriptor pressure after an RPC fails."""
    snapshot: dict[str, object] = {}
    try:
        snapshot.update(client.connection_diagnostics())
        current: BaseException | None = error
        for depth in range(4):
            if current is None:
                break
            snapshot[f"rpc_error_type_{depth}"] = type(current).__name__
            if isinstance(current, OSError) and current.errno is not None:
                snapshot.setdefault("rpc_os_errno", current.errno)
            if isinstance(current, ConnectionClosed):
                if current.rcvd is not None:
                    snapshot.setdefault("rpc_close_received_code", current.rcvd.code)
                if current.sent is not None:
                    snapshot.setdefault("rpc_close_sent_code", current.sent.code)
            if isinstance(current, InvalidStatus):
                snapshot.setdefault("rpc_http_status", current.response.status_code)
            current = current.__cause__ or current.__context__
    except Exception as exc:  # noqa: BLE001 — diagnostics cannot replace the failure
        snapshot["rpc_diagnostics_error_type"] = type(exc).__name__

    if sys.platform not in {"linux", "darwin"}:
        return snapshot
    snapshot["executor_pid"] = os.getpid()
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        snapshot["executor_fd_soft_limit"] = soft
        snapshot["executor_fd_hard_limit"] = hard
        path = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
        with os.scandir(path) as entries:
            count = sum(1 for _ in itertools.islice(entries, 4097))
        snapshot["executor_fd_count"] = min(count, 4096)
        snapshot["executor_fd_count_capped"] = count > 4096
    except Exception as exc:  # noqa: BLE001 — descriptor exhaustion can prevent the probe
        snapshot["executor_fd_probe_error_type"] = type(exc).__name__
        if isinstance(exc, OSError) and exc.errno is not None:
            snapshot["executor_fd_probe_errno"] = exc.errno
    return snapshot


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

    if error_type := getattr(app_server, "stderr_capture_error_type", None):
        snapshot["stderr_capture_error_type"] = error_type

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
