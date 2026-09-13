"""Startup events identify observed boundaries without inferring TUI readiness."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent import _startup_events as startup
from omnigent import debug_logging


@pytest.fixture(autouse=True)
def isolate_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(startup.STARTUP_CONTEXT_ENV, raising=False)
    monkeypatch.setattr(debug_logging, "attach_debug_log_sink", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        startup,
        "time",
        SimpleNamespace(monotonic_ns=lambda: 10_000_000_000, time_ns=lambda: 20_000_000_000),
    )


def records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [r.attributes for r in caplog.records if r.name == "omnigent.startup"]


def context(**changes: object) -> str:
    return json.dumps(
        {
            "version": 1,
            "attempt_id": "00000000-0000-4000-8000-000000000001",
            "elapsed_ms": 2500,
            "started_at_unix_ms": 16000,
            "handoff_monotonic_ns": 9_000_000_000,
            "pid": os.getpid(),
            **changes,
        }
    )


def test_exec_handoff_includes_wrapper_and_import_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.startup")
    monkeypatch.setenv(startup.STARTUP_CONTEXT_ENV, context())

    @startup.capture_cli_entry
    def main() -> None:
        assert startup.STARTUP_CONTEXT_ENV not in os.environ
        with startup.codex_startup_attempt():
            startup.record_startup_event("session_resolved", session_id="conv_synthetic")
            monkeypatch.setattr(startup.time, "monotonic_ns", lambda: 11_000_000_000)
            monkeypatch.setattr(startup.time, "time_ns", lambda: 1)  # wall clock jumps
            startup.record_startup_event("terminal_available")
            startup.record_startup_event("terminal_attach_started")

    main()
    events = records(caplog)
    assert [e["elapsed_ms"] for e in events] == [3500, 3500, 4500, 4500]
    assert {e["attempt_id"] for e in events} == {"00000000-0000-4000-8000-000000000001"}
    assert {e["start_boundary"] for e in events} == {"wrapper_entry"}
    assert {e["started_at_unix_ms"] for e in events} == {16000}
    assert [r.session_id for r in caplog.records] == [
        None,
        "conv_synthetic",
        "conv_synthetic",
        "conv_synthetic",
    ]
    assert startup._entry.get() is None
    assert startup._attempt.get() is None


@pytest.mark.parametrize(
    "value",
    [
        "malformed",
        "[" * 1000 + "0" + "]" * 1000,
        "[]",
        context(version=2),
        context(version=True),
        context(pid=os.getpid() + 1),
        context(elapsed_ms=-1),
        context(elapsed_ms=float("nan")),
        context(elapsed_ms=True),
        context(handoff_monotonic_ns=11_000_000_000),
        context(handoff_monotonic_ns=True),
        context(attempt_id="not-a-uuid"),
        context(started_at_unix_ms=0),
        "x" * 2049,
    ],
)
def test_invalid_or_cross_process_handoff_is_discarded(
    value: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.startup")
    monkeypatch.setenv(startup.STARTUP_CONTEXT_ENV, value)
    with startup.codex_startup_attempt():
        pass
    assert startup.STARTUP_CONTEXT_ENV not in os.environ
    assert records(caplog)[0]["start_boundary"] == "omnigent_cli_entry"
    assert records(caplog)[0]["elapsed_ms"] == 0
    assert records(caplog)[-1]["event"] == "launch_incomplete"


@pytest.mark.parametrize(
    ("error", "event"),
    [
        (RuntimeError("private details"), "launch_failed"),
        (TimeoutError(), "launch_failed"),
        (KeyboardInterrupt(), "launch_cancelled"),
        (asyncio.CancelledError(), "launch_cancelled"),
    ],
)
def test_unsuccessful_attempt_retained_without_content(
    error: BaseException, event: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.startup")
    with pytest.raises(type(error)):
        with startup.codex_startup_attempt():
            raise error
    assert [e["event"] for e in records(caplog)] == ["launch_started", event]
    assert "private details" not in caplog.text
    assert startup._attempt.get() is None


def test_runtime_failure_does_not_reclassify_startup(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.startup")
    with pytest.raises(RuntimeError):
        with startup.codex_startup_attempt():
            startup.record_startup_event("terminal_available")
            startup.record_startup_event("terminal_available")
            startup.record_startup_event("terminal_attach_started")
            startup.record_startup_event("terminal_attach_exited", exit_code=1)
            raise RuntimeError("later attach failure")
    assert [e["event"] for e in records(caplog)] == [
        "launch_started",
        "terminal_available",
        "terminal_attach_started",
        "terminal_attach_exited",
    ]
    assert records(caplog)[-1]["exit_code"] == 1


def test_uploader_and_logger_failure_do_not_break_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr(debug_logging, "attach_debug_log_sink", broken)
    monkeypatch.setattr(startup._logger, "info", broken)
    with startup.codex_startup_attempt():
        startup.record_startup_event("terminal_available")


def test_structured_rows_reach_existing_debug_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = []
    handler = logging.Handler()
    handler.emit = lambda record: rows.append(debug_logging.record_to_row(record, "cli"))

    def attach(loggers: list[logging.Logger], **kwargs: object) -> None:
        assert kwargs == {"source": "cli", "level": logging.INFO}
        for logger in loggers:
            logger.addHandler(handler)

    monkeypatch.setattr(debug_logging, "attach_debug_log_sink", attach)
    try:
        with startup.codex_startup_attempt():
            startup.record_startup_event("session_resolved", session_id="conv_synthetic")
            startup.record_startup_event("terminal_available")
    finally:
        startup._logger.removeHandler(handler)
    assert len(rows) == 4
    assert rows[0]["event_name"] == "client_startup"
    assert rows[0]["attributes"]["event"] == "launch_started"
    assert rows[2]["attributes"]["event"] == "terminal_available"
    assert rows[2]["session_id"] == "conv_synthetic"
    assert rows[2]["source"] == "cli"


def test_warning_cli_level_does_not_drop_uploaded_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent import cli_diagnostics

    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(cli_diagnostics, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_diagnostics, "_current", None)
    loggers = [logging.getLogger(name) for name in ("omnigent", "omnigent_ui_sdk")]
    for logger in loggers:
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "level", logger.level)
        monkeypatch.setattr(logger, "propagate", logger.propagate)
    monkeypatch.setattr(startup._logger, "level", logging.NOTSET)
    uploaded = []
    handler = logging.Handler()
    handler.emit = lambda record: uploaded.append(debug_logging.record_to_row(record, "cli"))

    def attach(loggers: list[logging.Logger], **kwargs: object) -> None:
        for logger in loggers:
            logger.addHandler(handler)

    monkeypatch.setattr(debug_logging, "attach_debug_log_sink", attach)
    cli_log = cli_diagnostics.setup_cli_logging(["codex"])
    try:
        assert startup._logger.getEffectiveLevel() == logging.WARNING
        with startup.codex_startup_attempt():
            startup.record_startup_event("terminal_available", session_id="conv_synthetic")
        assert [row["attributes"]["event"] for row in uploaded] == [
            "launch_started",
            "terminal_available",
            "launch_incomplete",
        ]
        assert "client_startup" not in cli_log.path.read_text()
    finally:
        startup._logger.removeHandler(handler)
        for logger in loggers:
            for local_handler in logger.handlers:
                local_handler.close()


@pytest.mark.asyncio
async def test_attach_subprocess_spawn_is_distinct_from_availability(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from pathlib import Path

    from omnigent.harnesses.codex_native import main as native

    caplog.set_level(logging.INFO, logger="omnigent.startup")

    async def wait() -> int:
        assert records(caplog)[-1]["event"] == "terminal_attach_started"
        return 1

    async def spawn(*args: object, **kwargs: object) -> SimpleNamespace:
        assert records(caplog)[-1]["event"] == "terminal_available"
        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(native, "asyncio", SimpleNamespace(create_subprocess_exec=spawn))
    with startup.codex_startup_attempt():
        startup.record_startup_event("terminal_available")
        await native._attach_direct_tmux(Path("/tmp/synthetic.sock"), "synthetic:main")
    assert records(caplog)[-1]["event"] == "terminal_attach_exited"
    assert records(caplog)[-1]["exit_code"] == 1


@pytest.mark.parametrize("args", [[], ["--resume", "conv_synthetic"]])
def test_codex_callback_tracks_backend_failure_before_native_launch(
    args: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from click.testing import CliRunner

    from omnigent import cli as cli_module

    caplog.set_level(logging.INFO, logger="omnigent.startup")
    monkeypatch.setattr(cli_module, "_load_effective_config", dict)

    def fail_backend(*args: object, **kwargs: object) -> None:
        assert records(caplog)[-1]["event"] == "launch_started"
        raise RuntimeError("synthetic backend failure")

    monkeypatch.setattr(cli_module, "_ensure_backend", fail_backend)
    result = CliRunner().invoke(cli_module.cli, ["codex", *args])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert [e["event"] for e in records(caplog)] == ["launch_started", "launch_failed"]
    assert {e["launch_kind"] for e in records(caplog)} == {"resume" if args else "create"}
