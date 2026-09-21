"""Privacy and lifecycle coverage for bounded Codex startup snapshots."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from omnigent.harnesses.codex_native.diagnostics import collect_codex_startup_diagnostics

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer

_RECORD = "2026-09-21T12:00:00.000Z ERROR "


def _server(
    entries: list[str] | None = None,
    *,
    process: object = None,
    reader: asyncio.Task[None] | None = None,
) -> CodexNativeAppServer:
    return cast(
        "CodexNativeAppServer",
        SimpleNamespace(
            proc=process,
            stderr_task=reader,
            recent_stderr=entries,
            codex_cli_version=None,
        ),
    )


def test_unavailable_capture_and_empty_completed_line_buffer_are_distinct() -> None:
    unavailable = collect_codex_startup_diagnostics(None)
    not_started = collect_codex_startup_diagnostics(_server())
    empty = collect_codex_startup_diagnostics(_server([]))

    assert unavailable == {
        "app_server_state": "unavailable",
        "stderr_reader_state": "unavailable",
        "stderr_tail_available": False,
        "stderr_tail": "",
        "stderr_tail_truncated": False,
        "stderr_lines_omitted": 0,
    }
    assert not_started["app_server_state"] == "not_started"
    assert not_started["stderr_reader_state"] == "not_started"
    assert not_started["stderr_tail_available"] is False
    assert empty["stderr_tail_available"] is True
    assert empty["stderr_tail"] == ""
    assert "app_server_pid" not in not_started
    assert "app_server_returncode" not in not_started
    assert "codex_version" not in not_started


@pytest.mark.parametrize("returncode", [None, 0, 17, -9])
def test_process_state_and_known_version(returncode: int | None) -> None:
    server = _server(process=SimpleNamespace(pid=4242, returncode=returncode))
    server.codex_cli_version = (0, 154, 1)
    snapshot = collect_codex_startup_diagnostics(server)
    assert snapshot["app_server_state"] == ("running" if returncode is None else "exited")
    assert snapshot["app_server_pid"] == 4242
    assert snapshot.get("app_server_returncode") == returncode
    assert snapshot["codex_version"] == "0.154.1"


async def test_running_reader_is_not_awaited_or_cancelled() -> None:
    reader = asyncio.create_task(asyncio.sleep(3600))
    try:
        snapshot = collect_codex_startup_diagnostics(_server(reader=reader))
        assert snapshot["stderr_reader_state"] == "running"
        assert not reader.done()
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


@pytest.mark.parametrize("state", ["completed", "cancelled", "failed"])
async def test_finished_reader_state_never_includes_exception_message(state: str) -> None:
    async def run() -> None:
        if state == "failed":
            raise ValueError("private exception body marker")

    reader = asyncio.create_task(run())
    if state == "cancelled":
        reader.cancel()
    await asyncio.sleep(0)
    snapshot = collect_codex_startup_diagnostics(_server(reader=reader))
    assert snapshot["stderr_reader_state"] == state
    assert "private exception body marker" not in str(snapshot)
    if state == "failed":
        assert snapshot["stderr_reader_error_type"] == "ValueError"
    else:
        assert "stderr_reader_error_type" not in snapshot


async def test_stderr_overrun_cause_is_reported_without_its_payload() -> None:
    async def run() -> None:
        try:
            raise asyncio.LimitOverrunError("private diagnostic payload", 65537)
        except asyncio.LimitOverrunError as error:
            raise ValueError(str(error)) from error

    reader = asyncio.create_task(run())
    await asyncio.sleep(0)
    snapshot = collect_codex_startup_diagnostics(_server(reader=reader))
    assert snapshot["stderr_reader_error_type"] == "ValueError"
    assert snapshot["stderr_reader_cause_type"] == "LimitOverrunError"
    assert "private diagnostic payload" not in str(snapshot)


def test_preserves_simple_provider_authentication_and_mcp_failures() -> None:
    entries = [
        "ERROR Invalid configuration: unknown model provider local-test",
        "ERROR authentication failed: HTTP 401 Unauthorized",
        "ERROR MCP connection failed: connection refused",
    ]
    original = entries.copy()
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == "\n".join(entries)
    assert snapshot["stderr_lines_omitted"] == 0
    assert snapshot["stderr_tail_truncated"] is False
    assert entries == original


def test_preserves_startup_warnings_without_retaining_payloads() -> None:
    warning = _RECORD.replace("ERROR", "WARN") + "waiting for backfill lease"
    locked = _RECORD.replace("ERROR", "WARNING") + "database is locked"
    entries = [warning, "WARN prompt: private prompt marker", "private continuation", locked]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == warning + "\n" + locked
    assert snapshot["stderr_lines_omitted"] == 2
    assert "private" not in str(snapshot)


@pytest.mark.parametrize(
    "header",
    [
        "ERROR request dump:",
        'ERROR response={"status":401}',
        "ERROR prompt: private prompt marker",
        "ERROR input: private prompt marker",
        'ERROR messages=[{"content":"private prompt marker"}]',
        "ERROR instructions: private prompt marker",
        "ERROR body: private prompt marker",
        _RECORD + "Authorization:",
        _RECORD + "Cookie: session=private-cookie-marker",
        _RECORD + "Set-Cookie: session=private-cookie-marker",
        "ERROR token:",
        "ERROR password:",
        "ERROR x-private-header:",
        "ERROR failed\tAuthorization:\tBasic private-credential-marker",
        "ERROR failed\vAuthorization:",
        "ERROR failed\fAuthorization:",
    ],
)
def test_payload_and_header_continuations_are_suppressed(header: str) -> None:
    entries = [
        header,
        "invalid-private-credential-marker",
        "ERROR private prompt continuation marker",
        _RECORD + "MCP connection failed: connection refused",
    ]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == entries[-1]
    assert snapshot["stderr_lines_omitted"] == 3
    assert snapshot["stderr_tail_truncated"] is True
    assert "private" not in str(snapshot)


def test_multiline_request_dump_is_not_partially_retained() -> None:
    dump = (
        "ERROR request failed:\n"
        "POST https://user:private-password-marker@example.test/?q=private-query-marker\n"
        "Authorization: Bearer private-token-marker\n"
        "Cookie: session=private-cookie-marker\n"
        '{"messages":[{"content":"private prompt marker"}]}'
    )
    snapshot = collect_codex_startup_diagnostics(_server([dump]))
    assert snapshot["stderr_tail"] == ""
    assert snapshot["stderr_lines_omitted"] == 1
    assert "private" not in str(snapshot)


@pytest.mark.parametrize("count", [20, 25])
def test_full_ring_may_begin_in_the_middle_of_a_private_dump(count: int) -> None:
    entries = ["ERROR private acquisition marker"] * (count - 1)
    entries.append(_RECORD + "MCP connection failed: connection refused")
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == entries[-1]
    assert snapshot["stderr_lines_omitted"] == count - 1
    assert "private acquisition" not in str(snapshot)


def test_terminal_escapes_are_removed_before_payload_detection_and_redaction() -> None:
    entries = [
        "ERROR pro\x1b[31mmpt: private prompt marker",
        _RECORD + "auth failed: Bear\x1b[0mer private-token-marker",
        _RECORD + "MCP connec\u200dtion failed\x1b]0;private-title-marker\x07: refused\x00",
    ]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    tail = str(snapshot["stderr_tail"])
    assert "Bearer [REDACTED]" in tail
    assert "MCP connection failed: refused" in tail
    assert "private" not in tail
    assert "\x1b" not in tail
    assert "\x00" not in tail
    assert "\u200d" not in tail


def test_url_userinfo_query_and_fragment_are_redacted() -> None:
    line = (
        "ERROR MCP connection failed: "
        "https://private-user:private/password@example.test/rpc"
        "?q=private-query-marker&session=private-session-marker#private-fragment-marker"
    )
    snapshot = collect_codex_startup_diagnostics(_server([line]))
    assert snapshot["stderr_tail"] == (
        "ERROR MCP connection failed: https://[REDACTED]@example.test/rpc?[REDACTED]"
    )
    assert "private" not in str(snapshot)


def test_complete_line_is_redacted_before_final_tail_clipping() -> None:
    private_value = "private-token-marker" * 250
    line = "ERROR MCP failed " + "padding " * 650 + " Bearer " + private_value
    snapshot = collect_codex_startup_diagnostics(_server([line]))
    tail = str(snapshot["stderr_tail"])
    assert len(tail) == 4096
    assert tail.endswith("Bearer [REDACTED]")
    assert "private-token-marker" not in tail
    assert snapshot["stderr_tail_truncated"] is True


def test_oversized_entries_are_omitted_whole_without_exposing_a_suffix() -> None:
    entries = [
        "ERROR request dump " + "private prompt marker " * 10_000,
        "invalid-private-credential-marker",
        _RECORD + "MCP connection failed: refused",
    ]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == entries[-1]
    assert snapshot["stderr_lines_omitted"] == 2
    assert snapshot["stderr_tail_truncated"] is True


def test_large_sanitized_tail_is_bounded_and_counts_fully_omitted_lines() -> None:
    entries = [_RECORD + "MCP connection failed " + "x" * 1000 for _ in range(150)]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert len(str(snapshot["stderr_tail"])) == 4096
    assert str(snapshot["stderr_tail"]).endswith(entries[-1])
    omitted = snapshot["stderr_lines_omitted"]
    assert isinstance(omitted, int)
    assert omitted > 130
    assert snapshot["stderr_tail_truncated"] is True
