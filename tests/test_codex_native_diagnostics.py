"""Opt-in stderr and lifecycle coverage for Codex startup snapshots."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from omnigent.harnesses.codex_native.diagnostics import collect_codex_startup_diagnostics
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer

_RECORD = "2026-09-21T12:00:00.000Z ERROR "


@pytest.fixture(autouse=True)
def clear_capture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HARNESS_STDERR_ENABLED_ENV_VAR, raising=False)


@pytest.fixture
def capture_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")


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


def test_unavailable_capture_and_empty_completed_line_buffer_are_distinct(
    capture_stderr: None,
) -> None:
    unavailable = collect_codex_startup_diagnostics(None)
    not_started = collect_codex_startup_diagnostics(_server())
    empty = collect_codex_startup_diagnostics(_server([]))

    assert unavailable == {
        "app_server_state": "unavailable",
        "stderr_reader_state": "unavailable",
        "stderr_capture_enabled": True,
        "stderr_tail_available": False,
        "stderr_tail": "",
        "stderr_tail_truncated": False,
        "stderr_lines_omitted": 0,
        "stderr_bytes_omitted": 0,
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


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "unexpected"])
def test_disabled_capture_does_not_read_stderr(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, value)

    class ServerWithoutReadableStderr:
        proc = SimpleNamespace(pid=4242, returncode=None)
        stderr_task = None
        codex_cli_version = (0, 154, 0)

        @property
        def recent_stderr(self) -> list[str]:
            raise AssertionError("stderr must not be read without opt-in")

    snapshot = collect_codex_startup_diagnostics(
        cast("CodexNativeAppServer", ServerWithoutReadableStderr())
    )
    assert snapshot["stderr_capture_enabled"] is False
    assert snapshot["app_server_state"] == "running"
    assert snapshot["app_server_pid"] == 4242
    assert snapshot["codex_version"] == "0.154.0"
    assert snapshot["stderr_reader_state"] == "not_started"
    assert not any(key.startswith("stderr_tail") or key.endswith("_omitted") for key in snapshot)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_capture_requires_explicit_truthy_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, value)
    snapshot = collect_codex_startup_diagnostics(_server(["startup detail"]))
    assert snapshot["stderr_capture_enabled"] is True
    assert snapshot["stderr_tail"] == "startup detail"


def test_preserves_diagnostic_context_and_complete_tracebacks(capture_stderr: None) -> None:
    entries = [
        "INFO startup: waiting for model provider",
        "ERROR failed to parse response body: unexpected EOF",
        "WARN request headers invalid: content-type is missing",
        "Traceback (most recent call last):",
        '  File "startup.py", line 42, in initialize',
        "ValueError: invalid input",
        "additional context without a severity label",
        '{"error": {"message": "connection refused"}}',
    ]
    original = entries.copy()
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == "\n".join(entries)
    assert snapshot["stderr_lines_omitted"] == 0
    assert snapshot["stderr_bytes_omitted"] == 0
    assert snapshot["stderr_tail_truncated"] is False
    assert entries == original


def test_uses_shared_credential_redaction_without_dropping_diagnostics(
    capture_stderr: None,
) -> None:
    entries = [
        "ERROR request failed: Authorization: Bearer synthetic-token-marker",
        "ERROR authentication failed: api_key=synthetic-key-marker status=401",
        'ERROR provider failed: {"password": "synthetic-password-marker", "status": 403}',
    ]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    tail = str(snapshot["stderr_tail"])
    assert "Authorization: [REDACTED]" in tail
    assert "api_key=[REDACTED] status=401" in tail
    assert '"password": "[REDACTED]", "status": 403' in tail
    assert "synthetic-" not in tail
    assert snapshot["stderr_lines_omitted"] == 0
    assert snapshot["stderr_tail_truncated"] is False


def test_terminal_controls_are_removed_before_credential_redaction(capture_stderr: None) -> None:
    entries = [
        _RECORD + "auth failed: Bear\x1b[0mer synthetic-token-marker",
        _RECORD + "MCP connec\u200dtion failed\x1b]0;synthetic-title-marker\x07: refused\x00",
    ]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    tail = str(snapshot["stderr_tail"])
    assert "Bearer [REDACTED]" in tail
    assert "MCP connection failed: refused" in tail
    assert "synthetic-" not in tail
    assert "\x1b" not in tail
    assert "\x00" not in tail
    assert "\u200d" not in tail


def test_multiline_entry_keeps_traceback_context(capture_stderr: None) -> None:
    diagnostic = "request failed:\nTraceback:\n  initialize()\nValueError: invalid response body"
    snapshot = collect_codex_startup_diagnostics(_server([diagnostic]))
    assert snapshot["stderr_tail"] == diagnostic
    assert snapshot["stderr_tail_truncated"] is False


def test_budget_keeps_complete_recent_entries(capture_stderr: None) -> None:
    entries = ["old " + "a" * 30_000, "middle " + "b" * 30_000, "recent " + "c" * 30_000]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == "\n".join(entries[1:])
    assert snapshot["stderr_lines_omitted"] == 1
    assert snapshot["stderr_bytes_omitted"] == len(entries[0]) + 1
    assert snapshot["stderr_tail_truncated"] is True


def test_exact_utf8_budget_includes_separator(capture_stderr: None) -> None:
    entries = ["a", "é" * 32_767]
    snapshot = collect_codex_startup_diagnostics(_server(entries))
    assert snapshot["stderr_tail"] == "\n".join(entries)
    assert len(str(snapshot["stderr_tail"]).encode("utf-8")) == 65_536
    assert snapshot["stderr_tail_truncated"] is False
    snapshot = collect_codex_startup_diagnostics(_server(["aa", entries[1]]))
    assert snapshot["stderr_tail"] == entries[1]
    assert snapshot["stderr_lines_omitted"] == 1
    assert snapshot["stderr_bytes_omitted"] == 3


def test_single_oversized_entry_keeps_valid_utf8_tail(capture_stderr: None) -> None:
    line = "€" * 30_000 + " root cause"
    snapshot = collect_codex_startup_diagnostics(_server(["earlier line", line]))
    tail = str(snapshot["stderr_tail"])
    assert len(tail.encode("utf-8")) <= 65_536
    assert line.endswith(tail)
    assert tail.endswith(" root cause")
    assert "\ufffd" not in tail
    assert snapshot["stderr_tail_truncated"] is True
    assert snapshot["stderr_lines_omitted"] == 1
    assert snapshot["stderr_bytes_omitted"] == len(
        ("earlier line\n" + line).encode("utf-8")
    ) - len(tail.encode("utf-8"))


def test_redacts_complete_entry_before_clipping(capture_stderr: None) -> None:
    line = "context " * 10_000 + " Bearer " + "synthetic-token-marker" * 5_000
    snapshot = collect_codex_startup_diagnostics(_server([line]))
    tail = str(snapshot["stderr_tail"])
    assert len(tail.encode("utf-8")) == 65_536
    assert tail.endswith("Bearer [REDACTED]")
    assert "synthetic-token-marker" not in tail
    assert snapshot["stderr_tail_truncated"] is True
    assert snapshot["stderr_lines_omitted"] == 0
