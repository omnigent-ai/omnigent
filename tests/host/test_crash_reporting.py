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
from collections.abc import Callable, Iterator
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


def test_exit_events_never_carry_url_credentials(
    fresh_hooks: None, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.debug_logging import record_to_row

    caplog.set_level(logging.INFO, logger=cr.__name__)
    cr.install_host_crash_hooks()
    cr.set_host_exit_context(
        daemon_target="https://synthetic-user:synthetic-password@example.invalid/api"
    )
    cr.log_host_started()
    cr.report_host_exit("clean", exit_code=0)

    records = _events(caplog, cr.HOST_STARTED_EVENT) + _events(caplog, cr.HOST_EXITING_EVENT)
    assert len(records) == 2
    for record in records:
        assert record.attributes["daemon_target"] == "https://example.invalid/api"  # type: ignore[attr-defined]
        assert "synthetic-password" not in json.dumps(record_to_row(record, "host"))


_CREDENTIAL_URL = "https://synthetic-user:synthetic-password@example.invalid/login"


def _emit_fatal_error() -> None:
    cr.report_host_exit(
        "fatal_connect", exit_code=78, error=f"HTTP 401. Sign in at {_CREDENTIAL_URL}"
    )


def _emit_uncaught() -> None:
    try:
        raise RuntimeError(f"could not reach {_CREDENTIAL_URL}")
    except RuntimeError as exc:
        cr.report_host_exit(
            "uncaught", exit_code=1, exc_info=(RuntimeError, exc, exc.__traceback__)
        )


def _emit_thread_crash() -> None:
    def _crash() -> None:
        raise ValueError(f"thread lost {_CREDENTIAL_URL}")

    thread = threading.Thread(target=_crash, name="crashy")
    thread.start()
    thread.join()


def _emit_task_error() -> None:
    async def _main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(cr.host_asyncio_exception_handler)
        loop.call_exception_handler(
            {
                "message": f"task failed for {_CREDENTIAL_URL}",
                "exception": KeyError(_CREDENTIAL_URL),
            }
        )

    asyncio.run(_main())


@pytest.mark.parametrize(
    "emit",
    [_emit_fatal_error, _emit_uncaught, _emit_thread_crash, _emit_task_error],
    ids=["fatal-error-attr", "uncaught-message-and-traceback", "thread-crash", "task-error"],
)
def test_host_events_never_upload_url_credentials(
    fresh_hooks: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    emit: Callable[[], None],
) -> None:
    """Message, stack trace and attributes all pass the sink's redaction."""
    from omnigent.debug_logging import record_to_row

    monkeypatch.setattr(threading, "excepthook", lambda _args: None)  # quiet chained hook
    caplog.set_level(logging.INFO, logger=cr.__name__)
    cr.install_host_crash_hooks()
    emit()

    records = [r for r in caplog.records if getattr(r, "event_name", "").startswith("host_")]
    assert records
    for record in records:
        row = json.dumps(record_to_row(record, "host"))
        assert "example.invalid" in row  # the URL is still there, minus its userinfo
        assert "synthetic-password" not in row
        assert "synthetic-user" not in row


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_wedged_flush_still_dies_by_signal_after_the_deadline(tmp_path: Path) -> None:
    """The hard-deadline fallback re-raises the signal instead of a plain exit."""
    rows_path = tmp_path / "rows.jsonl"
    proc = _run_child(
        """
        import threading

        class _WedgedFlush(logging.Handler):
            def emit(self, record):
                pass

            def flush(self):
                threading.Event().wait()  # never returns

        root.addHandler(_WedgedFlush())
        cr._SIGNAL_EXIT_DEADLINE_S = 0.5
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
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == -signal.SIGTERM
    assert elapsed < 3.0
    # The exit row was drained before the wedged flush.
    (row,) = _exit_rows(rows_path)
    assert isinstance(row["attributes"], dict)
    assert row["attributes"]["reason"] == "signal"


_STARTUP_CHILD = """
import functools, json, sys, time
from pathlib import Path
import omnigent.git_credential_github as git_credential_github
import omnigent.host.connect as connect
import omnigent.process_logging as process_logging

rows_path = sys.argv[1]

def send(batch):
    with open(rows_path, "a") as f:
        for row in batch:
            f.write(json.dumps(row) + "\\n")

connect.configure_process_logging = functools.partial(
    process_logging.configure_process_logging, debug_log_send=send
)

def stuck_startup(*_args, **_kwargs):
    print("ready", flush=True)
    time.sleep(60)  # e.g. a slow credential broker

git_credential_github.configure_host_git = stuck_startup
connect.run_host_process(
    server_url="https://app.example.databricks.com", config_path=Path(sys.argv[2])
)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_stop_signal_during_startup_is_reported(tmp_path: Path) -> None:
    """A stop signal before the host loop starts still reports and drains."""
    rows_path = tmp_path / "rows.jsonl"
    proc = subprocess.Popen(
        [sys.executable, "-c", _STARTUP_CHILD, str(rows_path), str(tmp_path / "config.yaml")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        while line and line.strip() != "ready":  # skip run_host_process's banner lines
            line = proc.stdout.readline()
        assert line.strip() == "ready"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == -signal.SIGTERM
    (row,) = _exit_rows(rows_path)
    assert isinstance(row["attributes"], dict)
    assert row["attributes"]["reason"] == "signal"


_RACING_CHILD = """
import functools, json, os, signal, sys, time
from pathlib import Path
from omnigent import debug_logging
from omnigent.host import crash_reporting
import omnigent.host.connect as connect
import omnigent.process_logging as process_logging

rows_path, signum, when, config_path = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]

def send(batch):
    with open(rows_path, "a") as f:
        for row in batch:
            f.write(json.dumps(row) + "\\n")

connect.configure_process_logging = functools.partial(
    process_logging.configure_process_logging, debug_log_send=send
)

def kill_self():
    os.kill(os.getpid(), signum)  # e.g. `host stop` while the host is already exiting
    time.sleep(0.5)  # let a default action land before continuing

def serve(*_args, **_kwargs):
    if when.startswith("in-body"):
        kill_self()
    return False

connect._serve_host_until_exit = serve

def previous_handler(*_args):
    pass  # e.g. a launcher's own handler; it must not swallow the re-raise

if when.endswith("with-previous-handler"):
    signal.signal(signum, previous_handler)

if when.startswith("mid-restore"):
    real_install = crash_reporting.install_host_signal_handlers

    def install_then_race_the_restore():
        restore = real_install()
        real_signal = signal.signal

        def signal_racing_restore(sig, handler):
            if handler is not previous_handler:  # only _restore() putting it back
                return real_signal(sig, handler)
            signal.signal = real_signal
            os.kill(os.getpid(), sig)  # lands mid-restore, after its guard
            time.sleep(0.05)  # our still-installed handler takes ownership
            result = real_signal(sig, handler)  # overwrites its SIG_DFL...
            time.sleep(0.3)  # ...and the fast exit worker re-raises right here
            return result

        signal.signal = signal_racing_restore
        return restore

    crash_reporting.install_host_signal_handlers = install_then_race_the_restore
    real_drain = crash_reporting.drain_debug_sink

    def drain_into_the_window():
        import threading

        if threading.current_thread().name == "host-signal-exit":
            time.sleep(0.15)  # re-raise lands after the overwrite, before the re-arm
        real_drain()

    crash_reporting.drain_debug_sink = drain_into_the_window

if when == "in-body-no-thread":
    import threading

    real_start = threading.Thread.start

    def start_unless_exit_thread(self):
        if self.name == "host-signal-exit":
            raise RuntimeError("can't start new thread")
        real_start(self)

    threading.Thread.start = start_unless_exit_thread

if when.startswith("during-drain"):
    real_close = debug_logging.close_debug_log_sink

    def close_with_signal(timeout=5.0):
        crash_reporting.close_debug_log_sink = real_close  # signal the first drain only
        os.kill(os.getpid(), signum)
        real_close(timeout=timeout)

    crash_reporting.close_debug_log_sink = close_with_signal

if when == "after-restore":
    real_install = crash_reporting.install_host_signal_handlers

    def install_then_signal_after_restore():
        restore = real_install()

        def restore_then_signal():
            restore()
            kill_self()

        return restore_then_signal

    crash_reporting.install_host_signal_handlers = install_then_signal_after_restore

connect.run_host_process(
    server_url="https://app.example.databricks.com", config_path=Path(config_path)
)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
@pytest.mark.parametrize(
    ("when", "reason"),
    [
        # The signal path owns an exit it reaches first.
        ("in-body", "signal"),
        # The exit was already reported; the signal waits for its drain.
        ("during-drain", "clean"),
        # ...and restoring a previous handler must not swallow the re-raise.
        ("during-drain-with-previous-handler", "clean"),
        # A signal landing inside the restore loop still owns the exit.
        ("mid-restore-with-previous-handler", "clean"),
        # Handlers are back to default, but the row was drained first.
        ("after-restore", "clean"),
    ],
)
def test_stop_signal_racing_a_normal_return_still_owns_the_exit(
    tmp_path: Path, signum: int, when: str, reason: str
) -> None:
    """Exactly one exit row is delivered, and the process dies by the signal."""
    rows_path = tmp_path / "rows.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _RACING_CHILD,
            str(rows_path),
            str(int(signum)),
            when,
            str(tmp_path / "config.yaml"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == -signum, proc.stderr
    (row,) = _exit_rows(rows_path)
    assert isinstance(row["attributes"], dict)
    assert row["attributes"]["reason"] == reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_inherited_ignored_sighup_stays_ignored(tmp_path: Path) -> None:
    """A host started under nohup keeps ignoring SIGHUP; SIGTERM still works."""
    rows_path = tmp_path / "rows.jsonl"
    proc = _run_child(
        """
        import signal
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # as inherited from nohup
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
        proc.send_signal(signal.SIGHUP)
        time.sleep(1.0)
        assert proc.poll() is None, "SIGHUP killed a host that inherited SIG_IGN"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == -signal.SIGTERM
    (row,) = _exit_rows(rows_path)
    assert isinstance(row["attributes"], dict)
    assert row["attributes"]["signal"] == "SIGTERM"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_stop_signal_still_kills_when_the_exit_thread_cannot_start(tmp_path: Path) -> None:
    """Thread exhaustion skips the report but keeps signal death and doesn't hang."""
    rows_path = tmp_path / "rows.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _RACING_CHILD,
            str(rows_path),
            str(int(signal.SIGTERM)),
            "in-body-no-thread",
            str(tmp_path / "config.yaml"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == -signal.SIGTERM, proc.stderr
    assert "cannot join thread" not in proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_stop_signal_still_kills_when_the_watchdog_cannot_start(tmp_path: Path) -> None:
    """Without a watchdog there's no deadline: die by the signal right away."""
    rows_path = tmp_path / "rows.jsonl"
    proc = _run_child(
        """
        import threading

        def _refuse(self):
            raise RuntimeError("can't start new thread")

        threading.Timer.start = _refuse  # the exit thread itself still starts
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
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == -signal.SIGTERM
