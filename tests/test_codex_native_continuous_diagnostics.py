"""Continuous stderr capture must never obstruct Codex's subprocess pipe."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.harnesses.codex_native import app_server, stderr_diagnostics
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
)
from omnigent.harnesses.codex_native.diagnostics import collect_codex_startup_diagnostics
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR, RedactingLogFormatter


class _Output(logging.Handler):
    """Replace delivery only; the real collector, sanitizer, and logger run."""

    def __init__(self) -> None:
        super().__init__()
        self.records: queue.Queue[logging.LogRecord] = queue.Queue()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail = False
        self.failed = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("test did not release the logging handler")
        if self.fail:
            self.failed.set()
            raise OSError("synthetic logging outage")
        self.records.put(record)

    async def next(self) -> logging.LogRecord:
        return await asyncio.to_thread(self.records.get, True, 5)


@pytest.fixture
def output(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Output]:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    logger = stderr_diagnostics._logger
    handler = _Output()
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        handler.release.set()
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


@pytest.fixture
def server(tmp_path: Path) -> app_server.CodexNativeAppServer:
    return app_server.CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path,
        session_id="child-session",
        recent_stderr=[],
    )


def _reader(server: app_server.CodexNativeAppServer) -> asyncio.StreamReader:
    stderr = asyncio.StreamReader()
    server.proc = cast(
        "asyncio.subprocess.Process", SimpleNamespace(stderr=stderr, pid=123, returncode=0)
    )
    server.stderr_task = asyncio.create_task(server._stderr_loop())
    return stderr


async def test_exports_startup_runtime_and_eof_with_active_session_attribution(
    server: app_server.CodexNativeAppServer,
    output: _Output,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "parent-session")
    stderr = _reader(server)
    try:
        stderr.feed_data(b"startup request password=synthetic-secret\n")
        startup = record_to_row(await output.next(), "runner")
        assert startup["session_id"] == "child-session"
        assert startup["event_name"] == "harness_diagnostic_output"
        assert "synthetic-secret" not in json.dumps(startup)
        assert startup["attributes"]["source_kind"] == "codex_app_server_stderr"
        assert startup["attributes"]["harness"] == "codex-native"
        assert startup["attributes"]["app_server_pid"] == "123"
        assert server.stderr_task is not None and not server.stderr_task.done()

        write_bridge_state(
            server.bridge_dir,
            CodexNativeBridgeState("switched-session", "codex.sock", "thread", "codex-home"),
        )
        stderr.feed_data(b"runtime provider error\n")
        runtime_record = await output.next()
        runtime = record_to_row(runtime_record, "runner")
        assert runtime["session_id"] == "switched-session"
        assert runtime["attributes"]["text"] == "runtime provider error"
        assert "runtime provider error" in RedactingLogFormatter(use_colors=False).format(
            runtime_record
        )
        assert runtime["attributes"]["launch_id"] == startup["attributes"]["launch_id"]

        stderr.feed_data(b"last EOF fragment")
        stderr.feed_eof()
        await server.stderr_task
        await server.close()
        assert (
            record_to_row(await output.next(), "runner")["attributes"]["text"]
            == "last EOF fragment"
        )
    finally:
        await server.close()


@pytest.mark.parametrize("setting", [None, "0"])
async def test_disabled_capture_preserves_drain_without_starting_exporter(
    server: app_server.CodexNativeAppServer,
    output: _Output,
    monkeypatch: pytest.MonkeyPatch,
    setting: str | None,
) -> None:
    if setting is None:
        monkeypatch.delenv(HARNESS_STDERR_ENABLED_ENV_VAR)
    else:
        monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, setting)

    def unexpected_exporter(**_kwargs: object) -> None:
        pytest.fail("disabled capture started an exporter")

    monkeypatch.setattr(app_server, "CodexStderrDiagnostics", unexpected_exporter)
    stderr = _reader(server)
    stderr.feed_data(b"ordinary stderr\n")
    stderr.feed_eof()
    await server.stderr_task
    await server.close()
    assert server.recent_stderr == ["ordinary stderr"]
    assert output.records.empty()


async def test_redacts_complete_large_record_before_clipping_and_preserves_utf8(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    stderr = _reader(server)
    # The credential crosses both pipe-read and old 64 KiB retention boundaries.
    payload = ('password="' + "synthetic-secret " * 5000 + '"\n' + "€" * 23000).encode()
    stderr.feed_data(payload)
    stderr.feed_eof()
    await server.stderr_task
    await server.close()
    row = record_to_row(await output.next(), "runner")
    text = row["attributes"]["text"]
    assert "synthetic-secret" not in json.dumps(row)
    assert len(text.encode()) <= 65536
    assert "�" not in text
    assert text.endswith("€")
    assert row["attributes"]["truncated"] == "True"


@pytest.mark.parametrize("reporter", ["working", "blocked", "unavailable"])
async def test_worker_start_failure_keeps_draining_without_debug_fallback(
    server: app_server.CodexNativeAppServer,
    output: _Output,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reporter: str,
) -> None:
    class UnavailableThread(threading.Thread):
        def start(self) -> None:
            if self.name.startswith("codex-stderr-diagnostics") or reporter == "unavailable":
                raise RuntimeError("synthetic-start-secret")
            super().start()

    monkeypatch.setattr(
        stderr_diagnostics,
        "threading",
        SimpleNamespace(Thread=UnavailableThread, Lock=threading.Lock, Event=threading.Event),
    )
    if reporter == "blocked":
        output.release.clear()
    # A fallback DEBUG write would contend with the blocked warning handler.
    app_server._logger.addHandler(output)
    try:
        with caplog.at_level(logging.DEBUG, logger=app_server._logger.name):
            stderr = _reader(server)
            stderr.feed_data(b"password synthetic-secret\nafter failed capture\n")
            stderr.feed_eof()
            await asyncio.wait_for(server.stderr_task, 5)
            assert server.recent_stderr == [
                "password synthetic-secret",
                "after failed capture",
            ]
            snapshot = collect_codex_startup_diagnostics(server)
            assert snapshot["stderr_reader_state"] == "completed"
            assert snapshot["stderr_capture_error_type"] == "RuntimeError"
            assert "synthetic-secret" not in json.dumps(snapshot)
            await asyncio.wait_for(server.close(), 2)
            assert "synthetic-secret" not in caplog.text
            assert "synthetic-start-secret" not in caplog.text
        if reporter != "unavailable":
            assert await asyncio.to_thread(output.entered.wait, 5)
            if reporter == "blocked":
                assert output.records.empty()
            output.release.set()
            row = record_to_row(await output.next(), "runner")
            assert row["event_name"] == "harness_diagnostic_capture_failed"
            assert row["session_id"] == "child-session"
            assert row["attributes"]["error_type"] == "RuntimeError"
            assert "synthetic-secret" not in json.dumps(row)
            assert "synthetic-start-secret" not in json.dumps(row)
        assert output.records.empty()
    finally:
        output.release.set()
        app_server._logger.removeHandler(output)
        await server.close()


async def test_oversized_record_is_omitted_whole_then_capture_recovers(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    stderr = _reader(server)
    too_large = b"password=" + b"s" * (2 * stderr_diagnostics.MAX_STDERR_RECORD_BYTES) + b"\n"
    stderr.feed_data(too_large + b"after oversized record\n")
    stderr.feed_eof()
    await server.stderr_task
    await server.close()
    row = record_to_row(await output.next(), "runner")
    assert row["attributes"]["text"] == "after oversized record"
    assert row["attributes"]["lines_omitted"] == "1"
    assert int(row["attributes"]["bytes_omitted"]) == len(too_large)
    assert row["attributes"]["truncated"] == "True"


async def test_blocked_exporter_does_not_block_child_or_shutdown(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    child = """
import sys
sys.stderr.write('first diagnostic\\n')
sys.stderr.flush()
sys.stdin.readline()
for i in range(512):
    sys.stderr.buffer.write(b'x' * 8192 + b'\\n')
sys.stderr.buffer.write(b'newest diagnostic\\n')
sys.stderr.buffer.flush()
print('ready after flood', flush=True)
sys.stdin.readline()
"""
    output.release.clear()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    server.proc = proc
    server.stderr_task = asyncio.create_task(server._stderr_loop())
    diagnostics = None
    try:
        assert await asyncio.to_thread(output.entered.wait, 5)
        diagnostics = server._stderr_diagnostics
        assert diagnostics is not None
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(b"flood\n")
        await proc.stdin.drain()
        assert await asyncio.wait_for(proc.stdout.readline(), 5) == b"ready after flood\n"
        proc.stdin.write(b"exit\n")
        await proc.stdin.drain()
        await asyncio.wait_for(proc.wait(), 5)
        # stdout and stderr are independent pipes; wait for the final stderr bytes.
        await asyncio.wait_for(server.stderr_task, 5)
        assert server.recent_stderr is not None
        assert server.recent_stderr[-1] == "newest diagnostic"
        with diagnostics._lock:
            assert diagnostics._queued_bytes <= stderr_diagnostics._QUEUE_BYTES
            assert len(diagnostics._records) <= stderr_diagnostics._QUEUE_RECORDS
        # Still blocked in a real logging handler; close has a finite join budget.
        await asyncio.wait_for(server.close(), 3)
        assert not output.release.is_set()
    finally:
        output.release.set()
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.communicate(), 5)
        await server.close()
        if diagnostics is not None:
            await asyncio.to_thread(diagnostics.close)
    first, final = await output.next(), await output.next()
    assert first.getMessage().endswith("first diagnostic")
    row = record_to_row(final, "runner")
    assert row["attributes"]["text"].endswith("newest diagnostic")
    assert int(row["attributes"]["lines_omitted"]) > 0
    assert int(row["attributes"]["bytes_omitted"]) > 0


@pytest.mark.parametrize("overflow", [False, True])
async def test_record_limit_includes_newline(
    server: app_server.CodexNativeAppServer, output: _Output, overflow: bool
) -> None:
    stderr = _reader(server)
    payload = b"x" * (stderr_diagnostics.MAX_STDERR_RECORD_BYTES - 1 + overflow) + b"\n"
    stderr.feed_data(payload)
    stderr.feed_eof()
    await server.stderr_task
    await server.close()
    row = record_to_row(await output.next(), "runner")
    attrs = row["attributes"]
    if overflow:
        assert attrs["text"] == ""
        assert attrs["lines_omitted"] == "1"
        assert int(attrs["bytes_omitted"]) == len(payload)
    else:
        assert attrs["text"] == "x" * 65536
        assert attrs["lines_omitted"] == "0"
    assert int(attrs["offset"]) == len(payload)


async def test_tiny_record_flood_sheds_old_records_with_counts(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    stderr = _reader(server)
    output.release.clear()
    try:
        stderr.feed_data(b"first\n")
        assert await asyncio.to_thread(output.entered.wait, 5)
        stderr.feed_data(b"x\n" * 1000)
        stderr.feed_eof()
        await server.stderr_task
    finally:
        output.release.set()
        await server.close()
    await output.next()
    attrs = record_to_row(await output.next(), "runner")["attributes"]
    assert attrs["text"].splitlines() == ["x"] * 256
    assert int(attrs["lines_omitted"]) == 1000 - 256
    assert int(attrs["bytes_omitted"]) == 2 * (1000 - 256)


async def test_export_failure_does_not_stop_later_capture(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    stderr = _reader(server)
    output.fail = True
    try:
        stderr.feed_data(b"logging unavailable\n")
        assert await asyncio.to_thread(output.failed.wait, 5)
        output.fail = False
        stderr.feed_data(b"logging recovered\n")
        stderr.feed_eof()
        await server.stderr_task
        await server.close()
        assert (await output.next()).getMessage().endswith("logging recovered")
    finally:
        await server.close()


async def test_cancellation_flushes_buffer_and_stops_worker(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    stderr = _reader(server)
    stderr.feed_data(b"before cancellation\n")
    await output.next()
    diagnostics = server._stderr_diagnostics
    assert diagnostics is not None and server.stderr_task is not None
    stderr.feed_data(b"trailing fragment")
    await asyncio.sleep(0)
    server.stderr_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server.stderr_task
    await server.close()
    assert (await output.next()).getMessage().endswith("trailing fragment")
    assert not diagnostics._thread.is_alive()


async def test_failed_reader_preserves_error_and_allows_exporter_cleanup(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    stderr = _reader(server)
    stderr.feed_data(b"before reader failure\n")
    await output.next()
    diagnostics = server._stderr_diagnostics
    assert diagnostics is not None and server.stderr_task is not None
    stderr.set_exception(OSError("synthetic read failure"))
    with pytest.raises(OSError, match="synthetic read failure"):
        await server.stderr_task
    await server.close()
    assert not diagnostics._thread.is_alive()


async def test_cancelling_close_during_eof_wait_still_cleans_up(
    server: app_server.CodexNativeAppServer, output: _Output
) -> None:
    # The process has exited, but a descendant still owns the stderr pipe.
    stderr = _reader(server)
    stderr.feed_data(b"before close\n")
    await output.next()
    diagnostics, reader = server._stderr_diagnostics, server.stderr_task
    assert diagnostics is not None and reader is not None
    stderr.feed_data(b"pending final fragment")
    await asyncio.sleep(0)
    closing = asyncio.create_task(server.close())
    await asyncio.sleep(0)
    assert not closing.done()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 3)
    assert reader.cancelled()
    assert not diagnostics._thread.is_alive()
    assert server.stderr_task is None and server._stderr_diagnostics is None
    assert server.proc is None
    assert (await output.next()).getMessage().endswith("pending final fragment")
