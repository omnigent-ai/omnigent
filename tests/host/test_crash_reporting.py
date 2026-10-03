"""Tests for host daemon crash and exit reporting."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.host import crash_reporting as cr


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == name]


@pytest.fixture
def fresh_hooks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install the hooks against sentinel previous hooks; restore after."""
    monkeypatch.setattr(cr, "_hooks_installed", False)
    monkeypatch.setattr(cr, "_state", cr._HostExitState())
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    yield


def test_report_host_exit_logs_once(fresh_hooks: None, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=cr.__name__)
    cr.install_host_crash_hooks()
    cr.set_host_exit_context(daemon_target="local", host_id="host_1")

    assert cr.report_host_exit("fatal_connect", exit_code=78, error="HTTP 403") is True
    assert cr.report_host_exit("uncaught", exit_code=1) is False

    (record,) = _events(caplog, cr.HOST_EXITING_EVENT)
    assert record.levelno == logging.ERROR
    attrs = record.attributes  # type: ignore[attr-defined]
    assert attrs["reason"] == "fatal_connect"
    assert attrs["exit_code"] == 78
    assert attrs["error"] == "HTTP 403"
    assert attrs["host_id"] == "host_1"
    assert attrs["daemon_target"] == "local"
    assert attrs["pid"] == os.getpid()


def test_excepthook_reports_uncaught_and_chains(
    fresh_hooks: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chained: list[type[BaseException]] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: chained.append(t))
    cr.install_host_crash_hooks()

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        sys.excepthook(RuntimeError, exc, exc.__traceback__)

    (record,) = _events(caplog, cr.HOST_EXITING_EVENT)
    assert record.levelno == logging.CRITICAL
    assert record.attributes["reason"] == "uncaught"  # type: ignore[attr-defined]
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert chained == [RuntimeError]


def test_excepthook_skips_already_reported_exit(
    fresh_hooks: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_host_process`` reports first; the hook must not log a duplicate."""
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: None)
    cr.install_host_crash_hooks()
    exc = RuntimeError("boom")
    cr.report_host_exit("uncaught", exit_code=1, exc_info=(RuntimeError, exc, None))

    sys.excepthook(RuntimeError, exc, None)

    assert len(_events(caplog, cr.HOST_EXITING_EVENT)) == 1


def test_thread_crash_is_logged_and_chained(
    fresh_hooks: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chained: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", chained.append)
    cr.install_host_crash_hooks()

    def _crash() -> None:
        raise ValueError("thread boom")

    thread = threading.Thread(target=_crash, name="crashy")
    thread.start()
    thread.join()

    (record,) = _events(caplog, cr.HOST_THREAD_CRASHED_EVENT)
    assert record.attributes["thread"] == "crashy"  # type: ignore[attr-defined]
    assert record.exc_info is not None and record.exc_info[0] is ValueError
    assert len(chained) == 1


def test_asyncio_handler_logs_unretrieved_task_error(caplog: pytest.LogCaptureFixture) -> None:
    async def _main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(cr.host_asyncio_exception_handler)
        task = asyncio.create_task(asyncio.sleep(0), name="host-test-task")
        await task
        loop.call_exception_handler(
            {
                "message": "Task exception was never retrieved",
                "exception": KeyError("k"),
                "task": task,
            }
        )

    asyncio.run(_main())

    (record,) = _events(caplog, cr.HOST_TASK_ERROR_EVENT)
    assert record.attributes["task"] == "host-test-task"  # type: ignore[attr-defined]
    assert record.exc_info is not None and record.exc_info[0] is KeyError


_CHILD_PRELUDE = """
import json, logging, sys, time
from omnigent import debug_logging as dl
from omnigent.host import crash_reporting as cr

out = sys.argv[1]

def send(batch):
    with open(out, "a") as f:
        for row in batch:
            f.write(json.dumps(row) + "\\n")

root = logging.getLogger()
root.setLevel(logging.INFO)
dl.attach_debug_log_sink([root], source="host", level=logging.INFO, send=send)
cr.install_host_crash_hooks()
"""


def _run_child(body: str, rows_path: Path) -> subprocess.Popen[str]:
    script = _CHILD_PRELUDE + textwrap.dedent(body)
    return subprocess.Popen(
        [sys.executable, "-c", script, str(rows_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _exit_rows(rows_path: Path) -> list[dict[str, object]]:
    if not rows_path.exists():
        return []
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    return [r for r in rows if r["event_name"] == cr.HOST_EXITING_EVENT]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_stop_signal_reaches_sink_then_dies_by_signal(tmp_path: Path, signum: int) -> None:
    rows_path = tmp_path / "rows.jsonl"
    proc = _run_child(
        """
        cr.install_host_signal_handlers()
        print("ready", flush=True)
        while True:
            time.sleep(0.05)
        """,
        rows_path,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signum)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    # Supervisors still see a signal death, exactly as before the handler.
    assert proc.returncode == -signum
    (row,) = _exit_rows(rows_path)
    attrs = row["attributes"]
    assert isinstance(attrs, dict)
    assert attrs["reason"] == "signal"
    assert attrs["signal"] == signal.Signals(signum).name
    assert attrs["exit_code"] == str(128 + signum)


def test_uncaught_crash_reaches_sink(tmp_path: Path) -> None:
    rows_path = tmp_path / "rows.jsonl"
    proc = _run_child('raise RuntimeError("daemon exploded")\n', rows_path)
    _, stderr = proc.communicate(timeout=30)

    assert proc.returncode == 1
    # The chained default hook still prints the traceback to the log file.
    assert "daemon exploded" in stderr
    (row,) = _exit_rows(rows_path)
    assert row["level"] == "CRITICAL"
    attrs = row["attributes"]
    assert isinstance(attrs, dict)
    assert attrs["reason"] == "uncaught"
    assert attrs["exception_type"] == "RuntimeError"
    assert "daemon exploded" in str(row["stack_trace"])


def test_close_debug_log_sink_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent import debug_logging as dl

    class _SlowSink:
        closed = False

        def close(self) -> None:
            time.sleep(5)

    monkeypatch.setattr(dl, "_active_sink", _SlowSink())
    started = time.monotonic()
    dl.close_debug_log_sink(timeout_s=0.1)
    assert time.monotonic() - started < 2
